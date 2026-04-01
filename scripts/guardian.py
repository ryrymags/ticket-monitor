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

MONITOR_LABEL = "com.ticketmonitor"
BROWSER_HOST_LABEL = f"{MONITOR_LABEL}.browser-host"
LOG_FILE = ROOT_DIR / "logs" / "guardian.log"


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
    last_completed = state.get_last_cycle_completed_at()
    if last_completed is None:
        return True, float("inf")
    age = (now - last_completed).total_seconds()
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
    service = get_service_status()
    stale, stale_age_seconds = is_stale(state, config.watchdog_stale_after_seconds, now)
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
