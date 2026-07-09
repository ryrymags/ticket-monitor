"""Tests for browser-first monitor scheduler behavior."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.browser_probe import BrowserProbeError
from src.config import EventConfig, MonitorConfig
from src.models import ProbeResult, ProbeSignalType
from src.preferences import TicketPreferences
from src.session_autofix import AutoReauthResult
from src.scheduler import (
    PROCESS_RESTART_EXIT_CODE,
    MonitorScheduler,
    _is_connectivity_error,
)
from src.state import MonitorState


@pytest.fixture(autouse=True)
def _fast_ping_spacing(monkeypatch):
    """Skip the real 1s spacing between rapid mention pings in tests."""
    monkeypatch.setattr("src.scheduler.MENTION_PING_SPACING_SECONDS", 0.0)


def _make_config(**overrides) -> MonitorConfig:
    defaults = dict(
        discord_webhook_url="https://discord.test/webhook",
        discord_username="Test",
        discord_ping_user_id="",
        ntfy_enabled=False,
        ntfy_topics=[],
        ntfy_server="https://ntfy.sh",
        ntfy_priority="high",
        events=[EventConfig(event_id="event-1", name="Night 1", date="2030-01-01", url="http://event")],
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
        browser_challenge_cooldown_base_seconds=60,
        browser_challenge_cooldown_max_seconds=1800,
        browser_challenge_cooldown_escalate_after=6,
        browser_challenge_cooldown_tiers_seconds=[300, 900, 1800],
        browser_challenge_cooldown_tier_every=3,
        browser_startup_grace_seconds=0,
        event_stagger_seconds=6,
        browser_adaptive_backoff_enabled=True,
        browser_adaptive_backoff_multiplier=2.0,
        browser_adaptive_recover_factor=0.5,
        browser_adaptive_max_seconds=300,
        browser_stealth_enabled=False,
        browser_locale="en-US",
        browser_timezone_id="America/New_York",
        browser_host_enabled=False,
        browser_host_chrome_executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        browser_host_user_data_dir="secrets/tm_chrome_profile",
        browser_host_remote_debugging_port=9222,
        alerts_ticket_cooldown_seconds=180,
        alerts_operational_heartbeat_hours=6,
        alerts_event_check_stale_seconds=180,
        alerts_operational_state_cooldown_seconds=1800,
        alerts_non_bingo_enabled=False,
        alerts_manual_action_after_seconds=900,
        alerts_operational_to_discord=False,
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
        auth_session_recheck_base_seconds=120,
        auth_session_recheck_max_seconds=900,
        auth_session_logout_confirmations_required=2,
        preferences=TicketPreferences(),
        bingo_configs=[TicketPreferences()],
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


class _FixedRand:
    def uniform(self, _low, _high):
        return 1.0


class TestTicketAlerting:
    def test_available_result_triggers_ticket_alert(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]

        scheduler._handle_probe_result(event, _make_result(available=True))

        # A new BINGO episode fires MENTION_PINGS_PER_EPISODE rapid pings.
        assert scheduler.notifier.send_ticket_available.call_count == 3
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

    def test_non_bingo_sends_message_without_mention(self, tmp_path):
        prefs = [
            TicketPreferences(
                min_tickets=2,
                max_price_per_ticket=200.0,
                preferred_sections=["LOGE"],
                alert_on_any_availability=False,
                name="LOGE pairs",
            )
        ]
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(preferences=prefs[0], bingo_configs=prefs),
        )
        event = scheduler.config.events[0]
        # FLOOR1 meets count+price for the LOGE config but is the wrong section →
        # non-BINGO. With non_bingo_enabled off it still posts a webhook message +
        # History, but must NOT @-mention.
        listing_groups = [{"section": "FLOOR1", "row": "A", "price": 150.0, "count": 2}]

        scheduler._handle_probe_result(event, _make_result(available=True, listing_groups=listing_groups))

        scheduler.notifier.send_ticket_available.assert_called_once()
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is False
        assert scheduler.state.get_last_available_at(event.event_id) is not None

    def test_second_bingo_config_alerts_when_first_config_does_not_match(self, tmp_path):
        prefs = [
            TicketPreferences(
                min_tickets=2,
                max_price_per_ticket=200.0,
                preferred_sections=["LOGE"],
                alert_on_any_availability=False,
                name="LOGE pairs",
            ),
            TicketPreferences(
                min_tickets=2,
                max_price_per_ticket=250.0,
                preferred_sections=["FLOOR"],
                alert_on_any_availability=False,
                name="Floor pairs",
            ),
        ]
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(preferences=prefs[0], bingo_configs=prefs),
        )
        event = scheduler.config.events[0]
        listing_groups = [{"section": "FLOOR1", "row": "A", "price": 150.0, "count": 2}]

        scheduler._handle_probe_result(event, _make_result(available=True, listing_groups=listing_groups))

        assert scheduler.notifier.send_ticket_available.call_count == 3
        kwargs = scheduler.notifier.send_ticket_available.call_args.kwargs
        assert kwargs.get("preferences") == prefs

    def test_duplicate_signature_is_deduped(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, result, now=base)
        # Once the ping episode is over, an identical signal within the cooldown
        # window is fully deduped (no primary alert, no extra ping).
        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=30))

        assert scheduler.notifier.send_ticket_available.call_count == 3

    def test_cooldown_elapsed_realerts_same_signature(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)

        scheduler._handle_probe_result(event, result)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=200)
        scheduler.state.set_last_alert_at(event.event_id, old_time)
        scheduler._handle_probe_result(event, result)

        # Triple ping on the new episode, then a single mention-less reminder.
        assert scheduler.notifier.send_ticket_available.call_count == 4
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is False


class TestMentionPings:
    """A new BINGO episode fires exactly MENTION_PINGS_PER_EPISODE rapid pings
    (Discord @-mention + ntfy each), then that listing stays silent forever.
    No time-based bursts, no re-pings for lingering/flapping listings."""

    def test_new_bingo_fires_exactly_three_pings(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, _make_result(available=True), now=base)

        calls = scheduler.notifier.send_ticket_available.call_args_list
        assert len(calls) == 3
        assert [c.kwargs.get("mention") for c in calls] == [True, True, True]
        assert calls[0].kwargs.get("reason") == "signature_changed"
        assert [c.kwargs.get("reason") for c in calls[1:]] == ["attention_burst", "attention_burst"]
        # Repeat pings must not duplicate the History row.
        assert [c.kwargs.get("record_history") for c in calls] == [True, False, False]
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is True

    def test_lingering_listing_never_repings(self, tmp_path):
        # The core fix: the old 900s failsafe re-armed a FULL burst every ~15 min
        # for a lingering listing. Now: 3 pings, then silence for that signature.
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, result, now=base)  # triple ping
        for offset in (90, 200, 950, 2000, 7200):
            scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=offset))

        mentions = [c.kwargs.get("mention") for c in scheduler.notifier.send_ticket_available.call_args_list]
        assert mentions[:3] == [True, True, True]
        assert True not in mentions[3:]

    def test_same_listing_flapping_does_not_reping(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        available = _make_result(available=True)
        unavailable = _make_result(available=False, signal_type=ProbeSignalType.DOM, dom_signals=["sold_out_text"])
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, available, now=base)
        scheduler._handle_probe_result(event, unavailable, now=base + timedelta(seconds=320))
        scheduler._handle_probe_result(event, available, now=base + timedelta(seconds=1300))

        mentions = [c.kwargs.get("mention") for c in scheduler.notifier.send_ticket_available.call_args_list]
        assert True not in mentions[3:]
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is True

    def test_new_listing_signature_starts_fresh_episode(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        listing_a = _make_result(available=True, dom_signals=["buy_ui"])
        listing_b = _make_result(available=True, dom_signals=["buy_ui", "resale_ui"])
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, listing_a, now=base)
        scheduler._handle_probe_result(event, listing_b, now=base + timedelta(seconds=400))

        calls = scheduler.notifier.send_ticket_available.call_args_list
        assert len(calls) == 6  # two full 3-ping episodes
        assert calls[3].kwargs.get("reason") == "signature_changed"
        assert [c.kwargs.get("mention") for c in calls] == [True] * 6

    def test_failed_send_resumes_remaining_pings_next_poll(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # First ping delivers, second fails -> pings still owed; next poll retries.
        scheduler.notifier.send_ticket_available.side_effect = [True, False, True, True]

        scheduler._handle_probe_result(event, result, now=base)
        assert scheduler.notifier.send_ticket_available.call_count == 2
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is False

        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=90))
        calls = scheduler.notifier.send_ticket_available.call_args_list
        assert len(calls) == 4
        assert [c.kwargs.get("mention") for c in calls] == [True] * 4
        assert scheduler.state.get_mention_burst_completed_for_episode(event.event_id) is True

    def test_post_episode_reminders_have_no_mention(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        result = _make_result(available=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(event, result, now=base)
        # Past the detector cooldown the embed still posts (History trail) but silently.
        scheduler._handle_probe_result(event, result, now=base + timedelta(seconds=360))

        last = scheduler.notifier.send_ticket_available.call_args_list[-1].kwargs
        assert last.get("mention") is False
        assert last.get("reason") == "cooldown_elapsed"


class TestUptimeHeartbeatGap:
    """The ledger's down-gap budget must include the cycle's measured work time,
    not just the planned sleep — otherwise slow-but-healthy cycles (alert sends,
    page timeouts) get back-filled as spurious downtime."""

    def test_expected_gap_includes_cycle_work(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        now = datetime.now(timezone.utc)
        scheduler._last_loop_sleep_seconds = 60.0
        scheduler._cycle_started_at = now - timedelta(seconds=100)

        assert scheduler._heartbeat_expected_gap(now) == pytest.approx(160.0, abs=0.5)

    def test_first_cycle_gap_stays_none(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._cycle_started_at = datetime.now(timezone.utc)

        assert scheduler._heartbeat_expected_gap(datetime.now(timezone.utc)) is None

    def test_slow_cycle_work_is_not_recorded_as_down(self, tmp_path):
        # Regression for 2026-07-09: cycles that spent ~100s checking + sending
        # a ticket alert were back-filled as "down offline" because only the 60s
        # planned sleep was budgeted and the 90s base margin absorbed the rest.
        scheduler = _make_scheduler(tmp_path)
        now = datetime.now(timezone.utc)
        scheduler.uptime.heartbeat(now - timedelta(seconds=160), "healthy", None)
        scheduler._last_loop_sleep_seconds = 60.0
        scheduler._cycle_started_at = now - timedelta(seconds=100)
        scheduler._cycle_healthy_checks = 1

        scheduler._record_uptime_heartbeat(False)

        assert "down" not in [seg["state"] for seg in scheduler.uptime.segments]

    def test_true_silence_still_backfills_down(self, tmp_path):
        # A genuine suspend stretches the gap far beyond sleep + work and must
        # still be recorded as downtime.
        scheduler = _make_scheduler(tmp_path)
        now = datetime.now(timezone.utc)
        scheduler.uptime.heartbeat(now - timedelta(seconds=400), "healthy", None)
        scheduler._last_loop_sleep_seconds = 60.0
        scheduler._cycle_started_at = now - timedelta(seconds=10)
        scheduler._cycle_healthy_checks = 1

        scheduler._record_uptime_heartbeat(False)

        assert "down" in [seg["state"] for seg in scheduler.uptime.segments]


class TestOutageTracking:
    def test_no_inventory_healthy_page_does_not_enter_outage(self, tmp_path):
        """A healthy 2xx page that just lists no tickets must not be counted as
        blind — otherwise empty events trip the outage → manual-action ping while
        the GUI (which reads last_check) shows green."""
        scheduler = _make_scheduler(tmp_path, _make_config(browser_challenge_threshold=3))
        event = scheduler.config.events[0]
        empty = _make_result(
            available=False,
            blocked=False,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        empty.raw_indicators["response_status"] = 200

        for _ in range(5):
            scheduler._handle_probe_result(event, empty)

        scheduler.notifier.send_monitor_blocked.assert_not_called()
        assert scheduler.state.get_in_outage_state(event.event_id) is False
        assert scheduler.state.get_consecutive_blocked(event.event_id) == 0

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
    def test_auth_like_failure_classification(self):
        blocked = _make_result(
            available=False,
            blocked=True,
            challenge=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )

        blocked.raw_indicators["response_status"] = 401
        assert MonitorScheduler._is_auth_like_failure(blocked) is True

        blocked.raw_indicators["response_status"] = 403
        blocked.raw_indicators["page_title"] = ""
        assert MonitorScheduler._is_auth_like_failure(blocked) is False

        blocked.raw_indicators["response_status"] = None
        assert MonitorScheduler._is_auth_like_failure(blocked) is False

        blocked.raw_indicators["page_title"] = "Ticketmaster Login"
        assert MonitorScheduler._is_auth_like_failure(blocked) is True

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
        blocked.raw_indicators["response_status"] = 401
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(config.events[0], blocked, now=base)
        scheduler._handle_probe_result(config.events[0], blocked, now=base + timedelta(seconds=5))
        scheduler._handle_probe_result(config.events[0], blocked, now=base + timedelta(seconds=10))

        assert scheduler.session_autofixer.attempt_reauth.call_count == 2
        assert scheduler.state.get_auth_pause_until() is not None
        # Auth pause is recorded as degraded, but no immediate ping — the manual-action
        # escalation only fires after the grace window (covered separately).
        scheduler.notifier.send_critical_attention.assert_not_called()

    def test_bare_403_block_does_not_trigger_auto_reauth(self, tmp_path):
        config = _make_config(
            browser_challenge_threshold=1,
            auth_auto_login_enabled=True,
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
        blocked.raw_indicators["response_status"] = 403

        scheduler._handle_probe_result(config.events[0], blocked)

        scheduler.session_autofixer.attempt_reauth.assert_not_called()

    def test_challenge_detected_failure_does_not_ping_immediately(self, tmp_path):
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

        # A challenge during auto re-login is logged + recorded as degraded, not pinged
        # immediately. The single manual-action ping is gated by the grace window.
        scheduler.notifier.send_critical_attention.assert_not_called()
        actions = [c.kwargs.get("action") for c in scheduler.notifier.send_auto_fix_action.call_args_list]
        assert "ticketmaster_reauth_failed" in actions


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

    def test_run_skips_active_probes_during_challenge_cooldown(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.state.set_challenge_cooldown_until(datetime.now(timezone.utc) + timedelta(minutes=10))
        scheduler._maybe_send_heartbeat = MagicMock()
        scheduler._consume_browser_restart_request_if_any = MagicMock()
        scheduler._run_cycle = MagicMock(return_value=False)
        scheduler._maybe_check_session_health = MagicMock()
        scheduler._evaluate_manual_action_escalation = MagicMock()
        scheduler._apply_challenge_cooldown = MagicMock(return_value=0.01)
        scheduler._record_uptime_heartbeat = MagicMock()

        def stop_after_sleep(_seconds):
            scheduler.stop()

        scheduler._interruptible_sleep = MagicMock(side_effect=stop_after_sleep)

        scheduler.run()

        scheduler._run_cycle.assert_not_called()
        scheduler._maybe_check_session_health.assert_not_called()
        scheduler._consume_browser_restart_request_if_any.assert_called_once()
        scheduler._record_uptime_heartbeat.assert_called_once_with(True, reason="blocked")

    def test_run_cycle_continues_to_next_event_after_probe_error(self, tmp_path):
        config = _make_config(
            events=[
                EventConfig(event_id="event-1", name="Night 1", date="2030-01-01", url="http://event-1"),
                EventConfig(event_id="event-2", name="Night 2", date="2030-01-02", url="http://event-2"),
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
                EventConfig(event_id="event-1", name="Night 1", date="2030-01-01", url="http://event-1"),
                EventConfig(event_id="event-2", name="Night 2", date="2030-01-02", url="http://event-2"),
            ],
        )
        scheduler = _make_scheduler(tmp_path, config=config)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Pretend the monitor has been running a while (staleness only counts uptime).
        scheduler._uptime_anchor = now - timedelta(hours=1)

        scheduler.state._event("event-1")["last_check"] = (now - timedelta(seconds=90)).isoformat()
        scheduler.state._event("event-2")["last_check"] = (now - timedelta(seconds=10)).isoformat()

        stale = scheduler._check_event_poll_staleness(now=now)

        assert stale is True
        # Staleness self-heals first (recycle) and records degraded state — no immediate ping.
        scheduler.notifier.send_critical_attention.assert_not_called()
        assert "event-1" in scheduler._stale_event_alerted
        assert scheduler.probe.close.call_count >= 1
        assert scheduler.probe.start.call_count >= 1

        scheduler.state._event("event-1")["last_check"] = (now - timedelta(seconds=5)).isoformat()
        stale_after_recovery = scheduler._check_event_poll_staleness(now=now + timedelta(seconds=1))

        assert stale_after_recovery is False
        assert "event-1" not in scheduler._stale_event_alerted
        scheduler.notifier.send_critical_attention.assert_not_called()

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


class TestPerEventScheduler:
    def _two_event_config(self, **overrides):
        events = overrides.pop(
            "events",
            [
                EventConfig(event_id="tuesday", name="Tuesday", date="2030-01-01", url="http://tuesday"),
                EventConfig(event_id="wednesday", name="Wednesday", date="2030-01-02", url="http://wednesday"),
            ],
        )
        defaults = dict(
            events=events,
            browser_per_event_scheduler_enabled=True,
            browser_per_event_poll_min_seconds=45,
            browser_per_event_poll_max_seconds=105,
            browser_per_event_min_gap_between_checks_seconds=20,
        )
        defaults.update(overrides)
        return _make_config(**defaults)

    def test_due_cycle_checks_only_one_event_and_reschedules_it(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, self._two_event_config())
        due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        scheduler._event_schedule = {"tuesday": due_at, "wednesday": due_at}
        clean = _make_result(
            available=False,
            blocked=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        scheduler.probe.check_event.return_value = clean

        needs_slow_retry = scheduler._run_due_event_cycle()

        assert needs_slow_retry is False
        scheduler.probe.check_event.assert_called_once()
        checked_event_id = scheduler.probe.check_event.call_args.args[0]
        other_event_id = "wednesday" if checked_event_id == "tuesday" else "tuesday"
        assert scheduler._event_schedule[checked_event_id] > due_at
        assert scheduler._event_schedule[other_event_id] == due_at
        assert scheduler.state.get_last_check(checked_event_id) is not None
        assert scheduler.state.get_last_check(other_event_id) is None

    def test_min_gap_prevents_back_to_back_due_events(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, self._two_event_config())
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._event_schedule = {
            "tuesday": base - timedelta(seconds=1),
            "wednesday": base - timedelta(seconds=1),
        }
        scheduler._next_event_check_not_before = base + timedelta(seconds=20)

        event, wait_seconds = scheduler._select_due_event(base + timedelta(seconds=5))

        assert event is None
        assert wait_seconds == 15

    def test_weighted_intervals_favor_wednesday_without_fixed_sequence(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            self._two_event_config(
                browser_event_weights={"tuesday": 1.0, "wednesday": 2.0},
            ),
        )
        scheduler._rand = random.Random(1234)

        tuesday = [scheduler._event_interval_seconds("tuesday") for _ in range(500)]
        wednesday = [scheduler._event_interval_seconds("wednesday") for _ in range(500)]

        assert min(tuesday + wednesday) >= 45
        assert max(tuesday + wednesday) <= 105
        assert sum(wednesday) / len(wednesday) < sum(tuesday) / len(tuesday)
        assert len({round(value, 1) for value in wednesday[:20]}) > 5

    def test_blocked_event_gets_longer_cooldown(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            self._two_event_config(
                events=[
                    EventConfig(event_id="event-1", name="Night 1", date="2030-01-01", url="http://event")
                ],
                browser_per_event_poll_min_seconds=10,
                browser_per_event_poll_max_seconds=10,
                browser_challenge_retry_seconds=60,
            ),
        )
        scheduler._rand = _FixedRand()
        due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        scheduler._event_schedule = {"event-1": due_at}
        blocked = _make_result(
            available=False,
            blocked=True,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        scheduler.probe.check_event.return_value = blocked

        scheduler._run_due_event_cycle()

        assert (scheduler._event_schedule["event-1"] - datetime.now(timezone.utc)).total_seconds() >= 59

    def test_legacy_cycle_mode_still_checks_all_events(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            self._two_event_config(browser_per_event_scheduler_enabled=False),
        )
        clean = _make_result(
            available=False,
            blocked=False,
            signal_type=ProbeSignalType.NONE,
            dom_signals=[],
        )
        scheduler.probe.check_event.return_value = clean

        scheduler._run_cycle()

        assert scheduler.probe.check_event.call_count == 2


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


class TestManualActionEscalation:
    def test_no_ping_when_healthy(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._evaluate_manual_action_escalation(datetime(2026, 1, 1, tzinfo=timezone.utc))
        scheduler.notifier.send_critical_attention.assert_not_called()
        assert scheduler.state.get_attention_since() is None

    def test_no_ping_before_grace_window(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(alerts_manual_action_after_seconds=900))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_session_logged_out(True)

        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=600))

        scheduler.notifier.send_critical_attention.assert_not_called()
        assert scheduler.state.get_attention_since() is not None

    def test_single_ping_after_grace_window(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(alerts_manual_action_after_seconds=900))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_session_logged_out(True)

        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901))

        scheduler.notifier.send_critical_attention.assert_called_once()
        kwargs = scheduler.notifier.send_critical_attention.call_args.kwargs
        assert "next_steps" in kwargs and kwargs["next_steps"]

        # A subsequent evaluation while still degraded must not double-ping.
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=950))
        assert scheduler.notifier.send_critical_attention.call_count == 1

    def test_blocks_and_stale_never_ping(self, tmp_path):
        # The whole philosophy: blocks/stale self-heal — they set the GUI degraded state
        # but must NEVER fire the manual-action ping. Only logged-out does.
        scheduler = _make_scheduler(tmp_path, _make_config(alerts_manual_action_after_seconds=0))
        event = scheduler.config.events[0]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler.state.set_in_outage_state(event.event_id, True)
        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=10))

        scheduler.notifier.send_critical_attention.assert_not_called()
        snap = scheduler.state.get_degraded_state()
        assert snap["degraded"] is True and snap["reason"] == "outage"

    def test_recovery_clears_escalation_state(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, _make_config(alerts_manual_action_after_seconds=900))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_session_logged_out(True)
        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901))

        scheduler.state.set_session_logged_out(False)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=1000))

        assert scheduler.state.get_attention_since() is None
        assert scheduler.state.get_attention_alerted() is False

    def test_login_copy_points_at_double_click_and_app(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        message, steps = scheduler._manual_action_summary("logged_out")
        assert "log back in" in message.lower()
        joined = " ".join(steps)
        assert "Reauth.command" in joined
        assert "Login tab" in joined


class TestNonBingoGlobalGate:
    def _floor_listing(self):
        return [{"section": "FLOOR1", "row": "A", "price": 150.0, "count": 2}]

    def _prefs(self):
        # Section filter on LOGE; a FLOOR listing is count+price OK but not preferred → non-BINGO.
        return [TicketPreferences(
            min_tickets=2, max_price_per_ticket=200.0,
            preferred_sections=["LOGE"], alert_on_any_availability=True, name="LOGE pairs",
        )]

    def test_non_bingo_sends_without_mention_when_global_flag_off(self, tmp_path):
        prefs = self._prefs()
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(preferences=prefs[0], bingo_configs=prefs, alerts_non_bingo_enabled=False),
        )
        event = scheduler.config.events[0]
        scheduler._handle_probe_result(event, _make_result(available=True, listing_groups=self._floor_listing()))
        # Webhook message + History still post for the detection — but no @-mention.
        scheduler.notifier.send_ticket_available.assert_called_once()
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is False

    def test_non_bingo_mentions_when_global_flag_on(self, tmp_path):
        prefs = self._prefs()
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(preferences=prefs[0], bingo_configs=prefs, alerts_non_bingo_enabled=True),
        )
        event = scheduler.config.events[0]
        scheduler._handle_probe_result(event, _make_result(available=True, listing_groups=self._floor_listing()))
        assert scheduler.notifier.send_ticket_available.call_count == 3
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is True

    def test_bingo_always_mentions_regardless_of_flag(self, tmp_path):
        prefs = self._prefs()
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(preferences=prefs[0], bingo_configs=prefs, alerts_non_bingo_enabled=False),
        )
        event = scheduler.config.events[0]
        loge = [{"section": "LOGE20", "row": "5", "price": 150.0, "count": 2}]
        scheduler._handle_probe_result(event, _make_result(available=True, listing_groups=loge))
        assert scheduler.notifier.send_ticket_available.call_count == 3
        assert scheduler.notifier.send_ticket_available.call_args.kwargs.get("mention") is True


class TestAdaptiveCadence:
    def _sched(self, tmp_path, **over):
        cfg = _make_config(
            browser_poll_min_seconds=10, browser_poll_max_seconds=10, **over
        )
        return _make_scheduler(tmp_path, cfg)

    def test_backoff_grows_on_block_and_decays_when_healthy(self, tmp_path):
        s = self._sched(tmp_path)
        assert s._next_sleep(blocked=True) == 20.0   # 10 * 2
        assert s._next_sleep(blocked=True) == 40.0   # 10 * 4
        assert s._next_sleep(blocked=False) == 20.0  # decay -> *2
        assert s._next_sleep(blocked=False) == 10.0  # decay -> *1 (floor)
        assert s._next_sleep(blocked=False) == 10.0  # stays at floor

    def test_backoff_clamped_to_max(self, tmp_path):
        s = self._sched(tmp_path, browser_adaptive_max_seconds=50)
        sleep = 0.0
        for _ in range(10):
            sleep = s._next_sleep(blocked=True)
        assert sleep == 50.0

    def test_disabled_uses_fixed_retry_and_floor(self, tmp_path):
        s = self._sched(
            tmp_path,
            browser_adaptive_backoff_enabled=False,
            browser_challenge_retry_seconds=60,
        )
        assert s._next_sleep(blocked=True) == 60.0   # fixed challenge retry
        assert s._next_sleep(blocked=False) == 10.0  # random floor


class TestCheckOutcomeRecording:
    def test_records_outcome_per_branch(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        scheduler.state.record_check_outcome = MagicMock()

        scheduler._handle_probe_result(event, _make_result(available=True))
        scheduler._handle_probe_result(
            event,
            _make_result(available=False, blocked=True, signal_type=ProbeSignalType.NONE, dom_signals=[]),
        )
        scheduler._handle_probe_result(
            event,
            _make_result(available=False, challenge=True, signal_type=ProbeSignalType.NONE, dom_signals=[]),
        )

        outcomes = [c.args[0] for c in scheduler.state.record_check_outcome.call_args_list]
        assert outcomes == ["healthy", "blocked", "challenge"]


class TestSessionHealthChecks:
    @staticmethod
    def _health_result(reason: str, *, status: int | None = None, challenge: bool = False, definitive: bool = False):
        return {
            "healthy": False,
            "reason": reason,
            "status": status,
            "challenge": challenge,
            "definitive_logged_out": definitive,
        }

    def test_403_health_result_is_block_not_logout(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.probe.check_session_health.return_value = self._health_result(
            "http_403", status=403, definitive=False
        )

        scheduler._maybe_check_session_health(base)

        assert scheduler.state.get_session_logged_out() is False
        assert scheduler.state.get_session_logout_pending_count() == 0
        assert scheduler.state.get_last_session_health_reason() == "http_403"
        assert scheduler._session_health_fail_streak == 1

    def test_definitive_logout_requires_two_confirmations(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.probe.check_session_health.return_value = self._health_result(
            "login_page_content", status=200, definitive=True
        )

        scheduler._maybe_check_session_health(base)

        assert scheduler.state.get_session_logged_out() is False
        assert scheduler.state.get_session_logout_pending_count() == 1

        scheduler._maybe_check_session_health(base + timedelta(seconds=121))

        assert scheduler.state.get_session_logged_out() is True
        assert scheduler.state.get_session_logout_pending_count() == 0

    def test_healthy_session_check_resets_pending_and_flag(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.probe.check_session_health.return_value = self._health_result(
            "login_page_content", status=200, definitive=True
        )
        scheduler._maybe_check_session_health(base)
        scheduler.state.set_session_logged_out(True)
        scheduler.probe.check_session_health.return_value = {
            "healthy": True,
            "reason": "ok",
            "status": 200,
            "challenge": False,
            "definitive_logged_out": False,
        }

        scheduler._maybe_check_session_health(base + timedelta(seconds=121))

        assert scheduler.state.get_session_logged_out() is False
        assert scheduler.state.get_session_logout_pending_count() == 0
        assert scheduler._session_health_fail_streak == 0

    def test_session_probe_skips_during_challenge_cooldown_without_stamping_last_check(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_challenge_cooldown_until(base + timedelta(minutes=5))

        scheduler._maybe_check_session_health(base)

        scheduler.probe.check_session_health.assert_not_called()
        assert scheduler.state.get_last_session_health_check_at() is None

    def test_session_probe_defers_after_blocked_event_cycle(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._last_cycle_blocked = True

        scheduler._maybe_check_session_health(base)

        scheduler.probe.check_session_health.assert_not_called()
        assert scheduler.state.get_last_session_health_check_at() is None

    def test_fast_recheck_interval_grows_and_caps(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(auth_session_recheck_base_seconds=120, auth_session_recheck_max_seconds=300),
        )
        scheduler._rand = _FixedRand()

        assert scheduler._session_health_due_interval() == 3600.0
        scheduler._session_health_fail_streak = 1
        assert scheduler._session_health_due_interval() == 120.0
        scheduler._session_health_fail_streak = 2
        assert scheduler._session_health_due_interval() == 240.0
        scheduler._session_health_fail_streak = 3
        assert scheduler._session_health_due_interval() == 300.0
        scheduler._session_health_fail_streak = 0
        scheduler.state.set_session_logged_out(True)
        assert scheduler._session_health_due_interval() == 120.0


class TestDegradedStatePersistence:
    """The GUI reads health.degraded* from state.json; it must match the exact
    condition that drives the manual-action ping (single source of truth)."""

    def test_outage_persists_degraded_reason(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_in_outage_state(event.event_id, True)

        scheduler._evaluate_manual_action_escalation(base)

        snap = scheduler.state.get_degraded_state()
        assert snap["degraded"] is True
        assert snap["reason"] == "outage"
        assert snap["since"] is not None

    def test_auth_pause_persists_degraded_even_when_checks_fresh(self, tmp_path):
        # The discrepancy bug: auth paused → ping fires, but the GUI used to show green
        # because Face Value Exchange listings still load. Now it's persisted as degraded.
        scheduler = _make_scheduler(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_auth_pause_until(base + timedelta(hours=1))

        scheduler._evaluate_manual_action_escalation(base)

        snap = scheduler.state.get_degraded_state()
        assert snap["degraded"] is True
        assert snap["reason"] == "auth_paused"

    def test_recovery_clears_degraded_state(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event = scheduler.config.events[0]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler.state.set_in_outage_state(event.event_id, True)
        scheduler._evaluate_manual_action_escalation(base)
        assert scheduler.state.get_degraded_state()["degraded"] is True

        scheduler.state.set_in_outage_state(event.event_id, False)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=10))

        assert scheduler.state.get_degraded_state()["degraded"] is False
        assert scheduler.state.get_degraded_state()["reason"] is None


class TestPerEventUptime:
    @staticmethod
    def _two_event_config():
        return _make_config(
            events=[
                EventConfig(event_id="ev-a", name="Night A", date="2030-01-01", url="http://a"),
                EventConfig(event_id="ev-b", name="Night B", date="2030-01-02", url="http://b"),
            ]
        )

    def test_per_event_ledgers_created(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, self._two_event_config())
        assert set(scheduler._event_uptime.keys()) == {"ev-a", "ev-b"}

    def test_block_page_records_impaired_for_event_uptime(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, self._two_event_config())
        ev_a, ev_b = scheduler.config.events[0], scheduler.config.events[1]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        scheduler._handle_probe_result(ev_a, _make_result(available=False, blocked=True), now=base)
        scheduler._handle_probe_result(ev_b, _make_result(available=False), now=base)
        assert scheduler._event_state["ev-a"] == "impaired"
        assert scheduler._event_state["ev-b"] == "healthy"
        assert scheduler._cycle_bad_checks == 1
        assert scheduler._cycle_healthy_checks == 1

        # A loaded block/challenge page means Ticketmaster is not actually visible.
        scheduler.state.set_in_outage_state("ev-a", True)
        scheduler._record_event_uptime_heartbeats(base, "impaired", "blocked", gap=10)
        a_states = {s["state"] for s in scheduler._event_uptime["ev-a"].segments}
        b_states = {s["state"] for s in scheduler._event_uptime["ev-b"].segments}
        assert a_states == {"impaired"}
        assert b_states == {"healthy"}

    def test_no_internet_marks_all_events_down(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, self._two_event_config())
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._record_event_uptime_heartbeats(base, "down", "no_internet", gap=10)
        for led in scheduler._event_uptime.values():
            assert {s["state"] for s in led.segments} == {"down"}


class TestGlobalCheckGap:
    def test_gap_stays_within_configured_bounds(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_per_event_min_gap_between_checks_seconds=60,
                browser_per_event_max_gap_between_checks_seconds=120,
            ),
        )
        scheduler._rand = random.Random(1234)
        gaps = [scheduler._next_check_gap_seconds() for _ in range(100)]
        assert all(60.0 <= g <= 120.0 for g in gaps)
        # Actually randomized, not a constant.
        assert min(gaps) < max(gaps)

    def test_gap_is_constant_when_min_equals_max(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_per_event_min_gap_between_checks_seconds=90,
                browser_per_event_max_gap_between_checks_seconds=90,
            ),
        )
        assert scheduler._next_check_gap_seconds() == 90.0


class TestChallengeCircuitBreaker:
    @staticmethod
    def _result(*, challenge=False, retry_after=None, abck_flagged=False, abck_trusted=False):
        return SimpleNamespace(
            challenge_detected=challenge,
            abck_flagged=abck_flagged,
            abck_trusted=abck_trusted,
            raw_indicators={"retry_after_seconds": retry_after},
        )

    def test_cooldown_grows_exponentially(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_challenge_cooldown_base_seconds=60,
                browser_challenge_cooldown_max_seconds=1800,
                browser_challenge_cooldown_escalate_after=10,
            ),
        )
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for expected in (60, 120, 240):
            scheduler._update_challenge_cooldown(
                result=self._result(challenge=True), status=None, now=base
            )
            assert scheduler.state.get_challenge_cooldown_until() == base + timedelta(seconds=expected)

    def test_cooldown_is_capped(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_challenge_cooldown_base_seconds=600,
                browser_challenge_cooldown_max_seconds=1800,
                browser_challenge_cooldown_escalate_after=10,
            ),
        )
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(6):
            scheduler._update_challenge_cooldown(
                result=self._result(challenge=True), status=None, now=base
            )
        assert scheduler.state.get_challenge_cooldown_until() == base + timedelta(seconds=1800)

    def test_cooldown_escalates_to_quiet_tiers(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_challenge_cooldown_base_seconds=60,
                browser_challenge_cooldown_max_seconds=300,
                browser_challenge_cooldown_escalate_after=3,
                browser_challenge_cooldown_tiers_seconds=[300, 900, 1800],
                browser_challenge_cooldown_tier_every=2,
            ),
        )
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expected = [60, 120, 300, 300, 900, 900, 1800]
        for seconds in expected:
            scheduler._update_challenge_cooldown(
                result=self._result(challenge=True), status=None, now=base
            )
            assert scheduler.state.get_challenge_cooldown_until() == base + timedelta(seconds=seconds)

    def test_single_abck_flag_does_not_cooldown(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._update_challenge_cooldown(
            result=self._result(abck_flagged=True), status=None, now=base
        )
        # One flagged read is the cookie's normal post-load state — not enough to act.
        assert scheduler.state.get_challenge_cooldown_until() is None
        assert scheduler._consecutive_abck_flagged == 1

    def test_sustained_abck_flag_triggers_early_cooldown(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(2):  # reaches ABCK_FLAG_THRESHOLD
            scheduler._update_challenge_cooldown(
                result=self._result(abck_flagged=True), status=None, now=base
            )
        assert scheduler.state.get_challenge_cooldown_until() is not None

    def test_abck_trusted_read_recovers(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(2):
            scheduler._update_challenge_cooldown(
                result=self._result(abck_flagged=True), status=None, now=base
            )
        assert scheduler.state.get_challenge_cooldown_until() is not None
        # A trusted (~0~) read is the recovery gate — it clears the cooldown.
        scheduler._update_challenge_cooldown(
            result=self._result(abck_trusted=True), status=200, now=base
        )
        assert scheduler.state.get_challenge_cooldown_until() is None
        assert scheduler._consecutive_abck_flagged == 0

    def test_clean_check_resets_cooldown(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._update_challenge_cooldown(
            result=self._result(challenge=True), status=None, now=base
        )
        assert scheduler.state.get_challenge_cooldown_until() is not None

        scheduler._update_challenge_cooldown(
            result=self._result(challenge=False), status=200, now=base
        )
        assert scheduler.state.get_challenge_cooldown_until() is None
        assert scheduler._consecutive_challenges == 0

    def test_429_with_retry_after_sets_floor(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_challenge_cooldown_base_seconds=60,
                browser_challenge_cooldown_max_seconds=1800,
            ),
        )
        scheduler._rand = _FixedRand()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 429 counts as a challenge even without the challenge flag; Retry-After (300s)
        # exceeds the base 60s cooldown and becomes the floor.
        scheduler._update_challenge_cooldown(
            result=self._result(challenge=False, retry_after=300), status=429, now=base
        )
        assert scheduler.state.get_challenge_cooldown_until() == base + timedelta(seconds=300)

    def test_apply_cooldown_extends_sleep_but_clamps(self, tmp_path):
        from src.scheduler import CHALLENGE_COOLDOWN_SLEEP_CAP_SECONDS

        scheduler = _make_scheduler(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # A long cooldown is clamped to the per-sleep cap so the loop keeps re-evaluating
        # the manual-action escalation (the cooldown itself persists in state).
        scheduler.state.set_challenge_cooldown_until(base + timedelta(seconds=400))
        assert scheduler._apply_challenge_cooldown(30.0, base) == CHALLENGE_COOLDOWN_SLEEP_CAP_SECONDS

        # A short remaining cooldown is honored as-is.
        scheduler.state.set_challenge_cooldown_until(base + timedelta(seconds=45))
        assert scheduler._apply_challenge_cooldown(30.0, base) == 45.0

        scheduler.state.set_challenge_cooldown_until(None)
        assert scheduler._apply_challenge_cooldown(30.0, base) == 30.0


class TestCriticalAlertDelivery:
    """The banner must reflect ACTUAL Discord delivery; a failed send must retry."""

    @staticmethod
    def _degraded(tmp_path, *, delivered: bool):
        # logged_out is the only reason that pings — use it to exercise delivery.
        scheduler = _make_scheduler(tmp_path, _make_config(alerts_manual_action_after_seconds=900))
        scheduler.state.set_session_logged_out(True)
        scheduler.notifier.send_critical_attention.return_value = delivered
        return scheduler

    def test_failed_send_does_not_claim_delivery_and_retries(self, tmp_path):
        scheduler = self._degraded(tmp_path, delivered=False)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._evaluate_manual_action_escalation(base)  # arm attention_since
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901))

        assert scheduler.notifier.send_critical_attention.call_count == 1
        assert scheduler.state.get_attention_alert_delivered() is False
        assert scheduler.state.get_attention_alert_attempts() == 1

        # Still not delivered → retries next cycle.
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=950))
        assert scheduler.notifier.send_critical_attention.call_count == 2

    def test_delivered_send_stops_retrying(self, tmp_path):
        scheduler = self._degraded(tmp_path, delivered=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901))

        assert scheduler.state.get_attention_alert_delivered() is True
        assert scheduler.notifier.send_critical_attention.call_count == 1

        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=950))
        assert scheduler.notifier.send_critical_attention.call_count == 1

    def test_retries_are_capped(self, tmp_path):
        from src.scheduler import MAX_ATTENTION_ALERT_ATTEMPTS

        scheduler = self._degraded(tmp_path, delivered=False)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._evaluate_manual_action_escalation(base)
        for i in range(MAX_ATTENTION_ALERT_ATTEMPTS + 3):
            scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901 + i))

        assert scheduler.notifier.send_critical_attention.call_count == MAX_ATTENTION_ALERT_ATTEMPTS

    def test_recovery_resets_delivery_state(self, tmp_path):
        scheduler = self._degraded(tmp_path, delivered=False)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._evaluate_manual_action_escalation(base)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=901))
        assert scheduler.state.get_attention_alert_attempts() == 1

        scheduler.state.set_session_logged_out(False)
        scheduler._evaluate_manual_action_escalation(base + timedelta(seconds=950))

        assert scheduler.state.get_attention_alert_attempts() == 0
        assert scheduler.state.get_attention_alert_delivered() is False
        assert scheduler.state.get_attention_alerted() is False


class TestStartupWarmup:
    @staticmethod
    def _blocked():
        return _make_result(
            available=False, blocked=True, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )

    def test_blind_during_warmup_does_not_trip_outage(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(browser_startup_grace_seconds=300, browser_challenge_threshold=2),
        )
        event = scheduler.config.events[0]
        now = scheduler.start_time + timedelta(seconds=10)  # inside warmup
        for _ in range(5):
            scheduler._handle_probe_result(event, self._blocked(), now=now)
        assert scheduler.state.get_in_outage_state(event.event_id) is False

    def test_blind_after_warmup_trips_outage(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(browser_startup_grace_seconds=30, browser_challenge_threshold=2),
        )
        event = scheduler.config.events[0]
        now = scheduler.start_time + timedelta(seconds=60)  # past warmup
        for _ in range(3):
            scheduler._handle_probe_result(event, self._blocked(), now=now)
        assert scheduler.state.get_in_outage_state(event.event_id) is True

    def test_challenge_cooldown_stays_at_base_during_warmup(self, tmp_path):
        scheduler = _make_scheduler(
            tmp_path,
            _make_config(
                browser_startup_grace_seconds=300,
                browser_challenge_cooldown_base_seconds=60,
                browser_challenge_cooldown_max_seconds=1800,
            ),
        )
        scheduler._rand = _FixedRand()
        now = scheduler.start_time + timedelta(seconds=10)
        result = SimpleNamespace(challenge_detected=True, raw_indicators={"retry_after_seconds": None})
        for _ in range(4):
            scheduler._update_challenge_cooldown(result=result, status=None, now=now)
        # No exponential growth during warmup — stays at base.
        assert scheduler.state.get_challenge_cooldown_until() == now + timedelta(seconds=60)


class TestDetailRetryCadence:
    def test_needs_detail_retry(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        # Complete sighting (price+section+listing all parsed) → no retry.
        assert scheduler._needs_detail_retry(_make_result(available=True)) is False
        # Sighting with a missing detail → retry.
        incomplete = _make_result(available=True)
        incomplete.price_summary = None
        assert scheduler._needs_detail_retry(incomplete) is True
        incomplete = _make_result(available=True)
        incomplete.listing_summary = None
        assert scheduler._needs_detail_retry(incomplete) is True
        # No availability → never a retry, regardless of missing fields.
        assert scheduler._needs_detail_retry(_make_result(available=False)) is False
        assert scheduler._needs_detail_retry(None) is False

    def test_incomplete_sighting_schedules_quick_retry(self, tmp_path):
        from src.scheduler import (
            DETAIL_RETRY_MAX_ATTEMPTS,
            DETAIL_RETRY_MAX_SECONDS,
            DETAIL_RETRY_MIN_SECONDS,
        )
        scheduler = _make_scheduler(tmp_path)
        event_id = scheduler.config.events[0].event_id
        incomplete = _make_result(available=True)
        incomplete.price_summary = None
        now = datetime.now(timezone.utc)

        for attempt in range(1, DETAIL_RETRY_MAX_ATTEMPTS + 1):
            scheduler._reschedule_event(event_id, now, incomplete)
            delay = (scheduler._event_schedule[event_id] - now).total_seconds()
            assert DETAIL_RETRY_MIN_SECONDS <= delay <= DETAIL_RETRY_MAX_SECONDS
            assert scheduler._detail_retry_counts[event_id] == attempt

        # Budget exhausted → back to the normal randomized cadence.
        scheduler._reschedule_event(event_id, now, incomplete)
        delay = (scheduler._event_schedule[event_id] - now).total_seconds()
        assert delay >= scheduler.config.browser_per_event_poll_min_seconds

    def test_complete_sighting_stays_on_normal_cadence(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        event_id = scheduler.config.events[0].event_id
        scheduler._detail_retry_counts[event_id] = 2
        now = datetime.now(timezone.utc)
        scheduler._reschedule_event(event_id, now, _make_result(available=True))
        delay = (scheduler._event_schedule[event_id] - now).total_seconds()
        # Complete sighting → no fast polling and the retry budget resets.
        assert delay >= scheduler.config.browser_per_event_poll_min_seconds
        assert event_id not in scheduler._detail_retry_counts

    def test_sighting_does_not_reset_backoff(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._cadence_backoff = 4.0
        scheduler._handle_probe_result(scheduler.config.events[0], _make_result(available=True))
        assert scheduler._cadence_backoff == 4.0

    def test_blind_check_does_not_reset_backoff(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._cadence_backoff = 4.0
        blind = _make_result(
            available=False, blocked=True, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        scheduler._handle_probe_result(scheduler.config.events[0], blind)
        assert scheduler._cadence_backoff == 4.0


class TestVariationProbeHook:
    def test_escalated_challenges_trigger_probe(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._maybe_run_variation_probe = MagicMock()
        now = scheduler.start_time + timedelta(hours=1)  # past startup warmup
        result = SimpleNamespace(
            challenge_detected=True,
            raw_indicators={},
            abck_flagged=False,
            abck_trusted=False,
        )
        escalate_after = scheduler.config.browser_challenge_cooldown_escalate_after
        for _ in range(escalate_after):
            scheduler._update_challenge_cooldown(result=result, status=None, now=now)
        scheduler._maybe_run_variation_probe.assert_called_once_with(now)

    def test_probe_is_rate_limited(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        now = datetime.now(timezone.utc)
        report = MagicMock()
        report.scope = "none"
        report.to_dict.return_value = {"scope": "none"}
        with patch("src.variation_probe.run_variation_matrix", return_value=report) as mock_run:
            scheduler._maybe_run_variation_probe(now)
            scheduler._maybe_run_variation_probe(now + timedelta(seconds=60))
        mock_run.assert_called_once()
        assert scheduler.state.get_variation_probe_report() == {"scope": "none"}

    def test_probe_disabled_by_config(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.config.browser_variation_probe_enabled = False
        with patch("src.variation_probe.run_variation_matrix") as mock_run:
            scheduler._maybe_run_variation_probe(datetime.now(timezone.utc))
        mock_run.assert_not_called()

    def test_profile_scope_clears_block_cookies(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.probe.clear_block_cookies = MagicMock(return_value=2)
        scheduler._apply_variation_probe_remedy("profile")
        scheduler.probe.clear_block_cookies.assert_called_once()

    def test_non_profile_scopes_do_not_touch_cookies(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.probe.clear_block_cookies = MagicMock()
        for scope in ("account", "ip_device", "none", "unknown"):
            scheduler._apply_variation_probe_remedy(scope)
        scheduler.probe.clear_block_cookies.assert_not_called()


class TestUptimeConnectivity:
    def test_is_connectivity_error_detects_net_errors(self):
        assert _is_connectivity_error(
            BrowserProbeError("Browser probe failed: net::ERR_INTERNET_DISCONNECTED at http://x")
        )
        assert _is_connectivity_error(BrowserProbeError("net::ERR_NAME_NOT_RESOLVED"))
        # Follows the __cause__ chain too.
        cause = RuntimeError("page.goto: net::ERR_CONNECTION_REFUSED")
        exc = BrowserProbeError("probe failed")
        exc.__cause__ = cause
        assert _is_connectivity_error(exc)

    def test_is_connectivity_error_ignores_blocks_and_timeouts(self):
        # A block page / http error is NOT a connectivity loss (page still loaded).
        assert not _is_connectivity_error(BrowserProbeError("http_403 forbidden"))
        assert not _is_connectivity_error(BrowserProbeError("Timeout 20000ms exceeded"))

    def test_no_internet_cycle_records_down(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.probe.check_event = MagicMock(
            side_effect=BrowserProbeError(
                "Browser probe failed for event-1: net::ERR_INTERNET_DISCONNECTED"
            )
        )
        scheduler._run_cycle()
        assert scheduler._cycle_connectivity_down is True

        scheduler._record_uptime_heartbeat(True)
        last = scheduler.uptime.segments[-1]
        assert last["state"] == "down"
        assert last["reason"] == "no_internet"

    def test_block_page_records_impaired_uptime(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        blocked = _make_result(
            available=False, blocked=True, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        blocked.raw_indicators["response_status"] = 403
        scheduler.probe.check_event = MagicMock(return_value=blocked)
        needs_slow_retry = scheduler._run_cycle()
        assert scheduler._cycle_connectivity_down is False
        assert scheduler._cycle_bad_checks == 1
        assert scheduler._cycle_healthy_checks == 0

        scheduler._record_uptime_heartbeat(needs_slow_retry)
        last = scheduler.uptime.segments[-1]
        assert last["state"] == "impaired"
        assert last["reason"] == "blocked"

    def test_unhealthy_no_signal_response_records_impaired(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        no_signal = _make_result(
            available=False, blocked=False, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        no_signal.raw_indicators["response_status"] = 500
        scheduler.probe.check_event = MagicMock(return_value=no_signal)
        needs_slow_retry = scheduler._run_cycle()
        assert scheduler._cycle_connectivity_down is False
        assert scheduler._cycle_bad_checks == 1
        assert scheduler._cycle_healthy_checks == 0

        scheduler._record_uptime_heartbeat(needs_slow_retry)
        last = scheduler.uptime.segments[-1]
        assert last["state"] == "impaired"
        assert last["reason"] == "blocked"

    def test_clean_soldout_cycle_records_healthy(self, tmp_path):
        # Sold-out (available=False) but a clean, unblocked scan → healthy.
        scheduler = _make_scheduler(tmp_path)
        clean = _make_result(
            available=False, blocked=False, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        scheduler.probe.check_event = MagicMock(return_value=clean)
        needs_slow_retry = scheduler._run_cycle()
        scheduler._record_uptime_heartbeat(needs_slow_retry)
        assert scheduler.uptime.segments[-1]["state"] == "healthy"

    def test_clean_cycle_is_healthy_despite_lingering_needs_slow_retry(self, tmp_path):
        # A stale outage flag on some event makes _run_cycle return needs_slow_retry
        # even though this cycle's checks came back clean. Uptime must still be healthy.
        scheduler = _make_scheduler(tmp_path)
        clean = _make_result(
            available=False, blocked=False, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        scheduler.probe.check_event = MagicMock(return_value=clean)
        scheduler._run_cycle()  # tallies one healthy check this cycle
        # Simulate a lagging cadence signal that used to force impaired.
        scheduler._record_uptime_heartbeat(needs_slow_retry=True)
        assert scheduler.uptime.segments[-1]["state"] == "healthy"

    def test_logged_out_flag_with_healthy_cycle_records_healthy(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        clean = _make_result(
            available=False, blocked=False, signal_type=ProbeSignalType.NONE, dom_signals=[]
        )
        scheduler.probe.check_event = MagicMock(return_value=clean)
        scheduler.state.set_session_logged_out(True)
        scheduler._run_cycle()
        scheduler._record_uptime_heartbeat(needs_slow_retry=False)
        last = scheduler.uptime.segments[-1]
        assert last["state"] == "healthy"
        assert last["reason"] is None

    def test_logged_out_session_without_checks_records_impaired(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler.state.set_session_logged_out(True)
        scheduler._cycle_healthy_checks = 0
        scheduler._cycle_bad_checks = 0

        scheduler._record_uptime_heartbeat(needs_slow_retry=False)

        last = scheduler.uptime.segments[-1]
        assert last["state"] == "impaired"
        assert last["reason"] == "logged_out"


class TestStalenessCooldownInteraction:
    def test_poll_staleness_skipped_during_challenge_cooldown(self, tmp_path):
        """A live challenge cooldown is planned quiet — it must not read as staleness
        (which would recycle the browser straight back into the block)."""
        config = _make_config(alerts_event_check_stale_seconds=30)
        scheduler = _make_scheduler(tmp_path, config=config)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scheduler._uptime_anchor = now - timedelta(hours=1)
        event_id = config.events[0].event_id
        scheduler.state._event(event_id)["last_check"] = (now - timedelta(seconds=90)).isoformat()
        scheduler.state.set_challenge_cooldown_until(now + timedelta(seconds=120))

        assert scheduler._check_event_poll_staleness(now=now) is False
        assert event_id not in scheduler._stale_event_alerted
        scheduler.probe.close.assert_not_called()

        # Once the cooldown lapses, the same lag counts as stale again.
        later = now + timedelta(seconds=121)
        scheduler.state._event(event_id)["last_check"] = (later - timedelta(seconds=90)).isoformat()
        assert scheduler._check_event_poll_staleness(now=later) is True


class TestStartingHeartbeat:
    def test_record_starting_heartbeat_marks_impaired(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        scheduler._record_starting_heartbeat()

        last = scheduler.uptime.segments[-1]
        assert last["state"] == "impaired"
        assert last["reason"] == "starting"
        for ledger in scheduler._event_uptime.values():
            last_ev = ledger.segments[-1]
            assert last_ev["state"] == "impaired"
            assert last_ev["reason"] == "starting"
