"""Polling scheduler — browser-first monitoring loop with outage detection."""

from __future__ import annotations

import os
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .browser_probe import BrowserProbe, BrowserProbeError
from .config import EventConfig, MonitorConfig
from .detector import Detector
from .models import ProbeSignalType
from .notifier import DiscordNotifier
from .session_autofix import TicketmasterSessionAutoFixer
from .state import MonitorState
from .uptime import UptimeLedger

logger = logging.getLogger(__name__)

PROCESS_RESTART_EXIT_CODE = 75
BROWSER_RESTART_REQUEST_FILE = "logs/restart-browser.request"
# Mention cadence for a single BINGO availability episode:
#   0–120s : ping on every qualifying poll (a live drop — every second counts).
#   120–300s: ping at most once per minute (likely lingering / pricey).
#   after 300s: stop pinging for this episode (no hours-long ping spam).
BURST_PHASE1_SECONDS = 120
BURST_PHASE2_SECONDS = 300
BURST_PHASE2_INTERVAL_SECONDS = 60
BURST_HARD_FAILSAFE_SECONDS = 900
# How many times to retry a failed critical-attention send before giving up for the
# episode (avoids hammering a permanently-broken webhook every cycle).
MAX_ATTENTION_ALERT_ATTEMPTS = 6
# Cap on a single challenge-cooldown sleep so the loop keeps re-evaluating the
# manual-action escalation (the cooldown itself persists in state and resumes).
CHALLENGE_COOLDOWN_SLEEP_CAP_SECONDS = 120
# Absolute path so the fallback terminal commands work from any directory — the old
# relative "scripts/monitorctl.sh" only worked if you'd already cd'd into the repo.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MONITORCTL = os.path.join(_REPO_ROOT, "scripts", "monitorctl.sh")

# The ONLY manual action is logging back in (everything else self-heals). Point at the
# double-click Desktop file first, then the in-app Login tab.
LOGIN_MANUAL_STEPS = [
    "Double-click “Ticket Monitor Reauth.command” on your Desktop, or",
    "open the Ticket Monitor app → Login tab → “Log In to Ticketmaster”.",
]
# Kept for the auto-reauth probe-reload error path (a different, internal failure).
REAUTH_MANUAL_STEPS = LOGIN_MANUAL_STEPS

# After this many consecutive blind checks, escalate the flush: clear the DataDome token
# (keeping login cookies) to shed a poisoned block cookie. ~2x the outage threshold.
PROLONGED_BLOCK_COOKIE_FLUSH_MULTIPLIER = 2
# A loop iteration that wakes this many seconds later than intended means the system was
# suspended (sleep) — not the monitor going stale. Re-anchor instead of crying stale.
SLEEP_OVERSHOOT_SECONDS = 120

# Chromium net-stack errors that mean the page never loaded because there's no
# working internet connection (as opposed to Ticketmaster loading a block page).
# When a cycle hits these and gets NO response from any event, monitoring is DOWN,
# not merely impaired — we can't see tickets at all.
_CONNECTIVITY_ERROR_SIGNATURES = (
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_name_resolution_failed",
    "err_network_changed",
    "err_network_access_denied",
    "err_address_unreachable",
    "err_connection_refused",
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_timed_out",
    "err_connection_aborted",
    "err_proxy_connection_failed",
    "err_socket_not_connected",
    "err_dns",
)


@dataclass
class _EventCheckOutcome:
    needs_slow_retry: bool = False
    had_response: bool = False
    had_connectivity_error: bool = False
    result: Any | None = None


def _is_connectivity_error(exc: Exception) -> bool:
    """True when an exception looks like a lost/absent internet connection."""
    texts = [str(exc).lower()]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        texts.append(str(cause).lower())
    blob = " ".join(texts)
    return any(sig in blob for sig in _CONNECTIVITY_ERROR_SIGNATURES)


class MonitorScheduler:
    """Orchestrates the monitoring loop."""

    def __init__(
        self,
        config: MonitorConfig,
        notifier: DiscordNotifier,
        state: MonitorState,
        start_time: datetime,
        probe: BrowserProbe | None = None,
        detector: Detector | None = None,
        rand: random.Random | None = None,
        session_autofixer: TicketmasterSessionAutoFixer | None = None,
    ):
        self.config = config
        self.notifier = notifier
        self.state = state
        self.start_time = start_time
        # Durable uptime/downtime timeline (healthy/impaired/down). One heartbeat
        # per cycle; down intervals are inferred from gaps in the stream. Co-located
        # with the state file so it lands beside state.json in prod and inside the
        # tmp dir under tests.
        uptime_path = os.path.join(
            os.path.dirname(getattr(state, "state_file", "")) or ".", "uptime_log.json"
        )
        self.uptime = UptimeLedger(path=uptime_path)

        self.probe = probe or BrowserProbe(
            storage_state_path=config.browser_storage_state_path,
            session_mode=config.browser_session_mode,
            user_data_dir=config.browser_user_data_dir,
            channel=config.browser_channel,
            cdp_endpoint_url=config.browser_cdp_endpoint_url,
            cdp_connect_timeout_seconds=config.browser_cdp_connect_timeout_seconds,
            reuse_event_tabs=config.browser_reuse_event_tabs,
            single_event_page=config.browser_single_event_page,
            headless=config.browser_headless,
            navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
            stealth_enabled=config.browser_stealth_enabled,
            locale=config.browser_locale,
            timezone_id=config.browser_timezone_id,
            event_dwell_min_seconds=config.browser_event_dwell_min_seconds,
            event_dwell_max_seconds=config.browser_event_dwell_max_seconds,
            homepage_warmup_interval_seconds=config.browser_homepage_warmup_interval_seconds,
        )
        self.detector = detector or Detector(config.alerts_ticket_cooldown_seconds)
        self._rand = rand or random.Random()
        self.session_autofixer = session_autofixer
        if self.session_autofixer is None and config.auth_auto_login_enabled:
            self.session_autofixer = TicketmasterSessionAutoFixer(
                keychain_service=config.auth_keychain_service,
                keychain_email_account=config.auth_keychain_email_account,
                keychain_password_account=config.auth_keychain_password_account,
            )

        self._running = True
        self._consecutive_runtime_errors = 0
        self._last_successful_check: datetime | None = state.get_last_successful_check()
        self._browser_error_times: deque[datetime] = deque()
        self._last_error_alert_at: datetime | None = None
        self._last_browser_recycle_at: datetime | None = None
        self._stale_event_alerted: set[str] = set()
        self._last_session_health_alert_at: datetime | None = None
        self._session_health_fail_streak: int = 0
        self._last_cycle_blocked: bool = False
        # Adaptive cadence: multiplier (>= 1.0) applied to the random poll floor.
        # Grows when a cycle is blocked/challenged, decays back toward 1.0 when healthy.
        self._cadence_backoff: float = 1.0
        # Challenge circuit-breaker: consecutive captcha/challenge (or 429) checks drive
        # an exponential cooldown so we stop hammering a fingerprint that's being blocked.
        self._consecutive_challenges: int = 0
        # Staleness only counts time the monitor is actually running — anchored at start
        # and re-anchored after a detected system sleep (set in run()).
        self._uptime_anchor: datetime = start_time
        # How long the loop deliberately slept before the current cycle. Lets the
        # uptime ledger tell an intended backoff (impairment) apart from an unplanned
        # silence (downtime). None on the first cycle of a process.
        self._last_loop_sleep_seconds: float | None = None
        # Set each cycle: True when no event got any response AND at least one failed
        # with a connectivity error → the internet is down (uptime = down, not impaired).
        self._cycle_connectivity_down: bool = False
        # Per-cycle check tally — the uptime state reflects what THIS cycle's checks
        # actually did (clean vs blocked/errored), not lagging cadence/outage flags.
        self._cycle_healthy_checks: int = 0
        self._cycle_bad_checks: int = 0
        self._event_schedule: dict[str, datetime] = {}
        self._next_event_check_not_before: datetime | None = None

    def stop(self):
        """Signal the loop to stop."""
        self._running = False

    def run(self):
        """Main loop — runs until stop() is called or interrupted."""
        logger.info("Monitor started. Checking %d event(s).", len(self.config.events))
        # Fresh run: don't inherit a stale degraded/attention clock from before a restart
        # or sleep. The stale clock only counts time the monitor is actually running.
        self._uptime_anchor = datetime.now(timezone.utc)
        self._expected_wake: datetime | None = None
        self.state.clear_attention()
        self.state.set_degraded_state(False)
        # Close any downtime gap right at startup so the recorded "down" spans
        # last-check → this moment (monitor start), not last-check → first pull.
        self.uptime.mark_online()
        try:
            self.probe.start()
        except BrowserProbeError as exc:
            logger.error("Failed to start browser probe: %s", exc)
            self.notifier.send_monitor_blocked(
                "monitor",
                "Browser probe could not start for this cycle.",
                context={"event_name": "monitor"},
                auto_fix_planned="launchd_restart_expected",
                manual_required=False,
            )

        while self._running:
            # Detect a system sleep/suspend: if we woke far later than we intended to,
            # re-anchor uptime so the gap isn't counted as the monitor "going stale".
            loop_now = datetime.now(timezone.utc)
            if self._expected_wake is not None:
                overshoot = (loop_now - self._expected_wake).total_seconds()
                if overshoot > SLEEP_OVERSHOOT_SECONDS:
                    logger.warning(
                        "Detected ~%.0fs sleep/suspend gap — re-anchoring uptime, skipping stale this cycle",
                        overshoot,
                    )
                    self._uptime_anchor = loop_now
                    self._stale_event_alerted.clear()
                    self.state.clear_attention()
            sleep_time = self._normal_loop_sleep()
            self.state.set_last_cycle_started_at()
            try:
                self._maybe_send_heartbeat()
                self._consume_browser_restart_request_if_any()
                loop_check_now = datetime.now(timezone.utc)
                if self._should_skip_cycle_for_challenge_cooldown(loop_check_now):
                    until = self.state.get_challenge_cooldown_until()
                    remaining = (until - loop_check_now).total_seconds() if until else 0.0
                    logger.info(
                        "Challenge cooldown active: %.0fs remaining; skipping active probes",
                        max(0.0, remaining),
                    )
                    self._cycle_connectivity_down = False
                    self._cycle_healthy_checks = 0
                    self._cycle_bad_checks = 0
                    self._last_cycle_blocked = True
                    needs_slow_retry = True
                else:
                    if self._per_event_scheduler_enabled():
                        needs_slow_retry = self._run_due_event_cycle()
                    else:
                        needs_slow_retry = self._run_cycle()
                    self._maybe_check_session_health()
                self._consecutive_runtime_errors = 0
                self.state.set_last_cycle_completed_at()
                self.state.clear_last_error()
                self._record_uptime_heartbeat(
                    needs_slow_retry,
                    reason="blocked" if self._last_cycle_blocked and self._cycle_healthy_checks == 0 else None,
                )

                if self._per_event_scheduler_enabled():
                    sleep_time = self._per_event_next_sleep(datetime.now(timezone.utc))
                else:
                    sleep_time = self._next_sleep(blocked=needs_slow_retry)

            except BrowserProbeError as exc:
                logger.error("Browser probe runtime error: %s", exc)
                self._consecutive_runtime_errors += 1
                self.state.set_last_cycle_completed_at()
                self.state.set_last_error(self._classify_browser_probe_error(exc), str(exc))
                self._record_uptime_heartbeat(
                    True, reason="error", connectivity_lost=_is_connectivity_error(exc)
                )
                self._handle_browser_probe_error(exc)
                sleep_time = self._runtime_error_backoff()

            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)
                self._consecutive_runtime_errors += 1
                self.state.set_last_cycle_completed_at()
                self.state.set_last_error(type(exc).__name__, str(exc))
                self._record_uptime_heartbeat(True, reason="error")
                sleep_time = self._runtime_error_backoff()
                self._maybe_send_error_alert(f"Unexpected monitor error: {type(exc).__name__}: {exc}")

            # Decide (every cycle) whether the monitor has been stuck long enough
            # to warrant a single manual-action ping.
            now = datetime.now(timezone.utc)
            self._evaluate_manual_action_escalation(now)
            # A live challenge cooldown overrides the normal cadence so we stop probing
            # a fingerprint that's being actively blocked.
            sleep_time = self._apply_challenge_cooldown(sleep_time, now)

            if not self._running:
                break
            logger.debug("Next check in %.1f seconds", sleep_time)
            self._expected_wake = datetime.now(timezone.utc) + timedelta(seconds=sleep_time)
            # Remember the intended sleep so the next heartbeat can tell a planned
            # backoff apart from an unplanned silence (down).
            self._last_loop_sleep_seconds = sleep_time
            self._interruptible_sleep(sleep_time)

        self.probe.close()
        self.uptime.flush()

    def _record_uptime_heartbeat(
        self,
        needs_slow_retry: bool,
        reason: str | None = None,
        connectivity_lost: bool = False,
    ):
        """Record one uptime segment heartbeat for the cycle just completed.

        Healthy unless the cycle was blocked/stale/errored or the monitor is in a
        persisted degraded state (logged-out, outage, auth-paused, stale). A lost
        internet connection is recorded as ``down``. The ledger also infers ``down``
        intervals itself from gaps between heartbeats.
        """
        try:
            now = datetime.now(timezone.utc)
            gap = self._last_loop_sleep_seconds
            # No internet (the page won't even load) is DOWN, not impairment — we
            # can't see tickets at all. Ticketmaster *blocking* still returns a page,
            # so that path never sets this flag and stays impaired.
            if connectivity_lost or self._cycle_connectivity_down:
                self.uptime.heartbeat(now, "down", "no_internet", expected_gap_seconds=gap)
                return

            # Classify from THIS cycle's actual results — not lagging cadence/outage
            # flags. Clean scans (even sold-out) are healthy. A block/challenge/error
            # from this cycle is impaired. Session-only problems are used only when
            # no event data flowed this cycle, so the timeline measures monitor
            # visibility while the banner reports auth state.
            if reason is not None or self._cycle_bad_checks > 0:
                state, out_reason = "impaired", (reason or "blocked")
            elif self._cycle_healthy_checks > 0:
                state, out_reason = "healthy", None
            else:
                if self.state.get_session_logged_out():
                    state, out_reason = "impaired", "logged_out"
                else:
                    pause_until = self.state.get_auth_pause_until()
                    if pause_until is not None and now < pause_until:
                        state, out_reason = "impaired", "auth_paused"
                    else:
                        # No checks ran and nothing flagged — fall back to the cadence hint.
                        state = "impaired" if needs_slow_retry else "healthy"
                        out_reason = "blocked" if needs_slow_retry else None

            self.uptime.heartbeat(now, state, out_reason, expected_gap_seconds=gap)
        except Exception as exc:  # pragma: no cover - telemetry must never crash the loop
            logger.debug("uptime heartbeat failed: %s", exc)

    def run_once(self):
        """Run a single check cycle and return (for --once mode)."""
        self.probe.start()
        self.state.set_last_cycle_started_at()
        self._maybe_send_heartbeat()
        if self._per_event_scheduler_enabled():
            self._run_due_event_cycle()
        else:
            self._run_cycle()
        self._maybe_check_session_health()
        self._evaluate_manual_action_escalation(datetime.now(timezone.utc))
        self.state.set_last_cycle_completed_at()
        self.state.clear_last_error()
        self.probe.close()

    # ---- Core logic ----

    def _run_cycle(self) -> bool:
        """Check all events once. Returns True when slow challenge retry mode is needed."""
        needs_slow_retry = False
        # Track connectivity: any response at all proves the internet works; a
        # connectivity error with zero responses means we're offline (→ down).
        # Reset up front so an early throw can't leak a stale value into the next
        # error-path heartbeat.
        self._reset_cycle_tallies()
        had_response = False
        had_connectivity_error = False

        for index, event_cfg in enumerate(self.config.events):
            if not self._running:
                break

            if index > 0:
                # Jitter the inter-event gap a little so request timing looks less robotic.
                stagger = max(0.0, float(self.config.event_stagger_seconds) + self._rand.uniform(-2.0, 2.0))
                self._interruptible_sleep(stagger)

            outcome = self._check_one_event(event_cfg)
            had_response = had_response or outcome.had_response
            had_connectivity_error = had_connectivity_error or outcome.had_connectivity_error
            needs_slow_retry = needs_slow_retry or outcome.needs_slow_retry

        # No response from any event + a connectivity error → the internet is down.
        # (A block page still returns a response, so that stays impaired, not down.)
        self._cycle_connectivity_down = had_connectivity_error and not had_response

        if self._check_event_poll_staleness(now=datetime.now(timezone.utc)):
            needs_slow_retry = True

        # Keep polling slower while any event remains in outage mode.
        for event_cfg in self.config.events:
            if self.state.get_in_outage_state(event_cfg.event_id):
                needs_slow_retry = True
                break

        self._last_cycle_blocked = self._cycle_bad_checks > 0
        return needs_slow_retry

    def _run_due_event_cycle(self) -> bool:
        """Check exactly one due event and reschedule that event."""
        self._reset_cycle_tallies()
        now = datetime.now(timezone.utc)
        event_cfg, wait_seconds = self._select_due_event(now)
        if event_cfg is None:
            logger.debug("No event due yet; next due event in %.1fs", wait_seconds)
            self._last_cycle_blocked = False
            return False

        logger.info("[%s] per-event scheduler wake: checking one due event", event_cfg.name)
        outcome = self._check_one_event(event_cfg)
        checked_at = datetime.now(timezone.utc)
        self._cycle_connectivity_down = outcome.had_connectivity_error and not outcome.had_response
        self._reschedule_event(event_cfg.event_id, checked_at, outcome.result)

        needs_slow_retry = outcome.needs_slow_retry
        if self._check_event_poll_staleness(now=checked_at):
            needs_slow_retry = True

        for configured_event in self.config.events:
            if self.state.get_in_outage_state(configured_event.event_id):
                needs_slow_retry = True
                break

        self._last_cycle_blocked = self._cycle_bad_checks > 0
        return needs_slow_retry

    def _reset_cycle_tallies(self):
        self._cycle_connectivity_down = False
        self._cycle_healthy_checks = 0
        self._cycle_bad_checks = 0

    def _check_one_event(self, event_cfg: EventConfig) -> _EventCheckOutcome:
        try:
            probe_result = self.probe.check_event(event_cfg.event_id, event_cfg.url)
        except BrowserProbeError as exc:
            logger.error("[%s] browser probe failed: %s", event_cfg.name, exc)
            connectivity_error = _is_connectivity_error(exc)
            self.state.set_last_error(self._classify_browser_probe_error(exc), f"{event_cfg.name}: {exc}")
            self._handle_browser_probe_error(exc)
            return _EventCheckOutcome(
                needs_slow_retry=True,
                had_response=False,
                had_connectivity_error=connectivity_error,
            )
        except Exception as exc:
            logger.exception("[%s] unexpected per-event failure: %s", event_cfg.name, exc)
            self.state.set_last_error(type(exc).__name__, f"{event_cfg.name}: {exc}")
            self._maybe_send_error_alert(
                f"Unexpected per-event check failure for {event_cfg.name}: {type(exc).__name__}: {exc}",
                context={"event_name": event_cfg.name, "event_id": event_cfg.event_id},
            )
            return _EventCheckOutcome(needs_slow_retry=True)

        logger.info(
            "[%s] available=%s blocked=%s challenge=%s signal=%s confidence=%.2f",
            event_cfg.name,
            probe_result.available,
            probe_result.blocked,
            probe_result.challenge_detected,
            probe_result.signal_type.value,
            probe_result.signal_confidence,
        )

        needs_slow_retry = probe_result.blocked or probe_result.challenge_detected
        self._handle_probe_result(event_cfg, probe_result)

        self.state.set_last_check(event_cfg.event_id)
        self._last_successful_check = datetime.now(timezone.utc)
        self.state.set_last_successful_check()
        return _EventCheckOutcome(
            needs_slow_retry=needs_slow_retry,
            had_response=True,
            result=probe_result,
        )

    def _per_event_scheduler_enabled(self) -> bool:
        return bool(self.config.browser_per_event_scheduler_enabled)

    def _ensure_event_schedule(self, now: datetime):
        configured_ids = {event.event_id for event in self.config.events}
        for event_id in list(self._event_schedule):
            if event_id not in configured_ids:
                self._event_schedule.pop(event_id, None)
        for event in self.config.events:
            self._event_schedule.setdefault(event.event_id, now)

    def _select_due_event(self, now: datetime) -> tuple[EventConfig | None, float]:
        if not self.config.events:
            return None, 1.0
        self._ensure_event_schedule(now)

        gap_target = self._next_event_check_not_before
        if gap_target is not None and now < gap_target:
            return None, max(0.0, (gap_target - now).total_seconds())

        event_by_id = {event.event_id: event for event in self.config.events}
        earliest_due = min(self._event_schedule[event.event_id] for event in self.config.events)
        if earliest_due > now:
            return None, max(0.0, (earliest_due - now).total_seconds())

        due_events = [
            event_by_id[event_id]
            for event_id, due_at in self._event_schedule.items()
            if event_id in event_by_id and due_at <= earliest_due
        ]
        return self._weighted_choice(due_events), 0.0

    def _weighted_choice(self, events: list[EventConfig]) -> EventConfig | None:
        if not events:
            return None
        total = sum(self._event_weight(event.event_id) for event in events)
        if total <= 0:
            return events[0]
        pick = self._rand.uniform(0.0, total)
        cumulative = 0.0
        for event in events:
            cumulative += self._event_weight(event.event_id)
            if pick <= cumulative:
                return event
        return events[-1]

    def _reschedule_event(self, event_id: str, checked_at: datetime, result: Any | None):
        interval = self._event_interval_seconds(event_id)
        if self._result_needs_event_cooldown(result):
            interval = max(interval, self._blocked_event_cooldown_seconds(result, checked_at))
        self._event_schedule[event_id] = checked_at + timedelta(seconds=interval)

        gap = max(0, int(self.config.browser_per_event_min_gap_between_checks_seconds))
        if gap > 0:
            self._next_event_check_not_before = checked_at + timedelta(seconds=gap)
        else:
            self._next_event_check_not_before = checked_at

    def _event_interval_seconds(self, event_id: str) -> float:
        low = float(self.config.browser_per_event_poll_min_seconds)
        high = float(self.config.browser_per_event_poll_max_seconds)
        if high <= low:
            return max(1.0, low)

        weight = self._event_weight(event_id)
        if weight > 1.0 and hasattr(self._rand, "betavariate"):
            fraction = self._rand.betavariate(1.0, weight)
        elif 0.0 < weight < 1.0 and hasattr(self._rand, "betavariate"):
            fraction = self._rand.betavariate(1.0 / weight, 1.0)
        else:
            fraction = self._rand.uniform(0.0, 1.0)
        return max(1.0, low + (high - low) * fraction)

    def _event_weight(self, event_id: str) -> float:
        weights = self.config.browser_event_weights or {}
        try:
            return max(0.01, float(weights.get(event_id, 1.0)))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _result_needs_event_cooldown(result: Any | None) -> bool:
        if result is None:
            return True
        return bool(getattr(result, "blocked", False) or getattr(result, "challenge_detected", False))

    def _blocked_event_cooldown_seconds(self, result: Any | None, now: datetime) -> float:
        base = max(
            float(self.config.browser_challenge_retry_seconds),
            float(self.config.browser_per_event_poll_max_seconds),
        )
        retry_after = self._result_retry_after_seconds(result)
        if retry_after is not None:
            base = max(base, float(retry_after))

        challenge_until = self.state.get_challenge_cooldown_until()
        if challenge_until is not None and challenge_until > now:
            base = max(base, (challenge_until - now).total_seconds())

        return max(1.0, base * self._rand.uniform(0.85, 1.15))

    @staticmethod
    def _result_retry_after_seconds(result: Any | None) -> int | None:
        if result is None:
            return None
        raw = getattr(result, "raw_indicators", None)
        if not isinstance(raw, dict):
            return None
        retry_after = raw.get("retry_after_seconds")
        if isinstance(retry_after, int) and retry_after > 0:
            return retry_after
        return None

    def _per_event_next_sleep(self, now: datetime) -> float:
        self._ensure_event_schedule(now)
        if not self._event_schedule:
            return 1.0
        earliest_due = min(self._event_schedule.values())
        target = earliest_due
        if self._next_event_check_not_before is not None and target < self._next_event_check_not_before:
            target = self._next_event_check_not_before
        return max(1.0, (target - now).total_seconds())

    def _check_event_poll_staleness(self, now: datetime) -> bool:
        """Alert and self-heal when any configured event stops receiving checks."""
        threshold_seconds = int(self.config.alerts_event_check_stale_seconds)
        stale_detected = False

        # Staleness only counts time the monitor has actually been running this session
        # (capped by the uptime anchor) — so a restart/wake after the Mac slept for 40
        # min isn't instantly "stale". The anchor advances on a detected sleep too.
        uptime_seconds = (now - self._uptime_anchor).total_seconds()
        for event_cfg in self.config.events:
            event_id = event_cfg.event_id
            last_check = self.state.get_last_check(event_id)
            if last_check is None:
                if uptime_seconds <= threshold_seconds:
                    continue
                age_seconds = int(uptime_seconds)
            else:
                age_seconds = int(min((now - last_check).total_seconds(), uptime_seconds))

            planned_due = self._event_schedule.get(event_id)
            if (
                self._per_event_scheduler_enabled()
                and planned_due is not None
                and now < planned_due + timedelta(seconds=threshold_seconds)
            ):
                continue

            if age_seconds > threshold_seconds:
                stale_detected = True
                logger.error(
                    "[%s] poll staleness detected: last_check_age=%ss threshold=%ss",
                    event_cfg.name,
                    age_seconds,
                    threshold_seconds,
                )
                if event_id not in self._stale_event_alerted:
                    self._stale_event_alerted.add(event_id)
                    self.state.record_check_outcome("stale", now)
                    # Degraded, but don't ping yet — self-healing (recycle below) gets
                    # a chance first. The manual-action escalation pings only if this
                    # keeps failing past the grace window.
                    logger.error("[%s] poll staleness recorded as degraded", event_cfg.name)
                continue

            if event_id in self._stale_event_alerted:
                logger.info(
                    "[%s] poll staleness recovered: last_check_age=%ss threshold=%ss",
                    event_cfg.name,
                    age_seconds,
                    threshold_seconds,
                )
                self._stale_event_alerted.discard(event_id)
                self.state.clear_last_operational_alert(event_id)

        if stale_detected:
            self._maybe_recycle_browser(
                now=now,
                reason="event poll staleness detected",
            )

        return stale_detected

    def _incident_fingerprint(
        self,
        *,
        alert_code: str,
        event_id: str,
        reason_code: str,
        blocked: bool | None = None,
        challenge: bool | None = None,
    ) -> str:
        blocked_token = "na" if blocked is None else str(bool(blocked)).lower()
        challenge_token = "na" if challenge is None else str(bool(challenge)).lower()
        return f"{alert_code}:{event_id}:{reason_code}:{blocked_token}:{challenge_token}"

    def _should_emit_operational_alert(
        self,
        *,
        event_id: str,
        fingerprint: str,
        now: datetime,
    ) -> bool:
        cooldown = max(0, int(self.config.alerts_operational_state_cooldown_seconds))
        last_fingerprint = self.state.get_last_operational_alert_fingerprint(event_id)
        last_alert_at = self.state.get_last_operational_alert_at(event_id)
        if (
            cooldown > 0
            and last_fingerprint == fingerprint
            and last_alert_at is not None
            and (now - last_alert_at).total_seconds() < cooldown
        ):
            return False
        self.state.set_last_operational_alert(event_id, fingerprint=fingerprint, dt=now)
        return True

    def _handle_probe_result(self, event_cfg: EventConfig, result, now: datetime | None = None):
        event_id = event_cfg.event_id
        now = now or datetime.now(timezone.utc)

        # Blindness/outage tracking: an explicit block (401/403/429 → result.blocked),
        # a bot challenge, or a no-signal probe on an UNHEALTHY HTTP response. A page
        # that loaded fine (2xx/3xx) but lists no tickets is just "no inventory right
        # now" — NOT blind — so empty events never trip the outage/manual-action ping.
        status = (
            result.raw_indicators.get("response_status")
            if isinstance(result.raw_indicators, dict)
            else None
        )
        http_unhealthy = isinstance(status, int) and not (200 <= status < 400)
        no_signal = result.signal_type == ProbeSignalType.NONE and http_unhealthy
        blind = result.blocked or result.challenge_detected or no_signal

        # Per-cycle uptime tally: a blind check (blocked/challenge/no-signal) is bad,
        # anything else is a clean scan. Drives healthy-vs-impaired for the Uptime tab.
        if blind:
            self._cycle_bad_checks += 1
        else:
            self._cycle_healthy_checks += 1

        # Effectiveness metrics: one outcome per check, for the live GUI health panel.
        if result.challenge_detected:
            outcome = "challenge"
        elif blind:
            outcome = "blocked"
        else:
            outcome = "healthy"
        self.state.record_check_outcome(outcome, now)

        # Challenge circuit-breaker: back fully off on captcha/challenge or 429 so we
        # stop feeding the block; clear it the moment a check comes back clean.
        self._update_challenge_cooldown(result=result, status=status, now=now)

        # Any sign of life → force max-speed polling (reset adaptive backoff) so we catch
        # the rest of a live ~2-min drop window at the floor cadence instead of a backed-off
        # one. Blind checks are excluded — those should keep backing off.
        if not blind and self._has_activity_signal(result):
            self._cadence_backoff = 1.0

        if blind:
            count = self.state.increment_consecutive_blocked(event_id)
            logger.warning("[%s] blind check #%d", event_cfg.name, count)
            if self._in_startup_warmup(now):
                # Ticketmaster blocks heavily right after launch/recycle. Count the
                # blind checks (so the threshold is met promptly once warmup ends) but
                # don't flag outage/degraded yet — let the monitor break in first.
                logger.info("[%s] blind during startup warmup — not flagging outage", event_cfg.name)
            elif count >= self.config.browser_challenge_threshold:
                if not self.state.get_in_outage_state(event_id):
                    message = f"Event checks are blind ({count} consecutive)."
                    incident = self._incident_fingerprint(
                        alert_code="monitor_outage",
                        event_id=event_id,
                        reason_code="blind_outage_threshold",
                        blocked=result.blocked,
                        challenge=result.challenge_detected,
                    )
                    sent = False
                    if self._should_emit_operational_alert(
                        event_id=event_id,
                        fingerprint=incident,
                        now=now,
                    ):
                        sent = self.notifier.send_monitor_blocked(
                            event_cfg.name,
                            message,
                            context={
                                "event_name": event_cfg.name,
                                "event_id": event_id,
                                "signal": result.signal_type.value,
                                "blocked": result.blocked,
                                "challenge": result.challenge_detected,
                                "consecutive": count,
                                "reason_code": "blind_outage_threshold",
                            },
                            auto_fix_planned="browser_recycle_now",
                            manual_required=False,
                        )
                    self.state.set_in_outage_state(event_id, True)
                    logger.error("[%s] entering outage state (alert_sent=%s)", event_cfg.name, sent)
                    self._maybe_recycle_browser(
                        now=now,
                        reason=f"blind/outage threshold reached for {event_cfg.name}",
                    )
                # Escalating flush: after a prolonged block, shed a poisoned DataDome
                # token (login cookies preserved) so the next probe gets a fresh one.
                threshold = max(1, int(self.config.browser_challenge_threshold))
                flush_at = threshold * PROLONGED_BLOCK_COOKIE_FLUSH_MULTIPLIER
                if count >= flush_at and (count - flush_at) % threshold == 0:
                    try:
                        cleared = self.probe.clear_block_cookies()
                        logger.warning(
                            "[%s] prolonged block (#%d) — flushed %d block cookie(s)",
                            event_cfg.name,
                            count,
                            cleared,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug("clear_block_cookies failed: %s", exc)
                self._maybe_auto_reauth(event_cfg=event_cfg, result=result, now=now)
        else:
            self.state.reset_consecutive_blocked(event_id)
            self.state.set_last_probe_success_at(event_id, now)
            if self.state.get_in_outage_state(event_id):
                sent = self.notifier.send_monitor_recovered(
                    event_cfg.name,
                    "Browser probe recovered and signals are healthy.",
                )
                self.state.set_in_outage_state(event_id, False)
                self.state.clear_last_operational_alert(event_id)
                logger.info("[%s] outage recovered (alert_sent=%s)", event_cfg.name, sent)

        # Availability alerting + dedupe/cooldown.
        decision = self.detector.evaluate(event_id, result, self.state, now=now)
        if result.available:
            listing_groups = (
                result.raw_indicators.get("listing_groups")
                if isinstance(result.raw_indicators, dict)
                else None
            )
            preferences = self._ticket_preferences()
            match = DiscordNotifier._ticket_match_status(listing_groups, preferences=preferences)
            is_bingo = match.get("preview_status") == "BINGO"

            # @-mention policy: a BINGO always pings. A non-BINGO detection pings only
            # when the "alert on all tickets" toggle is on. The webhook message + local
            # History entry are posted for EVERY detection regardless — only the ping is
            # gated. The mention burst (which governs ping cadence) is therefore armed
            # only when a mention is actually allowed.
            mention_allowed = is_bingo or self.config.alerts_non_bingo_enabled
            if mention_allowed:
                # A genuinely new listing (new signature) starts a fresh mention episode.
                # The same listing reappearing does NOT restart the burst — that is what
                # caused hours of re-pings on lingering/expensive listings.
                if decision.reason == "signature_changed":
                    self.state.reset_mention_burst(event_id)
                self._start_mention_burst_if_needed(event_id, now)
                mention_due = self._should_send_mention_burst(event_id, now)
            else:
                mention_due = False

            self.state.set_last_available_at(event_id, now)
            self.state.set_last_availability_signature(event_id, decision.signature)
            if decision.should_alert:
                sent = self.notifier.send_ticket_available(
                    event_name=event_cfg.name,
                    event_date=event_cfg.date,
                    event_url=event_cfg.url,
                    signal_type=result.signal_type.value,
                    signal_confidence=result.signal_confidence,
                    price_summary=result.price_summary,
                    section_summary=result.section_summary,
                    reason=decision.reason,
                    listing_summary=result.listing_summary,
                    listing_groups=listing_groups,
                    mention=mention_due,
                    preferences=preferences,
                )
                if sent:
                    if mention_due:
                        self._record_mention_burst_sent(event_id, now)
                    self.state.set_last_alert_at(event_id, now)
                    logger.info(
                        "[%s] ticket alert sent (%s, mention=%s)",
                        event_cfg.name,
                        decision.reason,
                        mention_due,
                    )
                else:
                    logger.error("[%s] ticket alert failed to send", event_cfg.name)
            elif mention_due:
                sent = self.notifier.send_ticket_available(
                    event_name=event_cfg.name,
                    event_date=event_cfg.date,
                    event_url=event_cfg.url,
                    signal_type=result.signal_type.value,
                    signal_confidence=result.signal_confidence,
                    price_summary=result.price_summary,
                    section_summary=result.section_summary,
                    reason="attention_burst",
                    listing_summary=result.listing_summary,
                    listing_groups=listing_groups,
                    mention=True,
                    preferences=preferences,
                )
                if sent:
                    self._record_mention_burst_sent(event_id, now)
                    logger.info("[%s] ticket alert sent (attention_burst)", event_cfg.name)
                else:
                    logger.error("[%s] ticket alert failed to send (attention_burst)", event_cfg.name)
        else:
            # Listing gone. Keep the last signature so the SAME listing reappearing
            # is treated as a duplicate (no fresh ping). A genuinely new listing has
            # a new signature and starts its own episode on arrival.
            pass

    def _ticket_preferences(self):
        return getattr(
            self.config,
            "bingo_configs",
            getattr(self.config, "preferences", None),
        )

    def _start_mention_burst_if_needed(self, event_id: str, now: datetime):
        started_at = self.state.get_mention_burst_started_at(event_id)
        if started_at is not None:
            elapsed = (now - started_at).total_seconds()
            if elapsed < BURST_HARD_FAILSAFE_SECONDS:
                return
            logger.warning(
                "[%s] resetting stale mention burst state after %.1fs",
                event_id,
                elapsed,
            )
            self.state.reset_mention_burst(event_id)
        self.state.set_mention_burst_started_at(event_id, now)
        self.state.set_mention_burst_last_mention_at(event_id, None)
        self.state.set_mention_burst_sent_count(event_id, 0)
        self.state.set_mention_burst_completed_for_episode(event_id, False)

    def _should_send_mention_burst(self, event_id: str, now: datetime) -> bool:
        if self.state.get_mention_burst_completed_for_episode(event_id):
            return False

        started_at = self.state.get_mention_burst_started_at(event_id)
        if started_at is None:
            return True

        elapsed = (now - started_at).total_seconds()
        # Episode is over once we pass the phase-2 window (or the hard failsafe).
        if elapsed >= BURST_PHASE2_SECONDS or elapsed >= BURST_HARD_FAILSAFE_SECONDS:
            self.state.set_mention_burst_completed_for_episode(event_id, True)
            return False

        # Phase 1: nonstop — ping on every qualifying poll.
        if elapsed < BURST_PHASE1_SECONDS:
            return True

        # Phase 2: throttle to at most once per minute.
        last_mention_at = self.state.get_mention_burst_last_mention_at(event_id)
        if last_mention_at is None:
            return True
        return (now - last_mention_at).total_seconds() >= BURST_PHASE2_INTERVAL_SECONDS

    def _record_mention_burst_sent(self, event_id: str, now: datetime):
        self.state.set_mention_burst_last_mention_at(event_id, now)
        self.state.increment_mention_burst_sent_count(event_id)

    def _maybe_auto_reauth(self, event_cfg: EventConfig, result, now: datetime):
        if self.config.browser_session_mode == "cdp_attach":
            return
        if not self.config.auth_auto_login_enabled:
            return
        if self.session_autofixer is None:
            return
        if not self._is_auth_like_failure(result):
            return

        pause_until = self.state.get_auth_pause_until()
        if pause_until is not None and now < pause_until:
            logger.warning(
                "[%s] auto re-auth paused until %s",
                event_cfg.name,
                pause_until.isoformat(),
            )
            return

        attempts_last_hour = self.state.get_auth_reauth_attempts_recent(window_seconds=3600, now=now)
        max_attempts = self.config.auth_max_auto_login_attempts_per_hour
        if attempts_last_hour >= max_attempts:
            pause_target = now + timedelta(seconds=self.config.auth_auto_login_cooldown_seconds)
            self.state.set_auth_pause_until(pause_target)
            # Degraded (auth paused). The manual-action escalation will ping if the
            # monitor stays unable to recover past the grace window.
            logger.error(
                "[%s] auto re-auth entering cooldown until %s (%d attempts/hour limit reached)",
                event_cfg.name,
                pause_target.isoformat(),
                max_attempts,
            )
            return

        # Avoid nested Playwright sync loops while re-auth spins up its own browser.
        try:
            self.probe.close()
        except Exception:
            pass

        self.state.record_auth_reauth_attempt(now)
        reauth = self.session_autofixer.attempt_reauth(
            event_url=event_cfg.url,
            storage_state_path=self.config.browser_storage_state_path,
            timeout_seconds=self.config.browser_navigation_timeout_seconds,
            session_mode=self.config.browser_session_mode,
            user_data_dir=self.config.browser_user_data_dir,
            channel=self.config.browser_channel,
            # Background re-auth should stay silent; manual login uses monitorctl reauth.
            headless=True,
            verify_event_urls=[ev.url for ev in self.config.events],
        )
        if reauth.success:
            self.state.set_last_auto_fix_at(now)
            self.state.set_auth_pause_until(None)
            self.state.clear_last_operational_alert(event_cfg.event_id)
            self.notifier.send_auto_fix_action(
                action="ticketmaster_reauth_success",
                reason=f"{event_cfg.name}: {reauth.reason}",
                context={"event_name": event_cfg.name, "event_id": event_cfg.event_id},
                auto_fix_planned="probe_reload_after_reauth",
            )
            reloaded = self._reload_probe_after_reauth(now=now, event_name=event_cfg.name)
            logger.info("[%s] auto re-auth succeeded (probe_reloaded=%s)", event_cfg.name, reloaded)
            return

        attempts_after = self.state.get_auth_reauth_attempts_recent(window_seconds=3600, now=now)
        self.notifier.send_auto_fix_action(
            action="ticketmaster_reauth_failed",
            reason=f"{event_cfg.name}: {reauth.reason}",
            context={"event_name": event_cfg.name, "event_id": event_cfg.event_id},
            auto_fix_planned="retry_auto_reauth" if attempts_after < max_attempts else "reauth_paused",
        )
        self._reload_probe_after_reauth(now=now, event_name=event_cfg.name)
        logger.warning(
            "[%s] auto re-auth failed: %s (attempts=%d/%d in last hour)",
            event_cfg.name,
            reauth.reason,
            attempts_after,
            max_attempts,
        )

        if reauth.reason == "challenge_detected":
            # Challenge during auto re-login — self-healing can't clear this on its own.
            # Recorded as degraded; the manual-action escalation pings if it persists.
            logger.error(
                "[%s] challenge detected during auto re-login (degraded)",
                event_cfg.name,
            )

        if attempts_after >= max_attempts:
            pause_target = now + timedelta(seconds=self.config.auth_auto_login_cooldown_seconds)
            self.state.set_auth_pause_until(pause_target)
            logger.error(
                "[%s] auto re-auth paused until %s after repeated failures",
                event_cfg.name,
                pause_target.isoformat(),
            )

    @staticmethod
    def _has_activity_signal(result) -> bool:
        """Any sign of life on the page — availability, a positive probe signal, or an
        offer/CTA DOM hit — used to snap the cadence back to max speed for a live drop."""
        if getattr(result, "available", False):
            return True
        if result.signal_type != ProbeSignalType.NONE:
            return True
        indicators = result.raw_indicators if isinstance(result.raw_indicators, dict) else {}
        dom = indicators.get("dom_signals") or []
        # Only STRONG inventory signals — generic Buy/Find CTAs are noisy and present
        # even while offsale (see BrowserProbe._is_available), so they don't count.
        return any(sig in {"offer_card_ui", "tickets_available_text"} for sig in dom)

    @staticmethod
    def _is_auth_like_failure(result) -> bool:
        if not result.blocked:
            return False
        if result.challenge_detected:
            return False

        indicators = result.raw_indicators if isinstance(result.raw_indicators, dict) else {}
        status = indicators.get("response_status")
        if status == 401:
            return True
        if status in {403, 429}:
            return False

        if result.signal_type != ProbeSignalType.NONE:
            return False

        page_title = str(indicators.get("page_title", "")).lower()
        if any(token in page_title for token in ("sign in", "log in", "login")):
            return True

        return False

    def _reload_probe_after_reauth(self, now: datetime, event_name: str) -> bool:
        try:
            self.probe.close()
            self.probe.start()
            return True
        except BrowserProbeError as exc:
            prior_type = self.state.get_last_error_type()
            manual_required = prior_type == "reauth_probe_reload_failed"
            self.state.set_last_error("reauth_probe_reload_failed", str(exc))
            self._maybe_send_error_alert(
                f"Probe reload failed after auto re-auth for {event_name}: {exc}",
                context={
                    "event_name": event_name,
                    "reason_code": "reauth_probe_reload_failed",
                },
                manual_required=manual_required,
                next_steps=REAUTH_MANUAL_STEPS if manual_required else None,
            )
            return False

    # ---- Manual-action escalation ----

    def _monitor_degraded_reason(self, now: datetime) -> str | None:
        """Return WHY the monitor is non-healthy (or None), for the GUI banner.

        ``logged_out`` is the ONLY human-actionable one (needs re-login). ``outage``
        (blocked) and ``stale`` self-heal and never ping — they're informational only.
        ``logged_out`` takes precedence because it's the one the user must act on.
        """
        if self.state.get_session_logged_out():
            return "logged_out"
        if any(self.state.get_in_outage_state(ev.event_id) for ev in self.config.events):
            return "outage"
        if self._stale_event_alerted:
            return "stale"
        pause_until = self.state.get_auth_pause_until()
        if pause_until is not None and now < pause_until:
            return "auth_paused"
        return None

    def _is_monitor_degraded(self, now: datetime) -> bool:
        return self._monitor_degraded_reason(now) is not None

    @staticmethod
    def _reason_needs_manual_action(reason: str | None) -> bool:
        """Only a dead session needs the human; blocks/stale self-heal silently."""
        return reason in {"logged_out", "auth_paused"}

    def _manual_action_summary(self, reason: str | None) -> tuple[str, list[str]]:
        """Plain-English description + next steps. Only login-actionable reasons reach
        here now (blocks/stale never ping)."""
        return (
            "You've been signed out of Ticketmaster, so the monitor can't use your "
            "account (needed to grab tickets the instant they drop). Please log back "
            "in — everything else recovers on its own.",
            LOGIN_MANUAL_STEPS,
        )

    def _evaluate_manual_action_escalation(self, now: datetime | None = None):
        """Send a single manual-action ping only after the monitor has stayed degraded
        past the grace window — giving self-healing time to work first."""
        now = now or datetime.now(timezone.utc)

        reason = self._monitor_degraded_reason(now)
        if reason is None:
            if self.state.get_attention_since() is not None or self.state.get_attention_alerted():
                logger.info("Monitor recovered; clearing manual-action escalation state")
                self.state.clear_attention()
            self.state.set_degraded_state(False)
            return

        # GUI banner mirrors any non-healthy reason; keep the first-seen time stable.
        degraded_since = self.state.get_degraded_state().get("since") or now
        self.state.set_degraded_state(True, reason=reason, since=degraded_since)

        # Blocks/stale self-heal — show "recovering" in the GUI but never ping the user.
        if not self._reason_needs_manual_action(reason):
            if self.state.get_attention_since() is not None or self.state.get_attention_alerted():
                self.state.clear_attention()
            return

        since = self.state.get_attention_since()
        if since is None:
            self.state.set_attention_since(now)
            since = now

        # Stop only once the alert is actually DELIVERED (or we've exhausted retries).
        # A send that Discord rejects (rate-limit / error / network) leaves us free to
        # retry next cycle instead of falsely marking the episode as alerted.
        if self.state.get_attention_alert_delivered():
            return
        if self.state.get_attention_alert_attempts() >= MAX_ATTENTION_ALERT_ATTEMPTS:
            return

        delay = max(0, int(self.config.alerts_manual_action_after_seconds))
        degraded_seconds = (now - since).total_seconds()
        if degraded_seconds < delay:
            return

        message, next_steps = self._manual_action_summary(reason)
        minutes = int(degraded_seconds // 60)
        delivered = self.notifier.send_critical_attention(
            message,
            context={"degraded_for": f"{minutes} min"},
            next_steps=next_steps,
        )
        attempts = self.state.get_attention_alert_attempts() + 1
        self.state.set_attention_alert_attempts(attempts)
        self.state.set_attention_alerted(True)
        self.state.set_attention_alert_delivered(bool(delivered))
        if delivered:
            logger.critical(
                "Manual-action ping delivered after %d min degraded: %s", minutes, message
            )
        else:
            logger.error(
                "Manual-action ping FAILED to deliver (attempt %d/%d) after %d min degraded: %s",
                attempts,
                MAX_ATTENTION_ALERT_ATTEMPTS,
                minutes,
                message,
            )

    # ---- State/metrics ----

    def _maybe_send_heartbeat(self):
        """Health-aware heartbeat: silent when healthy.

        We no longer post a periodic "I'm alive" embed to Discord \u2014 that was pure
        noise. Instead we log a health summary periodically; if the monitor is
        actually stuck, the manual-action escalation pings on its own. So silence
        means healthy, not dead.
        """
        now = datetime.now(timezone.utc)
        last = self.state.get_last_heartbeat_at()
        if last is not None:
            elapsed = (now - last).total_seconds() / 3600
            if elapsed < self.config.alerts_operational_heartbeat_hours:
                return

        monitor_started = self.state.get_monitor_start_time() or self.start_time
        uptime_hours = (now - monitor_started).total_seconds() / 3600

        stale_threshold = int(self.config.alerts_event_check_stale_seconds)
        statuses = []
        for event_cfg in self.config.events:
            last_check = self.state.get_last_check(event_cfg.event_id)
            in_outage = self.state.get_in_outage_state(event_cfg.event_id)
            if last_check is None:
                status = "not-yet-checked"
            elif in_outage:
                status = "outage"
            elif int((now - last_check).total_seconds()) > stale_threshold:
                status = "stale"
            else:
                status = "active"
            statuses.append(f"{event_cfg.name}={status}")

        logger.info(
            "Heartbeat (log-only): uptime=%.1fh degraded=%s %s",
            uptime_hours,
            self._is_monitor_degraded(now),
            " ".join(statuses),
        )
        self.state.set_last_heartbeat_at(now)

    def _maybe_check_session_health(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        interval = self._session_health_due_interval()
        last = self.state.get_last_session_health_check_at()
        if last is not None and (now - last).total_seconds() < interval:
            return

        cooldown_until = self.state.get_challenge_cooldown_until()
        if cooldown_until is not None and now < cooldown_until:
            logger.debug(
                "Skipping session health check during challenge cooldown until %s",
                cooldown_until.isoformat(),
            )
            return

        pending = self.state.get_session_logout_pending_count()
        if (
            self._last_cycle_blocked
            and not self.state.get_session_logged_out()
            and pending <= 0
        ):
            logger.debug("Deferring session health check after a blocked event cycle")
            return

        url = self.config.auth_session_health_check_url
        logger.debug("Running session health check against %s", url)
        try:
            result = self.probe.check_session_health(url)
        except Exception as exc:
            logger.warning("Session health check probe failed: %s", exc)
            self.notifier.send_error(
                f"Session health check could not complete: {exc}",
                context={"reason_code": "session_health_probe_error", "url": url},
            )
            self.state.set_last_session_health_check_at(now)
            return

        self.state.set_last_session_health_check_at(now)

        if result.get("healthy"):
            logger.debug("Session health check passed (status=%s)", result.get("status"))
            self._session_health_fail_streak = 0
            self.state.set_session_logout_pending_count(0)
            self.state.set_last_session_health_reason("ok")
            if self.state.get_session_logged_out():
                logger.info("Session health recovered; clearing logged-out state")
                self.state.set_session_logged_out(False)
            return

        reason = result.get("reason", "unknown")
        status = result.get("status")
        challenge = bool(result.get("challenge", False))
        definitive = bool(result.get("definitive_logged_out", False))
        self.state.set_last_session_health_reason(str(reason))

        if challenge or reason in {"challenge_detected", "http_401", "http_403"}:
            self._session_health_fail_streak += 1
            outcome = "challenge" if challenge or reason == "challenge_detected" else "blocked"
            self.state.record_check_outcome(outcome, now)
            logger.warning(
                "Session health check blocked (self-heals): reason=%s status=%s challenge=%s streak=%d",
                reason,
                status,
                challenge,
                self._session_health_fail_streak,
            )
            return

        if not definitive:
            self._session_health_fail_streak += 1
            self.state.record_check_outcome("blocked", now)
            logger.warning(
                "Session health check failed without definitive logout: reason=%s status=%s streak=%d",
                reason,
                status,
                self._session_health_fail_streak,
            )
            return

        self._session_health_fail_streak = 0
        if self.state.get_session_logged_out():
            self.state.set_session_logout_pending_count(0)
            logger.debug("Session health still logged out (reason=%s)", reason)
            return

        pending = self.state.get_session_logout_pending_count() + 1
        required = max(1, int(self.config.auth_session_logout_confirmations_required))
        if pending < required:
            self.state.set_session_logout_pending_count(pending)
            logger.warning(
                "Session health: possible sign-out (reason=%s, confirmation %d/%d) — confirming",
                reason,
                pending,
                required,
            )
            return

        self.state.set_session_logout_pending_count(0)
        logger.error("Session health: confirmed logged out (reason=%s) — will ping for re-login", reason)
        self.state.set_session_logged_out(True)

    def _session_health_due_interval(self) -> float:
        if (
            not self.state.get_session_logged_out()
            and self.state.get_session_logout_pending_count() <= 0
            and self._session_health_fail_streak <= 0
        ):
            return float(self.config.auth_session_health_check_interval_seconds)

        base = max(30, int(self.config.auth_session_recheck_base_seconds))
        cap = max(base, int(self.config.auth_session_recheck_max_seconds))
        exponent = max(0, self._session_health_fail_streak - 1)
        interval = min(base * (2 ** exponent), cap)
        return max(1.0, float(interval) * self._rand.uniform(0.85, 1.15))

    def _should_skip_cycle_for_challenge_cooldown(self, now: datetime) -> bool:
        until = self.state.get_challenge_cooldown_until()
        return until is not None and now < until and not self._in_startup_warmup(now)

    # ---- Sleep/backoff helpers ----

    def _next_sleep(self, *, blocked: bool) -> float:
        """Adaptive cadence: a randomized floor, scaled by a back-off multiplier that
        grows when the monitor is blocked/challenged and decays back toward the floor
        when checks are healthy. Bounded by ``adaptive_max_seconds``.

        When adaptive back-off is disabled, this reproduces the original behavior:
        a fixed ``challenge_retry_seconds`` on a blocked cycle, else the random floor.
        """
        floor = self._normal_loop_sleep()
        if not self.config.browser_adaptive_backoff_enabled:
            self._cadence_backoff = 1.0
            if blocked:
                return float(self.config.browser_challenge_retry_seconds)
            return floor

        cap = float(self.config.browser_adaptive_max_seconds)
        if blocked:
            self._cadence_backoff = self._cadence_backoff * float(
                self.config.browser_adaptive_backoff_multiplier
            )
        else:
            self._cadence_backoff = max(
                1.0, self._cadence_backoff * float(self.config.browser_adaptive_recover_factor)
            )
        # Clamp the multiplier so floor*multiplier never exceeds the cap.
        max_multiplier = max(1.0, cap / max(1.0, floor))
        self._cadence_backoff = min(self._cadence_backoff, max_multiplier)
        return min(floor * self._cadence_backoff, cap)

    def _normal_loop_sleep(self) -> float:
        if self.config.browser_poll_min_seconds > 0 and self.config.browser_poll_max_seconds > 0:
            low = float(min(self.config.browser_poll_min_seconds, self.config.browser_poll_max_seconds))
            high = float(max(self.config.browser_poll_min_seconds, self.config.browser_poll_max_seconds))
            return max(1.0, float(self._rand.uniform(low, high)))

        base = float(self.config.browser_poll_interval_seconds)
        jitter = float(
            self._rand.uniform(
                -self.config.browser_poll_jitter_seconds,
                self.config.browser_poll_jitter_seconds,
            )
        )
        stagger_penalty = max(0, len(self.config.events) - 1) * self.config.event_stagger_seconds
        sleep_time = base + jitter - stagger_penalty
        return max(1.0, sleep_time)

    def _runtime_error_backoff(self) -> float:
        base = 10.0
        exponent = max(0, self._consecutive_runtime_errors - 1)
        candidate = base * (self.config.backoff_multiplier ** exponent)
        return float(min(candidate, self.config.max_backoff_seconds))

    def _in_startup_warmup(self, now: datetime) -> bool:
        """True during the warmup window after launch or a browser recycle, when the
        Ticketmaster block flurry is expected and shouldn't be treated as an outage."""
        grace = max(0, int(self.config.browser_startup_grace_seconds))
        if grace <= 0:
            return False
        anchor = self.start_time
        if self._last_browser_recycle_at is not None and self._last_browser_recycle_at > anchor:
            anchor = self._last_browser_recycle_at
        return (now - anchor).total_seconds() < grace

    def _update_challenge_cooldown(self, *, result, status, now: datetime):
        """Grow/clear the challenge cooldown based on the latest probe.

        Captcha/challenge or HTTP 429 -> exponential cooldown, then tiered quiet
        periods for persistent blocks. A clean check resets it.
        """
        is_challenge = bool(result.challenge_detected) or status == 429
        if not is_challenge:
            if self._consecutive_challenges or self.state.get_challenge_cooldown_until() is not None:
                self._consecutive_challenges = 0
                self.state.set_challenge_cooldown_until(None)
            return

        self._consecutive_challenges += 1
        base = max(1, int(self.config.browser_challenge_cooldown_base_seconds))
        cap = max(base, int(self.config.browser_challenge_cooldown_max_seconds))
        escalate_after = max(1, int(self.config.browser_challenge_cooldown_escalate_after))
        tier_every = max(1, int(self.config.browser_challenge_cooldown_tier_every))
        tiers = [max(1, int(v)) for v in self.config.browser_challenge_cooldown_tiers_seconds]
        if not tiers:
            tiers = [cap]
        # During startup/recycle warmup, keep the cooldown at its base (no exponential
        # growth) so the monitor keeps trying to break in rather than backing off for
        # tens of minutes on the expected initial block flurry.
        if self._in_startup_warmup(now):
            cooldown = base
        elif self._consecutive_challenges >= escalate_after:
            idx = min(
                (self._consecutive_challenges - escalate_after) // tier_every,
                len(tiers) - 1,
            )
            cooldown = tiers[idx]
        else:
            cooldown = min(base * (2 ** (self._consecutive_challenges - 1)), cap)
        cooldown = max(1, int(cooldown * self._rand.uniform(0.85, 1.15)))

        retry_after = None
        if isinstance(result.raw_indicators, dict):
            retry_after = result.raw_indicators.get("retry_after_seconds")
        if isinstance(retry_after, int) and retry_after > 0:
            retry_after_cap = max(cap, tiers[-1])
            cooldown = max(cooldown, min(retry_after, retry_after_cap))

        self.state.set_challenge_cooldown_until(now + timedelta(seconds=cooldown))
        logger.warning(
            "Challenge circuit-breaker: cooling down %ds (consecutive=%d, retry_after=%s)",
            cooldown,
            self._consecutive_challenges,
            retry_after,
        )

    def _apply_challenge_cooldown(self, sleep_time: float, now: datetime) -> float:
        """Extend this cycle's sleep to cover an active challenge cooldown, but clamp a
        single sleep so the loop keeps re-evaluating the manual-action escalation (the
        cooldown persists in state and resumes on the next cycle)."""
        until = self.state.get_challenge_cooldown_until()
        if until is not None and now < until:
            remaining = (until - now).total_seconds()
            target = min(remaining, CHALLENGE_COOLDOWN_SLEEP_CAP_SECONDS)
            if target > sleep_time:
                logger.info(
                    "Challenge cooldown active: sleeping %.0fs (%.0fs remaining) before next probe",
                    target,
                    remaining,
                )
                return target
        return sleep_time

    def _interruptible_sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            remaining = end - time.monotonic()
            time.sleep(min(remaining, 1.0))

    # ---- Self-heal helpers ----

    def _classify_browser_probe_error(self, exc: Exception) -> str:
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            cause_name = type(cause).__name__.lower()
            cause_text = str(cause).lower()
            if "timeout" in cause_name:
                return "timeout"
            if cause_name == "error" and "closed" in cause_text:
                return "crash"

        text = str(exc).lower()
        if "timeout" in text:
            return "timeout"
        if "browser has been closed" in text or "target page, context or browser has been closed" in text:
            return "crash"
        if "econnreset" in text or "connection reset" in text:
            return "network_reset"
        return "generic"

    def _handle_browser_probe_error(self, exc: BrowserProbeError):
        now = datetime.now(timezone.utc)
        self._record_browser_error(now)
        error_type = self._classify_browser_probe_error(exc)
        window_10 = self._count_browser_errors(window_seconds=self.config.self_heal_browser_restart_window_seconds, now=now)
        window_30 = self._count_browser_errors(window_seconds=self.config.self_heal_process_restart_window_seconds, now=now)

        self._maybe_send_error_alert(
            f"Browser probe error ({error_type}); "
            f"errors in {self.config.self_heal_browser_restart_window_seconds}s={window_10}, "
            f"errors in {self.config.self_heal_process_restart_window_seconds}s={window_30}.",
        )

        if window_10 >= self.config.self_heal_browser_restart_threshold:
            self._maybe_recycle_browser(now=now, reason=f"{window_10} browser errors in self-heal window")

        if window_30 >= self.config.self_heal_process_restart_threshold:
            self.state.record_process_restart_request(now)
            self.notifier.send_auto_fix_action(
                action="process_restart_requested",
                reason=(
                    f"{window_30} browser errors in {self.config.self_heal_process_restart_window_seconds}s; "
                    f"exiting with code {PROCESS_RESTART_EXIT_CODE} so launchd can restart cleanly"
                ),
                auto_fix_planned="launchd_restart_expected",
                context={"reason_code": "browser_error_burst"},
            )
            logger.critical(
                "Requesting process restart via exit code %d after %d browser errors in %ds",
                PROCESS_RESTART_EXIT_CODE,
                window_30,
                self.config.self_heal_process_restart_window_seconds,
            )
            raise SystemExit(PROCESS_RESTART_EXIT_CODE)

    def _record_browser_error(self, now: datetime):
        self._browser_error_times.append(now)
        self._prune_browser_errors(now=now)

    def _count_browser_errors(self, window_seconds: int, now: datetime) -> int:
        self._prune_browser_errors(now=now)
        cutoff = now - timedelta(seconds=window_seconds)
        count = 0
        for ts in self._browser_error_times:
            if ts >= cutoff:
                count += 1
        return count

    def _prune_browser_errors(self, now: datetime):
        max_window = max(
            self.config.self_heal_browser_restart_window_seconds,
            self.config.self_heal_process_restart_window_seconds,
        )
        cutoff = now - timedelta(seconds=max_window)
        while self._browser_error_times and self._browser_error_times[0] < cutoff:
            self._browser_error_times.popleft()

    def _maybe_recycle_browser(self, now: datetime, reason: str):
        if self._last_browser_recycle_at is not None:
            elapsed = (now - self._last_browser_recycle_at).total_seconds()
            # Avoid recycle loops when the browser is hard-failing.
            if elapsed < 30:
                return

        try:
            logger.warning("Recycling browser context: %s", reason)
            self.probe.close()
            self.probe.start()
            self._last_browser_recycle_at = now
            self.state.record_browser_restart(now)
            self.notifier.send_auto_fix_action(
                action="browser_recycled",
                reason=reason,
                context={"reason_code": "browser_recycled"},
                auto_fix_planned="health_recheck",
            )
        except BrowserProbeError as recycle_exc:
            logger.error("Browser recycle failed: %s", recycle_exc)
            self.state.set_last_error("recycle_failed", str(recycle_exc))
            self._maybe_send_error_alert(
                f"Browser recycle failed: {recycle_exc}",
                context={"reason_code": "recycle_failed"},
            )

    def _maybe_send_error_alert(
        self,
        message: str,
        *,
        context: dict | None = None,
        manual_required: bool = False,
        next_steps: list[str] | None = None,
    ):
        now = datetime.now(timezone.utc)
        cooldown = self.config.self_heal_error_alert_cooldown_seconds
        if cooldown > 0 and self._last_error_alert_at is not None:
            elapsed = (now - self._last_error_alert_at).total_seconds()
            if elapsed < cooldown:
                return
        if self.notifier.send_error(
            message,
            context=context,
            manual_required=manual_required,
            next_steps=next_steps,
        ):
            self._last_error_alert_at = now

    def _consume_browser_restart_request_if_any(self):
        if not os.path.exists(BROWSER_RESTART_REQUEST_FILE):
            return
        try:
            os.remove(BROWSER_RESTART_REQUEST_FILE)
        except OSError:
            logger.warning("Browser restart request file exists but could not be removed")
        now = datetime.now(timezone.utc)
        self._maybe_recycle_browser(now=now, reason="manual/browser restart request file")
