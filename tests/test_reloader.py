"""Tests for local code-change reloader logic."""

from __future__ import annotations

from scripts import reloader
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
        updates_stability_delay_seconds=0,
        updates_watch_globs=["*.txt"],
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


def test_compute_fingerprint_changes_with_file_contents(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    fp1 = reloader.compute_fingerprint(tmp_path, ["*.txt"])
    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    fp2 = reloader.compute_fingerprint(tmp_path, ["*.txt"])
    assert fp1 != fp2


def test_run_reloader_sets_initial_baseline(tmp_path, monkeypatch):
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    (tmp_path / "watch.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(reloader, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(reloader, "MonitorState", lambda: state)
    monkeypatch.setattr(reloader, "DiscordNotifier", lambda **kwargs: notifier)

    exit_code = reloader.run_reloader(config=cfg, config_path="config.yaml")
    assert exit_code == 0
    assert state.get_last_code_fingerprint()
    assert notifier.auto_fix_calls == []


def test_run_reloader_restarts_on_change_when_preflight_passes(tmp_path, monkeypatch):
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    watched = tmp_path / "watch.txt"
    watched.write_text("one", encoding="utf-8")

    monkeypatch.setattr(reloader, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(reloader, "MonitorState", lambda: state)
    monkeypatch.setattr(reloader, "DiscordNotifier", lambda **kwargs: notifier)
    state.set_last_code_fingerprint(reloader.compute_fingerprint(tmp_path, cfg.updates_watch_globs))

    watched.write_text("two", encoding="utf-8")
    monkeypatch.setattr(reloader, "_run_doctor_lite", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(reloader, "_restart_service", lambda: True)

    exit_code = reloader.run_reloader(config=cfg, config_path="config.yaml")
    assert exit_code == 0
    assert len(notifier.auto_fix_calls) == 1


def test_run_reloader_skips_restart_on_preflight_failure(tmp_path, monkeypatch):
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    watched = tmp_path / "watch.txt"
    watched.write_text("one", encoding="utf-8")

    monkeypatch.setattr(reloader, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(reloader, "MonitorState", lambda: state)
    monkeypatch.setattr(reloader, "DiscordNotifier", lambda **kwargs: notifier)
    state.set_last_code_fingerprint(reloader.compute_fingerprint(tmp_path, cfg.updates_watch_globs))

    watched.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(reloader, "_run_doctor_lite", lambda *_args, **_kwargs: (False, "bad config"))
    monkeypatch.setattr(reloader, "_restart_service", lambda: False)

    exit_code = reloader.run_reloader(config=cfg, config_path="config.yaml")
    assert exit_code == 1
    assert len(notifier.critical_calls) == 1
