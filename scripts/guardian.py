#!/usr/bin/env python3
"""External watchdog that keeps the monitor service healthy."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config import MonitorConfig, load_config
from src.notifier import DiscordNotifier
from src.state import MonitorState
from src.uptime import current_status, load_uptime_segments, summarize_uptime

MONITOR_LABEL = "com.ticketmonitor"
BROWSER_HOST_LABEL = f"{MONITOR_LABEL}.browser-host"
GUARDIAN_LABEL = f"{MONITOR_LABEL}.guardian"
RELOADER_LABEL = f"{MONITOR_LABEL}.reloader"
LOG_FILE = ROOT_DIR / "logs" / "guardian.log"
UPTIME_LOG_FILE = ROOT_DIR / "uptime_log.json"
# The GUI holds an exclusive flock on this file for its whole lifetime (see
# app.py acquire_gui_single_instance_lock). Lock held => the app is open.
GUI_LOCK_FILE = ROOT_DIR / "logs" / "gui.lock"

# Last-resort reboot: a plain `shutdown -r now` via a scoped NOPASSWD sudoers rule
# (one-time manual setup — see scripts/setup_selfheal_reboot.sh). This ONLY reaches
# the desktop unattended when FileVault is off and automatic login is configured; with
# FileVault on, `fdesetup authrestart` unlocks the disk silently but still leaves a
# login-window flash requiring a password on this machine/macOS version (verified via
# a real reboot — the "onetimeAutoLogin" auto-completion did not occur), so it does
# NOT achieve zero-touch and isn't used here.
REBOOT_COMMAND = ["sudo", "-n", "/sbin/shutdown", "-r", "now"]
LOGINWINDOW_PREFS = Path("/Library/Preferences/com.apple.loginwindow")


@dataclass
class ServiceStatus:
    running: bool
    pid: int | None


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] guardian: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _launchctl_target(label: str = MONITOR_LABEL) -> str:
    return f"gui/{os.getuid()}/{label}"


def get_service_status(label: str = MONITOR_LABEL) -> ServiceStatus:
    cmd = ["launchctl", "print", _launchctl_target(label)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return ServiceStatus(running=False, pid=None)

    output = proc.stdout + "\n" + proc.stderr
    running = "state = running" in output
    pid_match = re.search(r"\bpid = (\d+)\b", output)
    pid = int(pid_match.group(1)) if pid_match else None
    return ServiceStatus(running=running, pid=pid)


def kickstart_service(label: str = MONITOR_LABEL) -> bool:
    cmd = ["launchctl", "kickstart", "-k", _launchctl_target(label)]
    return subprocess.run(cmd).returncode == 0


def _cdp_endpoint_reachable(endpoint_url: str, timeout: float = 3.0) -> bool:
    """Return True if the Chrome CDP endpoint responds at /json/version."""
    url = endpoint_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for_cdp_endpoint(endpoint_url: str, max_seconds: int = 30) -> bool:
    """Poll the CDP endpoint every second until it responds or max_seconds elapses."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if _cdp_endpoint_reachable(endpoint_url):
            return True
        time.sleep(1.0)
    return False


def _list_processes() -> list[tuple[int, int, str]]:
    proc = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True)
    if proc.returncode != 0:
        return []

    rows: list[tuple[int, int, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows.append((pid, ppid, parts[2]))
    return rows


def _descendants(rows: list[tuple[int, int, str]], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid, _cmd in rows:
        children.setdefault(ppid, []).append(pid)

    found: set[int] = set()
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def kill_orphaned_playwright_processes(repo_dir: Path, monitor_pid: int | None) -> int:
    rows = _list_processes()
    descendants = _descendants(rows, monitor_pid) if monitor_pid else set()

    killed = 0
    for pid, _ppid, command in rows:
        if pid == os.getpid():
            continue
        command_lower = command.lower()
        is_playwright = (
            "playwright" in command_lower
            or "chrome-headless-shell" in command_lower
            or "chromium_headless_shell" in command_lower
            or ("google chrome" in command_lower and "--remote-debugging-pipe" in command_lower)
        )
        if not is_playwright:
            continue

        related = str(repo_dir) in command or pid in descendants
        if not related:
            continue

        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            continue
    return killed


def is_stale(state: MonitorState, stale_after_seconds: int, now: datetime) -> tuple[bool, float]:
    """Age since the last sign of cycle *progress* (start or completion).

    A cycle can legitimately run for minutes (slow Chrome launch, challenge-cooldown
    sleep), so a fresh ``last_cycle_started_at`` counts as alive — only a monitor
    that is neither starting nor finishing cycles is stale.
    """
    marks = [
        d
        for d in (state.get_last_cycle_completed_at(), state.get_last_cycle_started_at())
        if d is not None
    ]
    if not marks:
        return True, float("inf")
    age = (now - max(marks)).total_seconds()
    return age > stale_after_seconds, age


def build_unhealthy_reason(service: ServiceStatus, stale: bool, stale_age_seconds: float, error_burst: bool, force_fix: bool, cdp_unreachable: bool = False) -> str:
    reasons: list[str] = []
    if force_fix:
        reasons.append("force_fix")
    if not service.running:
        reasons.append("service_not_running")
    if stale:
        if stale_age_seconds == float("inf"):
            reasons.append("no_successful_cycle_recorded")
        else:
            reasons.append(f"stale_health:{int(stale_age_seconds)}s")
    if error_burst:
        reasons.append("error_burst_detected")
    if cdp_unreachable:
        reasons.append("cdp_endpoint_unreachable")
    return ", ".join(reasons) or "unknown_health_issue"


def get_system_uptime_seconds(now: datetime | None = None) -> float:
    """Seconds since the Mac booted (0.0 when unknown, which fails the uptime guard)."""
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return 0.0
    match = re.search(r"sec\s*=\s*(\d+)", proc.stdout or "")
    if not match:
        return 0.0
    booted = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
    return max(0.0, ((now or datetime.now(timezone.utc)) - booted).total_seconds())


def impaired_since(now: datetime, segments: list[dict] | None = None) -> datetime | None:
    """Start of the current contiguous non-healthy stretch in the uptime ledger,
    or None when the monitor is currently healthy."""
    if segments is None:
        segments = load_uptime_segments(str(UPTIME_LOG_FILE))
    if not segments:
        return None
    status = current_status(segments, now=now)
    if status.get("state") == "healthy":
        return None
    since: datetime | None = None
    for segment in reversed(segments):
        if segment.get("state") == "healthy":
            break
        start = segment.get("start")
        try:
            since = datetime.fromisoformat(str(start))
        except (TypeError, ValueError):
            break
    return since


# Reboot eligibility window: the monitor must be essentially healthy-free over this
# span. A thrashing monitor interleaves short healthy blips with blocks — the old
# "contiguous non-healthy stretch" rule reset on every blip, so the reboot could
# never fire during exactly the churn it exists for.
REBOOT_WINDOW_HOURS = 1
REBOOT_MAX_HEALTHY_FRACTION = 0.10
# A stored variation-probe verdict older than this no longer describes the current
# block; run a fresh matrix before acting on it.
PROBE_REPORT_FRESH_SECONDS = 7200


def evaluate_reboot(
    *,
    config: MonitorConfig,
    now: datetime,
    window_summary: dict,
    probe_scope: str | None,
    system_uptime_seconds: float,
    last_reboot_at: datetime | None,
    reboots_last_day: int,
    fix_attempts_last_hour: int,
) -> tuple[bool, str]:
    """Decide whether a last-resort self-heal reboot is warranted (pure function).

    ``window_summary`` is :func:`src.uptime.summarize_uptime` over the last
    ``REBOOT_WINDOW_HOURS``. Returns (should_reboot, reason). Every guard is designed
    so a reboot can never loop: minimum recorded unhealthy time, minimum system
    uptime since the LAST boot, minimum spacing between self-heal reboots, and a
    hard daily cap.
    """
    if not config.watchdog_reboot_enabled:
        return False, "reboot_disabled"
    total_s = float(window_summary.get("total_s", 0) or 0)
    healthy_s = float(window_summary.get("healthy_s", 0) or 0)
    if total_s < config.watchdog_reboot_after_impaired_seconds:
        return False, f"window_only_{int(total_s)}s_recorded"
    if healthy_s > total_s * REBOOT_MAX_HEALTHY_FRACTION:
        healthy_pct = int(round(100.0 * healthy_s / total_s))
        return False, f"healthy_{healthy_pct}pct_in_window"
    # A reboot only plausibly helps when the block isn't cookie- or account-scoped.
    # Those scopes have targeted remedies; rebooting would burn a slot for nothing.
    if probe_scope in {"profile", "account", "none"}:
        return False, f"scope_{probe_scope}_has_targeted_remedy"
    if fix_attempts_last_hour < 1:
        return False, "lighter_remedies_not_tried_yet"
    if system_uptime_seconds < config.watchdog_reboot_min_system_uptime_seconds:
        return False, f"system_uptime_only_{int(system_uptime_seconds)}s"
    if last_reboot_at is not None:
        spacing = (now - last_reboot_at).total_seconds()
        if spacing < config.watchdog_reboot_min_spacing_seconds:
            return False, f"last_selfheal_reboot_{int(spacing)}s_ago"
    if reboots_last_day >= config.watchdog_reboot_max_per_day:
        return False, "daily_reboot_cap_reached"
    unhealthy_s = int(total_s - healthy_s)
    return True, f"unhealthy_{unhealthy_s}s_of_{int(total_s)}s_scope_{probe_scope or 'unknown'}"


def reboot_available() -> tuple[bool, str]:
    """A reboot only reaches the desktop unattended when FileVault is off (nothing to
    unlock pre-boot) and macOS automatic login is configured for this account."""
    try:
        proc = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=5)
    except Exception as exc:
        return False, f"fdesetup unavailable: {exc}"
    if "filevault is off" not in (proc.stdout or "").lower():
        return False, "FileVault is on — an unattended reboot would strand at the login screen"
    try:
        proc = subprocess.run(
            ["defaults", "read", str(LOGINWINDOW_PREFS), "autoLoginUser"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        return False, f"could not check automatic login: {exc}"
    if not (proc.stdout or "").strip():
        return False, "Automatic login is not configured (System Settings > Users & Groups > Login Options)"
    return True, "ok"


def trigger_selfheal_reboot(state: MonitorState, notifier: DiscordNotifier, reason: str, now: datetime) -> bool:
    """Record the reboot, tell Discord, then reboot. Recording happens FIRST so the
    rate-limiter survives even if the machine goes down mid-call."""
    logger = logging.getLogger("guardian")
    state.record_selfheal_reboot(now)
    try:
        notifier.send_auto_fix_action(
            action="selfheal_reboot",
            reason=reason,
            context={"reason_code": "guardian_selfheal_reboot"},
            auto_fix_planned="reboot",
        )
    except Exception as exc:  # Discord must never block the reboot
        logger.warning("Reboot notice failed to send: %s", exc)
    logger.warning("Triggering self-heal reboot: %s", reason)
    try:
        proc = subprocess.run(REBOOT_COMMAND, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        logger.error("reboot failed to launch: %s", exc)
        return False
    if proc.returncode != 0:
        logger.error(
            "reboot exited %d: %s", proc.returncode, (proc.stderr or proc.stdout).strip()
        )
        return False
    return True


def probe_report_scope(report: dict, now: datetime) -> str | None:
    """The stored variation-probe verdict, or None when missing/expired.

    A days-old verdict describes a different block than the one happening now;
    treating it as unknown makes the guardian run a fresh matrix before acting."""
    scope = report.get("scope")
    if not scope:
        return None
    at = report.get("at")
    try:
        taken_at = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=timezone.utc)
    if (now - taken_at).total_seconds() > PROBE_REPORT_FRESH_SECONDS:
        return None
    return scope


def _run_variation_probe_for_reboot(config: MonitorConfig, state: MonitorState) -> dict | None:
    """Best-effort block-scope diagnosis before a reboot; never raises.

    The matrix launches its own short-lived browsers against a temp COPY of the
    profile, so the live monitor's Chrome is untouched."""
    logger = logging.getLogger("guardian")
    try:
        from src.variation_probe import run_variation_matrix

        report = run_variation_matrix(config).to_dict()
    except Exception as exc:
        logger.warning("Pre-reboot variation probe failed: %s", exc)
        return None
    state.set_variation_probe_report(report)
    return report


def maybe_selfheal_reboot(
    config: MonitorConfig, state: MonitorState, notifier: DiscordNotifier, now: datetime
) -> bool:
    """Run the reboot decision; returns True when a reboot was triggered."""
    logger = logging.getLogger("guardian")
    if not config.watchdog_reboot_enabled:
        return False
    window_summary = summarize_uptime(
        load_uptime_segments(str(UPTIME_LOG_FILE)), hours=REBOOT_WINDOW_HOURS, now=now
    )
    decision_kwargs = dict(
        config=config,
        now=now,
        window_summary=window_summary,
        system_uptime_seconds=get_system_uptime_seconds(now),
        last_reboot_at=state.get_last_selfheal_reboot_at(),
        reboots_last_day=state.get_selfheal_reboots_recent(86400, now=now),
        fix_attempts_last_hour=state.get_guardian_fix_attempts_last_hour(),
    )
    scope = probe_report_scope(state.get_variation_probe_report() or {}, now)
    should, reason = evaluate_reboot(probe_scope=scope, **decision_kwargs)
    if should and scope is None:
        # Reboot warranted but undiagnosed — find out what scope the block actually
        # has first, so a cookie- or account-scoped block gets its targeted remedy
        # instead of burning a reboot slot. (The monitor-side trigger for this probe
        # is rarely reached, so the guardian owns the pre-reboot diagnosis.)
        logger.warning("Reboot warranted (%s) but block scope unknown — running variation probe", reason)
        report = _run_variation_probe_for_reboot(config, state)
        if report is not None:
            scope = report.get("scope")
            should, reason = evaluate_reboot(probe_scope=scope, **decision_kwargs)
    if not should:
        logger.debug("Reboot tier: not warranted (%s)", reason)
        return False
    available, detail = reboot_available()
    if not available:
        logger.error("Reboot warranted (%s) but reboot unavailable: %s", reason, detail)
        if should_alert_critical(state, now=now):
            notifier.send_critical_attention(
                f"A self-heal reboot is warranted ({reason}) but isn't safe to trigger: {detail}",
                context={"reason_code": "guardian_reboot_unavailable"},
                next_steps=["Run scripts/setup_selfheal_reboot.sh (one-time sudo setup)"],
            )
            state.set_guardian_last_critical_alert_at(now)
        return False
    return trigger_selfheal_reboot(state, notifier, reason, now)


# Staleness must persist across this many consecutive guardian passes (~2 min apart)
# before remediation. One stale pass is routinely just a slow cycle.
STALE_STRIKES_REQUIRED = 2

# Monitoring must always coincide with the GUI: when the app isn't open, the whole
# stack gets stopped rather than kept alive headless. The GUI must be absent for
# this many consecutive passes first (a relaunching GUI briefly drops its lock),
# and never within the post-boot grace (launchd may still be opening the app).
GUI_ABSENT_STRIKES_REQUIRED = 2
GUI_ENFORCEMENT_MIN_UPTIME_SECONDS = 300


def gui_is_running(lock_path: Path = GUI_LOCK_FILE) -> bool:
    """True when the GUI app holds its single-instance lock (i.e. it is open).

    An OS file lock dies with its process, so this is crash-proof: a force-quit
    GUI releases the lock even though it never ran any cleanup."""
    from src.state import try_lock_file_exclusive, unlock_file

    try:
        with open(lock_path, "a+", encoding="utf-8") as handle:
            if not try_lock_file_exclusive(handle):
                return True
            unlock_file(handle)
    except OSError:
        return False
    return False


def stop_monitoring_stack() -> None:
    """Boot the monitor, reloader, then the guardian itself out of launchd.

    Guardian last: booting out our own service kills this process, so it must be
    the final action of the pass."""
    for label in (MONITOR_LABEL, RELOADER_LABEL, GUARDIAN_LABEL):
        subprocess.run(
            ["launchctl", "bootout", _launchctl_target(label)],
            capture_output=True,
            text=True,
        )


def enforce_gui_coincidence(
    state: MonitorState, notifier: DiscordNotifier, now: datetime
) -> bool:
    """Stop the monitoring stack when the GUI is closed. Returns True when the GUI
    is absent (whether or not the stack was stopped this pass) — the caller should
    end its pass without remediating: fixing a monitor that is about to be stopped
    just churns Chrome."""
    logger = logging.getLogger("guardian")
    if gui_is_running():
        if state.get_gui_absent_strikes():
            state.set_gui_absent_strikes(0)
        return False
    if get_system_uptime_seconds(now) < GUI_ENFORCEMENT_MIN_UPTIME_SECONDS:
        logger.info("GUI not up yet, but the system just booted — giving launchd time to open it")
        return True

    strikes = state.get_gui_absent_strikes() + 1
    state.set_gui_absent_strikes(strikes)
    if strikes < GUI_ABSENT_STRIKES_REQUIRED:
        logger.warning(
            "GUI appears closed — strike %d/%d, stopping monitoring next pass if it stays closed",
            strikes,
            GUI_ABSENT_STRIKES_REQUIRED,
        )
        return True

    state.set_gui_absent_strikes(0)
    logger.warning("GUI is closed — stopping the monitoring stack (monitoring only runs while the app is open)")
    try:
        notifier.send_critical_attention(
            "The Ticket Monitor app was closed, so ticket monitoring has been stopped "
            "(monitoring only runs while the app is open). Open the app and press "
            "Start Monitor to resume.",
            context={"reason_code": "guardian_gui_closed_stop"},
            next_steps=["Open the Ticket Monitor app", "Press “Start Monitor”"],
        )
    except Exception as exc:  # the stop must proceed even if Discord is down
        logger.warning("GUI-closed notice failed to send: %s", exc)
    stop_monitoring_stack()
    return True


def _confirm_staleness(
    *,
    state: MonitorState,
    stale: bool,
    stale_age_seconds: float,
    service_running: bool,
    force_fix: bool,
    now: datetime,
) -> bool:
    """Demote a raw stale reading unless it deserves remediation.

    Two protections, both only for a monitor process that is actually running
    (a dead service is handled by the service_not_running path regardless):

    - An active challenge cooldown means the monitor is deliberately quiet.
      Kickstarting it would relaunch Chrome straight into the block it is
      waiting out, so staleness is ignored for the cooldown's duration.
    - Otherwise staleness must persist for STALE_STRIKES_REQUIRED consecutive
      guardian passes; a single strike is logged and given one more pass.
    """
    logger = logging.getLogger("guardian")
    if force_fix or not service_running:
        return stale
    if not stale:
        if state.get_guardian_stale_strikes():
            state.set_guardian_stale_strikes(0)
        return False

    cooldown_until = state.get_challenge_cooldown_until()
    if cooldown_until is not None and now < cooldown_until:
        logger.info(
            "Monitor is stale (%.0fs) but inside a challenge cooldown until %s — leaving it alone",
            stale_age_seconds,
            cooldown_until.isoformat(),
        )
        return False

    strikes = state.get_guardian_stale_strikes() + 1
    state.set_guardian_stale_strikes(strikes)
    if strikes < STALE_STRIKES_REQUIRED:
        logger.warning(
            "Monitor looks stale (%.0fs) — strike %d/%d, waiting one more pass before remediating",
            stale_age_seconds,
            strikes,
            STALE_STRIKES_REQUIRED,
        )
        return False
    return True


def should_alert_critical(state: MonitorState, now: datetime, cooldown_seconds: int = 1800) -> bool:
    last = state.get_guardian_last_critical_alert_at()
    if last is None:
        return True
    return (now - last).total_seconds() >= cooldown_seconds


def run_guardian(config: MonitorConfig, force_fix: bool = False) -> int:
    logger = logging.getLogger("guardian")
    if not config.watchdog_enabled and not force_fix:
        logger.info("Watchdog is disabled in config; exiting")
        return 0

    state = MonitorState()
    notifier = DiscordNotifier(
        webhook_url=config.discord_webhook_url,
        username=config.discord_username,
        ping_user_id=config.discord_ping_user_id,
    )
    now = datetime.now(timezone.utc)

    # GUI coincidence comes first: monitoring only runs while the app is open.
    # If the GUI is gone, nothing below matters — remediating (or rebooting!) on
    # behalf of a stack that is about to be stopped would be pure churn.
    if not force_fix and enforce_gui_coincidence(state, notifier, now):
        return 0

    service = get_service_status()
    stale, stale_age_seconds = is_stale(state, config.watchdog_stale_after_seconds, now)
    stale = _confirm_staleness(
        state=state,
        stale=stale,
        stale_age_seconds=stale_age_seconds,
        service_running=service.running,
        force_fix=force_fix,
        now=now,
    )
    browser_restarts_recent = state.get_browser_restart_count_recent(
        config.self_heal_process_restart_window_seconds,
        now=now,
    )
    event_distress = any(
        state.get_in_outage_state(event.event_id) or state.get_consecutive_blocked(event.event_id) > 0
        for event in config.events
    )
    # Only treat restart bursts as unhealthy when events are actively in distress.
    error_burst = (
        browser_restarts_recent >= config.self_heal_process_restart_threshold
        and event_distress
    )
    cdp_reachable = True
    if config.browser_session_mode == "cdp_attach":
        cdp_reachable = _cdp_endpoint_reachable(config.browser_cdp_endpoint_url)
    unhealthy = force_fix or not service.running or stale or error_burst or not cdp_reachable

    # Last-resort tier runs on every pass, independent of process liveness: a monitor
    # that is alive but has been blocked for 45+ minutes looks "healthy" to the
    # process checks above, yet is exactly the case a reboot exists for.
    if maybe_selfheal_reboot(config, state, notifier, now):
        logger.warning("Self-heal reboot triggered; the machine is going down")
        return 0

    if not unhealthy:
        logger.info(
            "Health check OK: service_running=%s stale=%s error_burst=%s recent_browser_restarts=%d event_distress=%s cdp_reachable=%s",
            service.running,
            stale,
            error_burst,
            browser_restarts_recent,
            event_distress,
            cdp_reachable,
        )
        return 0

    reason = build_unhealthy_reason(service, stale, stale_age_seconds, error_burst, force_fix, cdp_unreachable=not cdp_reachable)
    pause_until = state.get_guardian_pause_until()
    if pause_until is not None and now < pause_until and not force_fix:
        logger.warning("Guardian is in cooldown pause until %s; skipping aggressive remediation", pause_until.isoformat())
        return 1

    attempts_last_hour = state.get_guardian_fix_attempts_last_hour()
    # Throttle only burst-driven loops; still allow remediation for true liveness failures.
    if (
        attempts_last_hour >= config.watchdog_max_fix_attempts_per_hour
        and not force_fix
        and error_burst
    ):
        pause_target = now + timedelta(minutes=30)
        state.set_guardian_pause_until(pause_target)
        if should_alert_critical(state, now=now):
            notifier.send_critical_attention(
                "Guardian exhausted auto-fix attempts. "
                "Manual intervention is required.",
                context={"reason_code": "guardian_auto_fix_exhausted"},
                next_steps=[
                    "scripts/monitorctl.sh status",
                    "scripts/monitorctl.sh logs",
                    "scripts/monitorctl.sh reauth",
                ],
            )
            state.set_guardian_last_critical_alert_at(now)
        logger.error(
            "Exceeded max guardian attempts/hour (%d). Pausing fixes until %s",
            config.watchdog_max_fix_attempts_per_hour,
            pause_target.isoformat(),
        )
        return 1

    browser_host_restarted = False
    if config.browser_session_mode == "cdp_attach" and not cdp_reachable and config.browser_host_enabled:
        logger.warning("CDP endpoint unreachable at %s; restarting browser-host first", config.browser_cdp_endpoint_url)
        kickstart_service(BROWSER_HOST_LABEL)
        if _wait_for_cdp_endpoint(config.browser_cdp_endpoint_url, max_seconds=30):
            logger.info("CDP endpoint is up after browser-host restart")
        else:
            logger.error("CDP endpoint still unreachable after 30s; monitor restart may also fail")
        browser_host_restarted = True

    if attempts_last_hour == 0 or force_fix:
        ok = kickstart_service()
        action = ("restart_browser_host+" if browser_host_restarted else "") + "kickstart_service"
    else:
        killed = kill_orphaned_playwright_processes(ROOT_DIR, service.pid)
        ok = kickstart_service()
        base = f"kill_playwright_orphans({killed})+kickstart_service"
        action = ("restart_browser_host+" if browser_host_restarted else "") + base

    state.record_guardian_fix_attempt(now)
    state.record_process_restart_request(now)
    state.set_last_auto_fix_at(now)
    state.set_guardian_stale_strikes(0)
    notifier.send_auto_fix_action(
        action=action,
        reason=reason,
        context={"reason_code": "guardian_remediation"},
        auto_fix_planned="health_recheck",
    )
    logger.warning("Performed remediation: %s, reason=%s", action, reason)

    time.sleep(2)
    post_status = get_service_status()
    if post_status.running:
        logger.info("Service healthy after remediation")
        return 0

    logger.error("Service still unhealthy after remediation")
    return 1


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="External watchdog for ticket monitor service")
    parser.add_argument("--config", default="config.yaml", help="Path to monitor config")
    parser.add_argument("--force-fix", action="store_true", help="Run remediation flow even if monitor appears healthy")
    args = parser.parse_args()

    config = load_config(args.config)
    exit_code = run_guardian(config=config, force_fix=args.force_fix)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
