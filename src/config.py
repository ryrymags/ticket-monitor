"""Configuration loader and validator."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml
from dateutil import tz

from .preferences import TicketPreferences

logger = logging.getLogger(__name__)


@dataclass
class EventConfig:
    event_id: str
    name: str
    date: str
    url: str


@dataclass
class MonitorConfig:
    # Discord
    discord_webhook_url: str
    discord_username: str
    discord_ping_user_id: str

    # Events
    events: list[EventConfig]

    # Browser probe
    browser_storage_state_path: str
    browser_session_mode: str
    browser_user_data_dir: str
    browser_channel: str
    browser_cdp_endpoint_url: str
    browser_cdp_connect_timeout_seconds: int
    browser_reuse_event_tabs: bool
    browser_poll_min_seconds: int
    browser_poll_max_seconds: int
    browser_headless: bool
    browser_poll_interval_seconds: int
    browser_poll_jitter_seconds: int
    browser_navigation_timeout_seconds: int
    browser_challenge_threshold: int
    browser_challenge_retry_seconds: int
    event_stagger_seconds: int
    browser_host_enabled: bool
    browser_host_chrome_executable_path: str
    browser_host_user_data_dir: str
    browser_host_remote_debugging_port: int

    # Alerts / detection
    alerts_ticket_cooldown_seconds: int
    alerts_operational_heartbeat_hours: int
    alerts_event_check_stale_seconds: int
    alerts_operational_state_cooldown_seconds: int

    # Retry
    backoff_multiplier: float
    max_backoff_seconds: int

    # Self healing
    self_heal_browser_restart_threshold: int
    self_heal_browser_restart_window_seconds: int
    self_heal_process_restart_threshold: int
    self_heal_process_restart_window_seconds: int
    self_heal_error_alert_cooldown_seconds: int

    # Auth session auto-fix
    auth_auto_login_enabled: bool
    auth_keychain_service: str
    auth_keychain_email_account: str
    auth_keychain_password_account: str
    auth_max_auto_login_attempts_per_hour: int
    auth_auto_login_cooldown_seconds: int
    auth_session_health_check_interval_seconds: int
    auth_session_health_check_url: str

    # Watchdog
    watchdog_enabled: bool
    watchdog_interval_seconds: int
    watchdog_stale_after_seconds: int
    watchdog_max_fix_attempts_per_hour: int

    # Local updates
    updates_enabled: bool
    updates_interval_seconds: int
    updates_stability_delay_seconds: int
    updates_watch_globs: list[str]

    # Ticket preferences (configurable bingo rules)
    preferences: TicketPreferences

    # General
    timezone: str
    log_level: str
    log_file: str
    log_max_file_size_mb: int
    log_backup_count: int


def load_config(path: str = "config.yaml") -> MonitorConfig:
    """Load and validate configuration from a YAML file."""
    if not os.path.exists(path):
        print(f"Error: Config file not found: {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    errors: list[str] = []

    # Required top-level keys
    discord = raw.get("discord", {})
    events_raw = raw.get("events", [])
    browser = raw.get("browser", {})
    alerts = raw.get("alerts", {})
    polling = raw.get("polling", {})
    logging_cfg = raw.get("logging", {})
    self_heal = raw.get("self_heal", {})
    auth = raw.get("auth", {})
    watchdog = raw.get("watchdog", {})
    updates = raw.get("updates", {})
    browser_host = raw.get("browser_host", {})

    # Backward compat: ignore legacy API keys if present in old config files
    if raw.get("ticketmaster", {}).get("api_key") or os.environ.get("TM_API_KEY"):
        logger.info("Ignoring legacy ticketmaster.api_key (Discovery API is no longer used)")
    if raw.get("optional", {}).get("discovery_telemetry_enabled") is not None:
        logger.info("Ignoring legacy optional.discovery_telemetry_enabled (Discovery API is no longer used)")

    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or discord.get("webhook_url", "")).strip()
    if not webhook_url or webhook_url == "YOUR_WEBHOOK_URL_HERE":
        errors.append("discord.webhook_url is required — create one in Server Settings > Integrations > Webhooks")

    if not events_raw:
        errors.append("events: at least one event must be configured")

    # Parse events
    events: list[EventConfig] = []
    for i, ev in enumerate(events_raw):
        eid = str(ev.get("event_id", "")).strip()
        ename = str(ev.get("name", f"Event {i + 1}")).strip()
        edate = str(ev.get("date", "")).strip()
        eurl = str(ev.get("url", "")).strip()
        if not eid:
            errors.append(f"events[{i}].event_id is required")
        if not eurl and eid:
            eurl = f"https://www.ticketmaster.com/event/{eid}"
        events.append(EventConfig(event_id=eid, name=ename, date=edate, url=eurl))

    # Safe type conversion helpers — collect errors instead of crashing
    def safe_int(section: dict[str, Any], key: str, default: int, label: str) -> int:
        val = section.get(key, default)
        try:
            return int(val)
        except (ValueError, TypeError):
            errors.append(f"{label} must be an integer, got: {val!r}")
            return default

    def safe_float(section: dict[str, Any], key: str, default: float, label: str) -> float:
        val = section.get(key, default)
        try:
            return float(val)
        except (ValueError, TypeError):
            errors.append(f"{label} must be a number, got: {val!r}")
            return default

    def safe_bool(section: dict[str, Any], key: str, default: bool) -> bool:
        val = section.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in {"true", "1", "yes", "y", "on"}
        return bool(val)

    # Validate timezone
    timezone_str = str(polling.get("timezone", "US/Eastern"))
    if tz.gettz(timezone_str) is None:
        errors.append(f"polling.timezone is invalid: {timezone_str!r}")

    # Browser
    storage_state_path = str(browser.get("storage_state_path", "secrets/tm_storage_state.json")).strip()
    browser_session_mode = str(browser.get("session_mode", "storage_state")).strip().lower()
    browser_user_data_dir = str(browser.get("user_data_dir", "secrets/tm_profile")).strip()
    browser_channel = str(browser.get("channel", "chrome")).strip()
    browser_cdp_endpoint_url = str(browser.get("cdp_endpoint_url", "http://127.0.0.1:9222")).strip()
    browser_cdp_connect_timeout_seconds = safe_int(
        browser,
        "cdp_connect_timeout_seconds",
        10,
        "browser.cdp_connect_timeout_seconds",
    )
    browser_reuse_event_tabs = safe_bool(browser, "reuse_event_tabs", True)
    browser_poll_min_seconds = safe_int(browser, "poll_min_seconds", 45, "browser.poll_min_seconds")
    browser_poll_max_seconds = safe_int(browser, "poll_max_seconds", 60, "browser.poll_max_seconds")

    browser_headless = safe_bool(browser, "headless", True)
    browser_poll_interval_seconds = safe_int(
        browser, "poll_interval_seconds", 12, "browser.poll_interval_seconds"
    )
    browser_poll_jitter_seconds = safe_int(
        browser, "poll_jitter_seconds", 2, "browser.poll_jitter_seconds"
    )
    browser_navigation_timeout_seconds = safe_int(
        browser, "navigation_timeout_seconds", 20, "browser.navigation_timeout_seconds"
    )
    browser_challenge_threshold = safe_int(
        browser, "challenge_threshold", 5, "browser.challenge_threshold"
    )
    browser_challenge_retry_seconds = safe_int(
        browser, "challenge_retry_seconds", 60, "browser.challenge_retry_seconds"
    )
    event_stagger_seconds = safe_int(browser, "event_stagger_seconds", 6, "browser.event_stagger_seconds")
    browser_host_enabled = safe_bool(browser_host, "enabled", browser_session_mode == "cdp_attach")
    browser_host_chrome_executable_path = str(
        browser_host.get(
            "chrome_executable_path",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
    ).strip()
    browser_host_user_data_dir = str(
        browser_host.get("user_data_dir", "secrets/tm_chrome_profile")
    ).strip()
    browser_host_remote_debugging_port = safe_int(
        browser_host,
        "remote_debugging_port",
        9222,
        "browser_host.remote_debugging_port",
    )

    # Alerts
    alerts_ticket_cooldown_seconds = safe_int(
        alerts, "ticket_cooldown_seconds", 180, "alerts.ticket_cooldown_seconds"
    )
    alerts_operational_heartbeat_hours = safe_int(
        alerts, "operational_heartbeat_hours", 6, "alerts.operational_heartbeat_hours"
    )
    alerts_event_check_stale_seconds = safe_int(
        alerts, "event_check_stale_seconds", 180, "alerts.event_check_stale_seconds"
    )
    alerts_operational_state_cooldown_seconds = safe_int(
        alerts,
        "operational_state_cooldown_seconds",
        1800,
        "alerts.operational_state_cooldown_seconds",
    )

    # Retry
    backoff_multiplier = safe_float(polling, "backoff_multiplier", 2.0, "polling.backoff_multiplier")
    max_backoff_seconds = safe_int(polling, "max_backoff_seconds", 120, "polling.max_backoff_seconds")

    # Self healing
    self_heal_browser_restart_threshold = safe_int(
        self_heal, "browser_restart_threshold", 3, "self_heal.browser_restart_threshold"
    )
    self_heal_browser_restart_window_seconds = safe_int(
        self_heal,
        "browser_restart_window_seconds",
        600,
        "self_heal.browser_restart_window_seconds",
    )
    self_heal_process_restart_threshold = safe_int(
        self_heal, "process_restart_threshold", 6, "self_heal.process_restart_threshold"
    )
    self_heal_process_restart_window_seconds = safe_int(
        self_heal,
        "process_restart_window_seconds",
        1800,
        "self_heal.process_restart_window_seconds",
    )
    self_heal_error_alert_cooldown_seconds = safe_int(
        self_heal,
        "error_alert_cooldown_seconds",
        1800,
        "self_heal.error_alert_cooldown_seconds",
    )

    # Auth session auto-fix
    auth_auto_login_enabled = safe_bool(auth, "auto_login_enabled", False)
    auth_keychain_service = str(auth.get("keychain_service", "ticket-monitor")).strip()
    auth_keychain_email_account = str(auth.get("keychain_email_account", "ticketmaster-email")).strip()
    auth_keychain_password_account = str(auth.get("keychain_password_account", "ticketmaster-password")).strip()
    auth_max_auto_login_attempts_per_hour = safe_int(
        auth,
        "max_auto_login_attempts_per_hour",
        3,
        "auth.max_auto_login_attempts_per_hour",
    )
    auth_auto_login_cooldown_seconds = safe_int(
        auth,
        "auto_login_cooldown_seconds",
        1800,
        "auth.auto_login_cooldown_seconds",
    )
    auth_session_health_check_interval_seconds = safe_int(
        auth,
        "session_health_check_interval_seconds",
        3600,
        "auth.session_health_check_interval_seconds",
    )
    auth_session_health_check_url = str(
        auth.get("session_health_check_url", "https://www.ticketmaster.com/my-account")
    ).strip()

    # Watchdog
    watchdog_enabled = safe_bool(watchdog, "enabled", True)
    watchdog_interval_seconds = safe_int(watchdog, "interval_seconds", 120, "watchdog.interval_seconds")
    watchdog_stale_after_seconds = safe_int(watchdog, "stale_after_seconds", 180, "watchdog.stale_after_seconds")
    watchdog_max_fix_attempts_per_hour = safe_int(
        watchdog,
        "max_fix_attempts_per_hour",
        6,
        "watchdog.max_fix_attempts_per_hour",
    )

    # Updates
    updates_enabled = safe_bool(updates, "enabled", True)
    updates_interval_seconds = safe_int(updates, "interval_seconds", 60, "updates.interval_seconds")
    updates_stability_delay_seconds = safe_int(
        updates, "stability_delay_seconds", 20, "updates.stability_delay_seconds"
    )
    raw_watch_globs = updates.get(
        "watch_globs",
        [
            "monitor.py",
            "src/**/*.py",
            "config.yaml",
            "requirements.txt",
            "pyproject.toml",
        ],
    )
    updates_watch_globs: list[str] = []
    if isinstance(raw_watch_globs, list):
        for i, value in enumerate(raw_watch_globs):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"updates.watch_globs[{i}] must be a non-empty string")
                continue
            updates_watch_globs.append(value.strip())
    else:
        errors.append("updates.watch_globs must be a list of glob strings")

    # Ticket preferences (configurable bingo rules)
    preferences_raw = raw.get("preferences", {}) or {}
    try:
        preferences = TicketPreferences.from_dict(preferences_raw)
    except Exception as pref_exc:
        errors.append(f"preferences: invalid value — {pref_exc}")
        preferences = TicketPreferences()

    # Logging
    log_max_file_size_mb = safe_int(logging_cfg, "max_file_size_mb", 10, "logging.max_file_size_mb")
    log_backup_count = safe_int(logging_cfg, "backup_count", 3, "logging.backup_count")

    # Numeric ranges
    if browser_session_mode not in {"storage_state", "persistent_profile", "cdp_attach"}:
        errors.append("browser.session_mode must be one of: storage_state, persistent_profile, cdp_attach")
    if browser_session_mode == "storage_state" and not storage_state_path:
        errors.append("browser.storage_state_path is required when browser.session_mode is storage_state")
    if browser_session_mode == "persistent_profile" and not browser_user_data_dir:
        errors.append("browser.user_data_dir is required when browser.session_mode is persistent_profile")
    if browser_session_mode == "cdp_attach" and not browser_cdp_endpoint_url:
        errors.append("browser.cdp_endpoint_url is required when browser.session_mode is cdp_attach")
    if browser_session_mode == "cdp_attach" and browser_host_enabled:
        if not browser_host_chrome_executable_path:
            errors.append(
                "browser_host.chrome_executable_path is required when browser.session_mode is cdp_attach"
            )
        if not browser_host_user_data_dir:
            errors.append("browser_host.user_data_dir is required when browser.session_mode is cdp_attach")
        if browser_host_remote_debugging_port < 1:
            errors.append("browser_host.remote_debugging_port must be >= 1")
    if browser_cdp_connect_timeout_seconds < 1:
        errors.append("browser.cdp_connect_timeout_seconds must be >= 1")
    if browser_poll_min_seconds < 1:
        errors.append("browser.poll_min_seconds must be >= 1")
    if browser_poll_max_seconds < 1:
        errors.append("browser.poll_max_seconds must be >= 1")
    if browser_poll_min_seconds > browser_poll_max_seconds:
        errors.append("browser.poll_min_seconds must be <= browser.poll_max_seconds")
    if browser_poll_interval_seconds < 1:
        errors.append("browser.poll_interval_seconds must be >= 1")
    if browser_poll_jitter_seconds < 0:
        errors.append("browser.poll_jitter_seconds must be >= 0")
    if browser_poll_jitter_seconds > browser_poll_interval_seconds:
        errors.append("browser.poll_jitter_seconds must be <= browser.poll_interval_seconds")
    if browser_navigation_timeout_seconds < 1:
        errors.append("browser.navigation_timeout_seconds must be >= 1")
    if browser_challenge_threshold < 1:
        errors.append("browser.challenge_threshold must be >= 1")
    if browser_challenge_retry_seconds < 1:
        errors.append("browser.challenge_retry_seconds must be >= 1")
    if event_stagger_seconds < 0:
        errors.append("browser.event_stagger_seconds must be >= 0")
    if alerts_ticket_cooldown_seconds < 1:
        errors.append("alerts.ticket_cooldown_seconds must be >= 1")
    if alerts_operational_heartbeat_hours < 1:
        errors.append("alerts.operational_heartbeat_hours must be >= 1")
    if alerts_event_check_stale_seconds < 1:
        errors.append("alerts.event_check_stale_seconds must be >= 1")
    if alerts_operational_state_cooldown_seconds < 0:
        errors.append("alerts.operational_state_cooldown_seconds must be >= 0")
    if backoff_multiplier < 1:
        errors.append("polling.backoff_multiplier must be >= 1")
    if max_backoff_seconds < 1:
        errors.append("polling.max_backoff_seconds must be >= 1")
    if self_heal_browser_restart_threshold < 1:
        errors.append("self_heal.browser_restart_threshold must be >= 1")
    if self_heal_browser_restart_window_seconds < 1:
        errors.append("self_heal.browser_restart_window_seconds must be >= 1")
    if self_heal_process_restart_threshold < 1:
        errors.append("self_heal.process_restart_threshold must be >= 1")
    if self_heal_process_restart_window_seconds < 1:
        errors.append("self_heal.process_restart_window_seconds must be >= 1")
    if self_heal_error_alert_cooldown_seconds < 0:
        errors.append("self_heal.error_alert_cooldown_seconds must be >= 0")
    if auth_max_auto_login_attempts_per_hour < 1:
        errors.append("auth.max_auto_login_attempts_per_hour must be >= 1")
    if auth_auto_login_cooldown_seconds < 0:
        errors.append("auth.auto_login_cooldown_seconds must be >= 0")
    if auth_session_health_check_interval_seconds < 60:
        errors.append("auth.session_health_check_interval_seconds must be >= 60")
    if auth_auto_login_enabled:
        if not auth_keychain_service:
            errors.append("auth.keychain_service is required when auth.auto_login_enabled is true")
        if not auth_keychain_email_account:
            errors.append("auth.keychain_email_account is required when auth.auto_login_enabled is true")
        if not auth_keychain_password_account:
            errors.append("auth.keychain_password_account is required when auth.auto_login_enabled is true")
    if watchdog_interval_seconds < 10:
        errors.append("watchdog.interval_seconds must be >= 10")
    if watchdog_stale_after_seconds < 30:
        errors.append("watchdog.stale_after_seconds must be >= 30")
    if watchdog_max_fix_attempts_per_hour < 1:
        errors.append("watchdog.max_fix_attempts_per_hour must be >= 1")
    if updates_interval_seconds < 10:
        errors.append("updates.interval_seconds must be >= 10")
    if updates_stability_delay_seconds < 0:
        errors.append("updates.stability_delay_seconds must be >= 0")
    if not updates_watch_globs:
        errors.append("updates.watch_globs must include at least one glob")

    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    return MonitorConfig(
        discord_webhook_url=webhook_url,
        discord_username=str(discord.get("username", "Ticket Monitor")),
        discord_ping_user_id=str(discord.get("ping_user_id", "")).strip(),
        events=events,
        browser_storage_state_path=storage_state_path,
        browser_session_mode=browser_session_mode,
        browser_user_data_dir=browser_user_data_dir,
        browser_channel=browser_channel,
        browser_cdp_endpoint_url=browser_cdp_endpoint_url,
        browser_cdp_connect_timeout_seconds=browser_cdp_connect_timeout_seconds,
        browser_reuse_event_tabs=browser_reuse_event_tabs,
        browser_poll_min_seconds=browser_poll_min_seconds,
        browser_poll_max_seconds=browser_poll_max_seconds,
        browser_headless=browser_headless,
        browser_poll_interval_seconds=browser_poll_interval_seconds,
        browser_poll_jitter_seconds=browser_poll_jitter_seconds,
        browser_navigation_timeout_seconds=browser_navigation_timeout_seconds,
        browser_challenge_threshold=browser_challenge_threshold,
        browser_challenge_retry_seconds=browser_challenge_retry_seconds,
        event_stagger_seconds=event_stagger_seconds,
        browser_host_enabled=browser_host_enabled,
        browser_host_chrome_executable_path=browser_host_chrome_executable_path,
        browser_host_user_data_dir=browser_host_user_data_dir,
        browser_host_remote_debugging_port=browser_host_remote_debugging_port,
        alerts_ticket_cooldown_seconds=alerts_ticket_cooldown_seconds,
        alerts_operational_heartbeat_hours=alerts_operational_heartbeat_hours,
        alerts_event_check_stale_seconds=alerts_event_check_stale_seconds,
        alerts_operational_state_cooldown_seconds=alerts_operational_state_cooldown_seconds,
        backoff_multiplier=backoff_multiplier,
        max_backoff_seconds=max_backoff_seconds,
        self_heal_browser_restart_threshold=self_heal_browser_restart_threshold,
        self_heal_browser_restart_window_seconds=self_heal_browser_restart_window_seconds,
        self_heal_process_restart_threshold=self_heal_process_restart_threshold,
        self_heal_process_restart_window_seconds=self_heal_process_restart_window_seconds,
        self_heal_error_alert_cooldown_seconds=self_heal_error_alert_cooldown_seconds,
        auth_auto_login_enabled=auth_auto_login_enabled,
        auth_keychain_service=auth_keychain_service,
        auth_keychain_email_account=auth_keychain_email_account,
        auth_keychain_password_account=auth_keychain_password_account,
        auth_max_auto_login_attempts_per_hour=auth_max_auto_login_attempts_per_hour,
        auth_auto_login_cooldown_seconds=auth_auto_login_cooldown_seconds,
        auth_session_health_check_interval_seconds=auth_session_health_check_interval_seconds,
        auth_session_health_check_url=auth_session_health_check_url,
        watchdog_enabled=watchdog_enabled,
        watchdog_interval_seconds=watchdog_interval_seconds,
        watchdog_stale_after_seconds=watchdog_stale_after_seconds,
        watchdog_max_fix_attempts_per_hour=watchdog_max_fix_attempts_per_hour,
        updates_enabled=updates_enabled,
        updates_interval_seconds=updates_interval_seconds,
        updates_stability_delay_seconds=updates_stability_delay_seconds,
        updates_watch_globs=updates_watch_globs,
        preferences=preferences,
        timezone=timezone_str,
        log_level=str(logging_cfg.get("level", "INFO")).upper(),
        log_file=str(logging_cfg.get("file", "logs/monitor.log")),
        log_max_file_size_mb=log_max_file_size_mb,
        log_backup_count=log_backup_count,
    )
