"""Tests for config loading and validation."""

from __future__ import annotations

import pytest
import yaml

from src.config import load_config


def _write_config(tmp_path, overrides=None):
    """Write a valid config file with optional overrides."""
    config = {
        "discord": {"webhook_url": "https://discord.com/api/webhooks/test"},
        "events": [
            {
                "event_id": "vvG1IZ9YbmdXqt",
                "name": "Test Event",
                "date": "2026-07-28",
                "url": "https://ticketmaster.com/event/test",
            }
        ],
        "polling": {"timezone": "US/Eastern"},
    }
    if overrides:
        for key, val in overrides.items():
            parts = key.split(".")
            target = config
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = val

    path = str(tmp_path / "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f)
    return path


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        path = _write_config(tmp_path)
        config = load_config(path)
        assert len(config.events) == 1
        assert config.events[0].name == "Test Event"

    def test_env_var_overrides_webhook(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://env-webhook")
        config = load_config(path)
        assert config.discord_webhook_url == "https://env-webhook"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_missing_events_exits(self, tmp_path):
        config_data = {
            "discord": {"webhook_url": "https://discord.com/api/webhooks/test"},
            "events": [],
        }
        path = str(tmp_path / "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)
        with pytest.raises(SystemExit):
            load_config(path)

    def test_invalid_timezone_exits(self, tmp_path):
        path = _write_config(tmp_path, {"polling.timezone": "US/Hogwarts"})
        with pytest.raises(SystemExit):
            load_config(path)

    def test_invalid_interval_exits(self, tmp_path):
        path = _write_config(tmp_path, {"browser.poll_interval_seconds": "fast"})
        with pytest.raises(SystemExit):
            load_config(path)

    def test_defaults_applied(self, tmp_path):
        path = _write_config(tmp_path)
        config = load_config(path)
        assert config.browser_poll_interval_seconds == 12
        assert config.browser_poll_jitter_seconds == 2
        assert config.browser_challenge_threshold == 5
        assert config.browser_session_mode == "storage_state"
        assert config.browser_user_data_dir == "secrets/tm_profile"
        assert config.browser_channel == ""
        assert config.browser_cdp_endpoint_url == "http://127.0.0.1:9222"
        assert config.browser_cdp_connect_timeout_seconds == 10
        assert config.browser_reuse_event_tabs is True
        assert config.browser_poll_min_seconds == 45
        assert config.browser_poll_max_seconds == 60
        assert config.browser_host_enabled is False
        assert config.browser_host_chrome_executable_path.endswith("/Google Chrome")
        assert config.browser_host_user_data_dir == "secrets/tm_chrome_profile"
        assert config.browser_host_remote_debugging_port == 9222
        assert config.alerts_ticket_cooldown_seconds == 180
        assert config.self_heal_browser_restart_threshold == 3
        assert config.alerts_event_check_stale_seconds == 180
        assert config.alerts_operational_state_cooldown_seconds == 1800
        assert config.auth_auto_login_enabled is False
        assert config.auth_keychain_service == "ticket-monitor"
        assert config.auth_keychain_email_account == "ticketmaster-email"
        assert config.auth_keychain_password_account == "ticketmaster-password"
        assert config.auth_max_auto_login_attempts_per_hour == 3
        assert config.auth_auto_login_cooldown_seconds == 1800
        assert config.watchdog_interval_seconds == 120
        assert config.updates_interval_seconds == 60
        assert config.timezone == "US/Eastern"

    def test_auto_generates_event_url(self, tmp_path):
        config_data = {
            "discord": {"webhook_url": "https://discord.com/api/webhooks/test"},
            "events": [{"event_id": "abc123", "name": "Test"}],
        }
        path = str(tmp_path / "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)
        config = load_config(path)
        assert config.events[0].url == "https://www.ticketmaster.com/event/abc123"

    def test_invalid_auth_max_attempts_exits(self, tmp_path):
        path = _write_config(tmp_path, {"auth.max_auto_login_attempts_per_hour": 0})
        with pytest.raises(SystemExit):
            load_config(path)

    def test_invalid_auth_cooldown_exits(self, tmp_path):
        path = _write_config(tmp_path, {"auth.auto_login_cooldown_seconds": -1})
        with pytest.raises(SystemExit):
            load_config(path)

    def test_auto_login_requires_keychain_fields(self, tmp_path):
        path = _write_config(
            tmp_path,
            {
                "auth.auto_login_enabled": True,
                "auth.keychain_service": "",
            },
        )
        with pytest.raises(SystemExit):
            load_config(path)

    def test_invalid_browser_session_mode_exits(self, tmp_path):
        path = _write_config(tmp_path, {"browser.session_mode": "invalid"})
        with pytest.raises(SystemExit):
            load_config(path)

    def test_cdp_attach_mode_loads_with_defaults(self, tmp_path):
        path = _write_config(tmp_path, {"browser.session_mode": "cdp_attach"})
        config = load_config(path)
        assert config.browser_session_mode == "cdp_attach"
        assert config.browser_host_enabled is True

    def test_invalid_poll_min_max_exits(self, tmp_path):
        path = _write_config(
            tmp_path,
            {
                "browser.poll_min_seconds": 90,
                "browser.poll_max_seconds": 30,
            },
        )
        with pytest.raises(SystemExit):
            load_config(path)

    def test_persistent_profile_requires_user_data_dir(self, tmp_path):
        path = _write_config(
            tmp_path,
            {
                "browser.session_mode": "persistent_profile",
                "browser.user_data_dir": "",
            },
        )
        with pytest.raises(SystemExit):
            load_config(path)

    def test_invalid_event_check_stale_seconds_exits(self, tmp_path):
        path = _write_config(tmp_path, {"alerts.event_check_stale_seconds": 0})
        with pytest.raises(SystemExit):
            load_config(path)
