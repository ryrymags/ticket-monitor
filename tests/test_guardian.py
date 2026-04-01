"""Tests for external watchdog guardian logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts import guardian
from src.config import EventConfig, MonitorConfig
from src.preferences import TicketPreferences
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
        updates_watch_globs=["monitor.py"],
        timezone="US/Eastern",
        log_level="INFO",
        log_file="logs/test.log",
        log_max_file_size_mb=10,
        log_backup_count=3,
    )
    defaults.update(overrides)
    return MonitorConfig(**defaults)


class _FakeNotifier:
    def __init__(self, *args, **kwargs):
        del args, kwargs
        self.auto_fix_calls = []
        self.critical_calls = []

    def send_auto_fix_action(self, action: str, reason: str, **kwargs) -> bool:
        self.auto_fix_calls.append((action, reason, kwargs))
        return True

    def send_critical_attention(self, message: str, **kwargs) -> bool:
        self.critical_calls.append((message, kwargs))
        return True


def test_is_stale_logic(tmp_path):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    stale, age = guardian.is_stale(state=state, stale_after_seconds=180, now=now)
    assert stale is True
    assert age == float("inf")

    state.set_last_cycle_completed_at(now - timedelta(seconds=60))
    stale, age = guardian.is_stale(state=state, stale_after_seconds=180, now=now)
    assert stale is False
    assert age < 180


def test_run_guardian_healthy_does_nothing(tmp_path, monkeypatch):
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    state.set_last_cycle_completed_at(datetime.now(timezone.utc))
    notifier = _FakeNotifier()

    monkeypatch.setattr(guardian, "MonitorState", lambda: state)
    monkeypatch.setattr(guardian, "DiscordNotifier", lambda **kwargs: notifier)
    monkeypatch.setattr(guardian, "get_service_status", lambda: guardian.ServiceStatus(running=True, pid=1234))

    called = {"kickstart": 0}

    def _kickstart():
        called["kickstart"] += 1
        return True

    monkeypatch.setattr(guardian, "kickstart_service", _kickstart)

    exit_code = guardian.run_guardian(config=cfg, force_fix=False)
    assert exit_code == 0
    assert called["kickstart"] == 0
    assert notifier.auto_fix_calls == []


def test_run_guardian_unhealthy_attempts_fix(tmp_path, monkeypatch):
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()

    monkeypatch.setattr(guardian, "MonitorState", lambda: state)
    monkeypatch.setattr(guardian, "DiscordNotifier", lambda **kwargs: notifier)
    statuses = iter(
        [
            guardian.ServiceStatus(running=False, pid=None),
            guardian.ServiceStatus(running=True, pid=4321),
        ]
    )
    monkeypatch.setattr(guardian, "get_service_status", lambda: next(statuses))
    monkeypatch.setattr(guardian, "kickstart_service", lambda: True)
    monkeypatch.setattr(guardian, "kill_orphaned_playwright_processes", lambda *args, **kwargs: 0)

    exit_code = guardian.run_guardian(config=cfg, force_fix=False)
    assert exit_code == 0
    assert state.get_process_restart_requests_24h() >= 1
    assert len(notifier.auto_fix_calls) == 1


def test_run_guardian_still_attempts_liveness_fix_after_max_attempts(tmp_path, monkeypatch):
    cfg = _make_config(watchdog_max_fix_attempts_per_hour=1)
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    state.record_guardian_fix_attempt()
    state.set_last_cycle_completed_at(datetime.now(timezone.utc) - timedelta(seconds=600))

    monkeypatch.setattr(guardian, "MonitorState", lambda: state)
    monkeypatch.setattr(guardian, "DiscordNotifier", lambda **kwargs: notifier)
    monkeypatch.setattr(guardian, "get_service_status", lambda: guardian.ServiceStatus(running=False, pid=None))
    monkeypatch.setattr(guardian, "kill_orphaned_playwright_processes", lambda *args, **kwargs: 0)
    monkeypatch.setattr(guardian, "kickstart_service", lambda: True)

    exit_code = guardian.run_guardian(config=cfg, force_fix=False)
    assert exit_code == 1
    assert state.get_guardian_pause_until() is None
    assert len(notifier.critical_calls) == 0
    assert len(notifier.auto_fix_calls) == 1


def test_run_guardian_pauses_after_max_attempts_for_error_burst(tmp_path, monkeypatch):
    cfg = _make_config(
        watchdog_max_fix_attempts_per_hour=1,
        self_heal_process_restart_threshold=1,
    )
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    state.record_guardian_fix_attempt()
    state.record_browser_restart()
    state.increment_consecutive_blocked("event-1")
    state.set_last_cycle_completed_at(datetime.now(timezone.utc))

    monkeypatch.setattr(guardian, "MonitorState", lambda: state)
    monkeypatch.setattr(guardian, "DiscordNotifier", lambda **kwargs: notifier)
    monkeypatch.setattr(guardian, "get_service_status", lambda: guardian.ServiceStatus(running=True, pid=1234))

    exit_code = guardian.run_guardian(config=cfg, force_fix=False)
    assert exit_code == 1
    assert state.get_guardian_pause_until() is not None
    assert len(notifier.critical_calls) == 1
