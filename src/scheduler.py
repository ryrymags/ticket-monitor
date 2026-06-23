"""Polling scheduler — browser-first monitoring loop with outage detection."""

from __future__ import annotations

import os
import logging
import random
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .browser_probe import BrowserProbe, BrowserProbeError
from .config import EventConfig, MonitorConfig
from .detector import Detector
from .models import ProbeSignalType
from .notifier import DiscordNotifier
from .session_autofix import TicketmasterSessionAutoFixer
from .state import MonitorState

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
REAUTH_MANUAL_STEPS = [
    "scripts/monitorctl.sh reauth",
    "python3 monitor.py --bootstrap-session --config config.yaml",
    "scripts/monitorctl.sh doctor",
]
EVENT_STALE_MANUAL_STEPS = [
    "scripts/monitorctl.sh status",
    "scripts/monitorctl.sh doctor",
    "scripts/monitorctl.sh reauth",
]


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

        self.probe = probe or BrowserProbe(
            storage_state_path=config.browser_storage_state_path,
            session_mode=config.browser_session_mode,
            user_data_dir=config.browser_user_data_dir,
            channel=config.browser_channel,
            cdp_endpoint_url=config.browser_cdp_endpoint_url,
            cdp_connect_timeout_seconds=config.browser_cdp_connect_timeout_seconds,
            reuse_event_tabs=config.browser_reuse_event_tabs,
            headless=config.browser_headless,
            navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
            stealth_enabled=config.browser_stealth_enabled,
            locale=config.browser_locale,
            timezone_id=config.browser_timezone_id,
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
        # Adaptive cadence: multiplier (>= 1.0) applied to the random poll floor.
        # Grows when a cycle is blocked/challenged, decays back toward 1.0 when healthy.
        self._cadence_backoff: float = 1.0

    def stop(self):
        """Signal the loop to stop."""
        self._running = False

    def run(self):
        """Main loop — runs until stop() is called or interrupted."""
        logger.info("Monitor started. Checking %d event(s).", len(self.config.events))
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
            sleep_time = self._normal_loop_sleep()
            self.state.set_last_cycle_started_at()
            try:
                self._maybe_send_heartbeat()
                self._maybe_check_session_health()
                self._consume_browser_restart_request_if_any()
                needs_slow_retry = self._run_cycle()
                self._consecutive_runtime_errors = 0
                self.state.set_last_cycle_completed_at()
                self.state.clear_last_error()

                sleep_time = self._next_sleep(blocked=needs_slow_retry)

            except BrowserProbeError as exc:
                logger.error("Browser probe runtime error: %s", exc)
                self._consecutive_runtime_errors += 1
                self.state.set_last_cycle_completed_at()
                self.state.set_last_error(self._classify_browser_probe_error(exc), str(exc))
                self._handle_browser_probe_error(exc)
                sleep_time = self._runtime_error_backoff()

            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)
                self._consecutive_runtime_errors += 1
                self.state.set_last_cycle_completed_at()
                self.state.set_last_error(type(exc).__name__, str(exc))
                sleep_time = self._runtime_error_backoff()
                self._maybe_send_error_alert(f"Unexpected monitor error: {type(exc).__name__}: {exc}")

            # Decide (every cycle) whether the monitor has been stuck long enough
            # to warrant a single manual-action ping.
            self._evaluate_manual_action_escalation(datetime.now(timezone.utc))

            if not self._running:
                break
            logger.debug("Next check in %.1f seconds", sleep_time)
            self._interruptible_sleep(sleep_time)

        self.probe.close()

    def run_once(self):
        """Run a single check cycle and return (for --once mode)."""
        self.probe.start()
        self.state.set_last_cycle_started_at()
        self._maybe_send_heartbeat()
        self._maybe_check_session_health()
        self._run_cycle()
        self._evaluate_manual_action_escalation(datetime.now(timezone.utc))
        self.state.set_last_cycle_completed_at()
        self.state.clear_last_error()
        self.probe.close()

    # ---- Core logic ----

    def _run_cycle(self) -> bool:
        """Check all events once. Returns True when slow challenge retry mode is needed."""
        needs_slow_retry = False

        for index, event_cfg in enumerate(self.config.events):
            if not self._running:
                break

            if index > 0:
                # Jitter the inter-event gap a little so request timing looks less robotic.
                stagger = max(0.0, float(self.config.event_stagger_seconds) + self._rand.uniform(-2.0, 2.0))
                self._interruptible_sleep(stagger)

            try:
                probe_result = self.probe.check_event(event_cfg.event_id, event_cfg.url)
            except BrowserProbeError as exc:
                logger.error("[%s] browser probe failed: %s", event_cfg.name, exc)
                self.state.set_last_error(self._classify_browser_probe_error(exc), f"{event_cfg.name}: {exc}")
                self._handle_browser_probe_error(exc)
                needs_slow_retry = True
                continue
            except Exception as exc:
                logger.exception("[%s] unexpected per-event failure: %s", event_cfg.name, exc)
                self.state.set_last_error(type(exc).__name__, f"{event_cfg.name}: {exc}")
                self._maybe_send_error_alert(
                    f"Unexpected per-event check failure for {event_cfg.name}: {type(exc).__name__}: {exc}",
                    context={"event_name": event_cfg.name, "event_id": event_cfg.event_id},
                )
                needs_slow_retry = True
                continue

            logger.info(
                "[%s] available=%s blocked=%s challenge=%s signal=%s confidence=%.2f",
                event_cfg.name,
                probe_result.available,
                probe_result.blocked,
                probe_result.challenge_detected,
                probe_result.signal_type.value,
                probe_result.signal_confidence,
            )

            if probe_result.blocked or probe_result.challenge_detected:
                needs_slow_retry = True

            self._handle_probe_result(event_cfg, probe_result)

            self.state.set_last_check(event_cfg.event_id)
            self._last_successful_check = datetime.now(timezone.utc)
            self.state.set_last_successful_check()

        if self._check_event_poll_staleness(now=datetime.now(timezone.utc)):
            needs_slow_retry = True

        # Keep polling slower while any event remains in outage mode.
        for event_cfg in self.config.events:
            if self.state.get_in_outage_state(event_cfg.event_id):
                needs_slow_retry = True
                break

        return needs_slow_retry

    def _check_event_poll_staleness(self, now: datetime) -> bool:
        """Alert and self-heal when any configured event stops receiving checks."""
        threshold_seconds = int(self.config.alerts_event_check_stale_seconds)
        stale_detected = False

        for event_cfg in self.config.events:
            event_id = event_cfg.event_id
            last_check = self.state.get_last_check(event_id)
            if last_check is None:
                startup_age = (now - self.start_time).total_seconds()
                if startup_age <= threshold_seconds:
                    continue
                age_seconds = int(startup_age)
            else:
                age_seconds = int((now - last_check).total_seconds())

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

        # Effectiveness metrics: one outcome per check, for the live GUI health panel.
        if result.challenge_detected:
            outcome = "challenge"
        elif blind:
            outcome = "blocked"
        else:
            outcome = "healthy"
        self.state.record_check_outcome(outcome, now)

        if blind:
            count = self.state.increment_consecutive_blocked(event_id)
            logger.warning("[%s] blind check #%d", event_cfg.name, count)
            if count >= self.config.browser_challenge_threshold:
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
    def _is_auth_like_failure(result) -> bool:
        if not result.blocked:
            return False
        if result.challenge_detected:
            return False

        indicators = result.raw_indicators if isinstance(result.raw_indicators, dict) else {}
        status = indicators.get("response_status")
        if status in {401, 403}:
            return True
        if status == 429:
            return False

        if result.signal_type != ProbeSignalType.NONE:
            return False

        page_title = str(indicators.get("page_title", "")).lower()
        if any(token in page_title for token in ("sign in", "log in", "login")):
            return True

        return status is None

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

    def _is_monitor_degraded(self, now: datetime) -> bool:
        """True when the monitor genuinely can't get ticket data and self-heal hasn't fixed it.

        Covers: any event in outage (blind/blocked), stale event polling, or auth
        paused after repeated re-login failures. A stale account session alone is
        NOT degraded — Face Value Exchange listings still load without it.
        """
        if any(self.state.get_in_outage_state(ev.event_id) for ev in self.config.events):
            return True
        if self._stale_event_alerted:
            return True
        pause_until = self.state.get_auth_pause_until()
        if pause_until is not None and now < pause_until:
            return True
        return False

    def _manual_action_summary(self, now: datetime) -> tuple[str, list[str]]:
        """Plain-English description + next steps for the current degraded state."""
        pause_until = self.state.get_auth_pause_until()
        if pause_until is not None and now < pause_until:
            return (
                "Auto re-login keeps failing, so the monitor can't refresh your "
                "Ticketmaster session. It needs you to log in manually.",
                REAUTH_MANUAL_STEPS,
            )
        if self._stale_event_alerted:
            return (
                "The monitor has stopped getting fresh data for your event(s) and "
                "auto-recovery hasn't fixed it.",
                EVENT_STALE_MANUAL_STEPS,
            )
        return (
            "The monitor has been blocked from checking tickets and can't clear it "
            "on its own.",
            EVENT_STALE_MANUAL_STEPS,
        )

    def _evaluate_manual_action_escalation(self, now: datetime | None = None):
        """Send a single manual-action ping only after the monitor has stayed degraded
        past the grace window — giving self-healing time to work first."""
        now = now or datetime.now(timezone.utc)

        if not self._is_monitor_degraded(now):
            if self.state.get_attention_since() is not None or self.state.get_attention_alerted():
                logger.info("Monitor recovered; clearing manual-action escalation state")
                self.state.clear_attention()
            return

        since = self.state.get_attention_since()
        if since is None:
            self.state.set_attention_since(now)
            return

        if self.state.get_attention_alerted():
            return

        delay = max(0, int(self.config.alerts_manual_action_after_seconds))
        degraded_seconds = (now - since).total_seconds()
        if degraded_seconds < delay:
            return

        message, next_steps = self._manual_action_summary(now)
        minutes = int(degraded_seconds // 60)
        self.notifier.send_critical_attention(
            message,
            context={"degraded_for": f"{minutes} min"},
            next_steps=next_steps,
        )
        self.state.set_attention_alerted(True)
        logger.critical(
            "Manual-action ping sent after %d min degraded: %s",
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
        interval = self.config.auth_session_health_check_interval_seconds
        last = self.state.get_last_session_health_check_at()
        if last is not None and (now - last).total_seconds() < interval:
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
            return

        reason = result.get("reason", "unknown")
        status = result.get("status")
        challenge = result.get("challenge", False)
        # Log only. A stale account session does not stop Face Value Exchange
        # listings from loading, so this alone is not worth a ping. If it actually
        # breaks checks, that surfaces as an outage and escalates on its own.
        logger.warning(
            "Session health check failed (log-only): reason=%s status=%s challenge=%s",
            reason,
            status,
            challenge,
        )

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
