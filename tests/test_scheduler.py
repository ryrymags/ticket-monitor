"""Tests for browser-first monitor scheduler behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.browser_probe import BrowserProbeError
from src.config import EventConfig, MonitorConfig
from src.models import ProbeResult, ProbeSignalType
from src.preferences import TicketPreferences
from src.session_autofix import AutoReauthResult
from src.scheduler import PROCESS_RESTART_EXIT_CODE, MonitorScheduler
from src.state import MonitorState


def _make_config(**overrides) -> MonitorConfig:
    defaults = dict(
        discord_webhook_url="https://discord.test/webhook",
        discord_username="Test",
        discord_ping_user_id="",
        events=[EventConfig(event_id="event-1", name="Night 1", date="2026-07-28", url="http://event")],
        browser_storage_state_path="secrets/test_state.json",
        browser_session_mode="storage_state",
        browser_user_data_dir="secrets/test_profile",
        browser_channel="chrome",
        browser_cdp_endpoint_url="http://127.0.0.1:9222",
        browser_cdp_connect_timeout_seconds=10,
        browser_reuse_event_tabs=True,
        browser_poll_min_seconds=45,
        browser_poll_max_seconds=60,
        browser_headless=True,
        browser_poll_interval_seconds=12,
        browser_poll_jitter_seconds=2,
        browser_navigation_timeout_seconds=20,
        browser_challenge_threshold=5,
        browser_challenge_retry_seconds=60,
        event_stagger_seconds=6,
        browser_host_enabled=False,
        browser_host_chrome_executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        browser_host_user_data_dir="secrets/tm_chrome_profile",
        browser_host_remote_debugging_port=9222,
        alerts_ticket_cooldown_seconds=180,
        alerts_operational_heartbeat_hours=6,
        alerts_event_check_stale_seconds=180,
        alerts_operational_state_cooldown_seconds=1800,
        backoff_multiplier=2.0,
        max_backoff_seconds=120,
        self_heal_browser_restart_threshold=3,
        self_heal_browser_restart_window_seconds=600,
        self_heal_process_restart_threshold=6,
        self_heal_process_restart_window_seconds=1800,
        self_heal_error_alert_cooldown_seconds=1800,
        auth_auto_login_enabled=False,
        auth_keychain_service="ticket-monitor",
        auth_keychain_email_account="ticketmaster-email",
        auth_keychain_password_account="ticketmaster-password",
        auth_max_auto_login_attempts_per_hour=3,
        auth_auto_login_cooldown_seconds=1800,
        auth_session_health_check_interval_seconds=3600,
        auth_session_health_check_url="https://www.ticketmaster.com/my-account",
        preferences=TicketPreferences(),
        watchdog_enabled=True,
        watchdog_interval_seconds=120,
        watchdog_stale_after_seconds=180,
        watchdog_max_fix_attempts_per_hour=6,
        updates_enabled=True,
        updates_interval_seconds=60,
        updates_stability_delay_seconds=20,
        updates_watch_globs=[
            "monitor.py",
            "src/**/*.py",
            "config.yaml",
            "requirements.txt",
            "pyproject.toml",
        ],
        timezone="US/Eastern",
        log_level="INFO",
        log_file="logs/test.log",
        log_max_file_size_mb=10,
        log_backup_count=3,
    )
    defaults.update(overrides)
    return MonitorConfig(**defaults)


def _make_result(
    *,
    available: bool,
    blocked: bool = False,
    challenge: bool = False,
    signal_type: ProbeSignalType = ProbeSignalType.DOM,
    dom_signals: list[str] | None = None,
    availability_count: int = 0,
    listing_groups: list[dict] | None = None,
) -> ProbeResult:
    return ProbeResult(
        event_id="event-1",
        event_url="http://event",
        available=available,
        blocked=blocked,
        challenge_detected=challenge,
        signal_type=signal_type,
        signal_confidence=0.9,
        price_summary="$99.00 - $129.00" if available else None,
        section_summary="Section 101" if available else None,
        raw_indicators={
            "dom_signals": dom_signals or ["buy_ui"],
            "network_signals": [],
            "availability_count": availability_count,
            "listing_groups": (
                listing_groups
                if listing_groups is not None
                else ([{"section": "Section 101", "row": "1", "price": 99.0, "count": 1}] if available else [])
            ),
        },
        listing_summary="Section 101 / Row 1 / $99.00 x1" if available else None,
    )


def _make_scheduler(tmp_path, config: MonitorConfig | None = None) -> MonitorScheduler:
    config = config or _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    scheduler = MonitorScheduler(
        config=config,
        notifier=MagicMock(),
        state=state,
        start_time=datetime.now(timezone.utc),
        probe=MagicMock(),
    )
    scheduler.probe.start = MagicMock(return_value=None)
    return scheduler


class TestTicketAlerting:
    def test_available_result_triggers_ticket_alert(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]

        scheduler._handle_probe_result(event, _make_result(available=True))

        scheduler.notifier.send_ticket_available.assert_called_once()
        assert scheduler.state.get_last_alert_at(event.event_id) is not None
        assert scheduler.state.get_last_availability_signature(event.event_id) != ""

    def test_listing_groups_forwarded_to_notifier(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        listing_groups = [{"section": "LOGE20", "row": "14", "price": 200.1, "count": 4}]
        result = _make_result(available=True, listing_groups=listing_groups)

        scheduler._handle_probe_result(event, result)

        kwargs = scheduler.notifier.send_ticket_available.call_args.kwargs
        assert kwargs.get("listing_groups") == listing_groups

    def test_duplicate_signature_is_deduped(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)

        scheduler._handle_probe_result(event, result)
        scheduler._handle_probe_result(event, result)

        assert scheduler.notifier.send_ticket_available.call_count == 1

    def test_cooldown_elapsed_realerts_same_signature(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)

        scheduler._handle_probe_result(event, result)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=200)
        scheduler.state.set_last_alert_at(event.event_id, old_time)
        scheduler._handle_probe_result(event, result)

        assert scheduler.notifier.send_ticket_available.call_count == 2


class TestMentionBurst:
    def test_burst_sends_mentions_every_45s_for_up_to_7(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        for step in range(7):
            scheduler._handle_probe_result(
                event,
                result,
                now=base + timedelta(seconds=45 * step),
            )

        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=315))

        assert scheduler.notifier.send_ticket_available.call_count == 7
        assert scheduler.state.get_mention_burst_sent_count(event.event_id) == 7
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is True

        mentions = [call.kwargs.get("mention") for call in scheduler.notifier.send_ticket_available.call_args_list]
        assert mentions == [True, True, True, True, True, True, True]

    def test_after_burst_window_detector_alerts_continue_without_mention(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, result, now=base)
        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=360))

        assert scheduler.notifier.send_ticket_available.call_count == 2
        assert scheduler.notifier.send_ticket_available.call_args_list[-1].kwargs.get("mention") is False

    def test_burst_reminder_sends_when_detector_dedupes(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        listing_groups = [{"section": "BALCONY301", "row": "6", "price": 120.0, "count": 3}]
        result = _make_result(available=True, listing_groups=listing_groups)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, result, now=base)
        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=45))

        assert scheduler.notifier.send_ticket_available.call_count == 2
        second_kwargs = scheduler.notifier.send_ticket_available.call_args_list[-1].kwargs
        assert second_kwargs.get("reason") == "attention_burst"
        assert second_kwargs.get("listing_groups") == listing_groups
        assert scheduler.state.get_last_alert_at(event.event_id) == base

    def test_unavailable_resets_burst_and_rearms_mentions(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        available = _make_result(available=True)
        unavailable = _make_result(
            available=False,
            signal_type=ProbeSignalType.DOM,
            dom_signals=["sold_out_text"],
        )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, available, now=base)
        scheduler._handle_probe_result(event, unavailable, now=base + timedelta(seconds=30))
        scheduler._handle_probe_result(event, available, now=base + timedelta(seconds=60))

        assert scheduler.notifier.send_ticket_available.call_count == 2
        first_call = scheduler.notifier.send_ticket_available.call_args_list[0].kwargs
        second_call = scheduler.notifier.send_ticket_available.call_args_list[1].kwargs
        assert first_call.get("mention") is True
        assert second_call.get("mention") is True

    def test_hard_failsafe_marks_burst_complete(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_mention_burst_started_at(event.event_id, base)
        should_send = scheduler._should_send_mention_burst(event.event_id, base + timedelta(seconds=901))

        assert should_send is False
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is True

    def test_stale_burst_state_is_reset_and_rearmed(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stale_started = now - timedelta(seconds=901)

        scheduler.state.set_mention_burst_started_at(event.event_id, stale_started)
        scheduler.state.set_mention_burst_last_mention_at(event.event_id, stale_started)
        scheduler.state.set_mention_burst_sent_count(event.event_id, 4)
        scheduler.state.set_mention_burst_completed_for_episode(event.event_id, False)

        scheduler._handle_probe_result(event, result, now=now)

        assert scheduler.notifier.send_ticket_available.call_count == 1
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is True
        assert scheduler.state.get_mention_burst_sent_count(event.event_id) == 1
        assert scheduler.state.get_mention_burst_started_at(event.event_id) == now


class TestOutageTracking:
    def test_threshold_triggers_blocked_alert(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(browser_challenge_threshold=3))
        event = scheduler.config.events[0]
        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )

        scheduler._handle_probe_result(event, blocked)
        scheduler._handle_probe_result(event, blocked)
        scheduler._handle_probe_result(event, blocked)

        scheduler.notifier.send_monitor_blocked.assert_called_once()
        blocked_call = scheduler.notifier.send_monitor_blocked.call_args
        assert blocked_call.args[0] == event.name
        assert blocked_call.kwargs.get("auto_fix_planned") == "browser_recycle_now"
        assert blocked_call.kwargs.get("manual_required") is False
        assert blocked_call.kwargs.get("context", {}).get("event_id") == event.event_id
        assert scheduler.state.get_in_outage_state(event.event_id) is True

    def test_threshold_triggers_browser_recycle(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(browser_challenge_threshold=2))
        event = scheduler.config.events[0]
        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )

        scheduler._handle_probe_result(event, blocked)
        scheduler._handle_probe_result(event, blocked)

        assert scheduler.probe.close.call_count >= 1
        assert scheduler.probe.start.call_count >= 1
        assert scheduler.state.get_browser_restart_count_24h() >= 1

    def test_recovery_alert_fires_once(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(browser_challenge_threshold=2))
        event = scheduler.config.events[0]
        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        healthy = _make_result(
            available=False,
            blocked=False,
            challenge=False,
            signal_type=ProbeSignalType.DOM,
            dom_signals=["sold_out_text"],
        )

        scheduler._handle_probe_result(event, blocked)
        scheduler._handle_probe_result(event, blocked)  # enters outage
        scheduler._handle_probe_result(event, healthy)  # recovers

        scheduler.notifier.send_monitor_recovered.assert_called_once()
        assert scheduler.state.get_in_outage_state(event.event_id) is False
        assert scheduler.state.get_consecutive_blocked(event.event_id) == 0


class TestAutoReauth:
    def test_cdp_attach_mode_skips_scripted_auto_reauth(self, tmp_path):
        config = _make_config(
            browser_session_mode="cdp_attach",
            auth_auto_login_enabled=True,
        )
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        probe = MagicMock()
        probe.start = MagicMock(return_value=None)
        session_autofixer = MagicMock(
            attempt_reauth=MagicMock(return_value=AutoReauthResult(success=True, reason="session_refreshed"))
        )
        scheduler = MonitorScheduler(
            config=config,
            notifier=MagicMock(),
            state=state,
            start_time=datetime.now(timezone.utc),
            probe=probe,
            session_autofixer=session_autofixer,
        )

        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        blocked.raw_indicators["response_status"] = 401

        scheduler._handle_probe_result(config.events[0], blocked)

        session_autofixer.attempt_reauth.assert_not_called()

    def test_auth_like_outage_triggers_auto_reauth_success(self, tmp_path):
        config = _make_config(
            browser_challenge_threshold=1,
            auth_auto_login_enabled=True,
            auth_max_auto_login_attempts_per_hour=3,
        )
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        probe = MagicMock()
        probe.start = MagicMock(return_value=None)
        scheduler = MonitorScheduler(
            config=config,
            notifier=MagicMock(),
            state=state,
            start_time=datetime.now(timezone.utc),
            probe=probe,
            session_autofixer=MagicMock(
                attempt_reauth=MagicMock(return_value=AutoReauthResult(success=True, reason="session_refreshed"))
            ),
        )

        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        blocked.raw_indicators["response_status"] = 401

        scheduler._handle_probe_result(config.events[0], blocked)

        scheduler.session_autofixer.attempt_reauth.assert_called_once()
        assert scheduler.probe.close.call_count >= 1
        assert scheduler.probe.start.call_count >= 1
        actions = [call.kwargs.get("action") for call in scheduler.notifier.send_auto_fix_action.call_args_list]
        assert "ticketmaster_reauth_success" in actions

    def test_auto_reauth_failures_trigger_cooldown(self, tmp_path):
        config = _make_config(
            browser_challenge_threshold=1,
            auth_auto_login_enabled=True,
            auth_max_auto_login_attempts_per_hour=2,
            auth_auto_login_cooldown_seconds=600,
        )
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        probe = MagicMock()
        probe.start = MagicMock(return_value=None)
        scheduler = MonitorScheduler(
            config=config,
            notifier=MagicMock(),
            state=state,
            start_time=datetime.now(timezone.utc),
            probe=probe,
            session_autofixer=MagicMock(
                attempt_reauth=MagicMock(return_value=AutoReauthResult(success=False, reason="auth_required_status"))
            ),
        )

        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        blocked.raw_indicators["response_status"] = 403
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(config.events[0], blocked, now=base)
        scheduler._handle_probe_result(config.events[0], blocked, now=base + timedelta(seconds=5))
        scheduler._handle_probe_result(config.events[0], blocked, now=base + timedelta(seconds=10))

        assert scheduler.session_autofixer.attempt_reauth.call_count == 2
        assert scheduler.state.get_auth_pause_until() is not None
        assert scheduler.notifier.send_critical_attention.call_count >= 1
        critical_kwargs = scheduler.notifier.send_critical_attention.call_args.kwargs
        assert "next_steps" in critical_kwargs
        assert "scripts/monitorctl.sh reauth" in critical_kwargs["next_steps"]

    def test_challenge_detected_failure_sends_manual_attention(self, tmp_path):
        config = _make_config(
            browser_challenge_threshold=1,
            auth_auto_login_enabled=True,
            auth_max_auto_login_attempts_per_hour=3,
        )
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        probe = MagicMock()
        probe.start = MagicMock(return_value=None)
        scheduler = MonitorScheduler(
            config=config,
            notifier=MagicMock(),
            state=state,
            start_time=datetime.now(timezone.utc),
            probe=probe,
            session_autofixer=MagicMock(
                attempt_reauth=MagicMock(return_value=AutoReauthResult(success=False, reason="challenge_detected"))
            ),
        )

        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        blocked.raw_indicators["response_status"] = 401

        scheduler._handle_probe_result(config.events[0], blocked)

        assert scheduler.notifier.send_critical_attention.call_count >= 1
        critical_kwargs = scheduler.notifier.send_critical_attention.call_args.kwargs
        assert "next_steps" in critical_kwargs
        assert "scripts/monitorctl.sh reauth" in critical_kwargs["next_steps"]


class TestCycleBehavior:
    def test_run_cycle_returns_slow_retry_on_blocked_event(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        blocked = _make_result(
            available=False,
            blocked=True,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        scheduler.probe.check_event.return_value = blocked

        needs_slow_retry = scheduler._run_cycle()

        assert needs_slow_retry is True

    def test_run_cycle_continues_to_next_event_after_probe_error(self, tmp_path):
        config = _make_config(
            events=[
                EventConfig(event_id="event-1", name="Night 1", date="2026-07-28", url="http://event-1"),
                EventConfig(event_id="event-2", name="Night 2", date="2026-07-29", url="http://event-2"),
            ],
        )
        scheduler = _make_scheduler(tmp_path, config=config)
        healthy = _make_result(available=False, signal_type=ProbeSignalType.DOM, dom_signals=["sold_out_text"])

        def _check_event(event_id: str, _url: str):
            if event_id == "event-1":
                raise BrowserProbeError("simulated per-event failure")
            return healthy

        scheduler.probe.check_event.side_effect = _check_event

        needs_slow_retry = scheduler._run_cycle()

        assert needs_slow_retry is True
        assert scheduler.probe.check_event.call_count == 2
        assert scheduler.state.get_last_check("event-1") is None
        assert scheduler.state.get_last_check("event-2") is not None

    def test_poll_staleness_alerts_and_recovers(self, tmp_path):
        config = _make_config(
            alerts_event_check_stale_seconds=30,
            events=[
                EventConfig(event_id="event-1", name="Night 1", date="2026-07-28", url="http://event-1"),
                EventConfig(event_id="event-2", name="Night 2", date="2026-07-29", url="http://event-2"),
            ],
        )
        scheduler = _make_scheduler(tmp_path, config=config)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler.state._event("event-1")["last_check"] = (now - timedelta(seconds=90)).isoformat()
        scheduler.state._event("event-2")["last_check"] = (now - timedelta(seconds=10)).isoformat()

        stale = scheduler._check_event_poll_staleness(now=now)

        assert stale is True
        scheduler.notifier.send_critical_attention.assert_called_once()
        assert scheduler.probe.close.call_count >= 1
        assert scheduler.probe.start.call_count >= 1

        scheduler.state._event("event-1")["last_check"] = (now - timedelta(seconds=5)).isoformat()
        stale_after_recovery = scheduler._check_event_poll_staleness(now=now + timedelta(seconds=1))

        assert stale_after_recovery is False
        assert "event-1" not in scheduler._stale_event_alerted
        assert scheduler.notifier.send_critical_attention.call_count == 1

    def test_runtime_backoff_grows_and_caps(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._consecutive_runtime_errors = 1
        assert scheduler._runtime_error_backoff() == 10.0
        scheduler._consecutive_runtime_errors = 2
        assert scheduler._runtime_error_backoff() == 20.0
        scheduler._consecutive_runtime_errors = 3
        assert scheduler._runtime_error_backoff() == 40.0
        scheduler._consecutive_runtime_errors = 10
        assert scheduler._runtime_error_backoff() == 120.0


class TestSelfHealing:
    def test_error_classification_prefers_wrapped_exception_type(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)

        class TimeoutError(Exception):
            pass

        class Error(Exception):
            pass

        try:
            try:
                raise TimeoutError("navigation timeout")
            except TimeoutError as timeout_exc:
                raise BrowserProbeError("wrapped timeout") from timeout_exc
        except BrowserProbeError as wrapped_timeout:
            assert scheduler._classify_browser_probe_error(wrapped_timeout) == "timeout"

        try:
            try:
                raise Error("Target page, context or browser has been closed")
            except Error as closed_exc:
                raise BrowserProbeError("wrapped closed") from closed_exc
        except BrowserProbeError as wrapped_closed:
            assert scheduler._classify_browser_probe_error(wrapped_closed) == "crash"

    def test_browser_errors_trigger_recycle(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                self_heal_browser_restart_threshold=2,
                self_heal_process_restart_threshold=10,
            ),
        )

        scheduler._handle_browser_probe_error(BrowserProbeError("Page.goto timeout 20000ms"))
        scheduler._handle_browser_probe_error(BrowserProbeError("Page.goto timeout 20000ms"))

        assert scheduler.probe.close.call_count >= 1
        assert scheduler.probe.start.call_count >= 1
        assert scheduler.state.get_browser_restart_count_24h() >= 1

    def test_process_restart_threshold_exits(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                self_heal_browser_restart_threshold=10,
                self_heal_process_restart_threshold=3,
            ),
        )

        scheduler._handle_browser_probe_error(BrowserProbeError("timeout #1"))
        scheduler._handle_browser_probe_error(BrowserProbeError("timeout #2"))
        try:
            scheduler._handle_browser_probe_error(BrowserProbeError("timeout #3"))
            assert False, "Expected SystemExit for process restart threshold"
        except SystemExit as exc:
            assert exc.code == PROCESS_RESTART_EXIT_CODE
