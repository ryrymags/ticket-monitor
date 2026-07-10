"""Configuration loader and validator."""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml
from dateutil import tz

from .preferences import TicketPreferences

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Invalid or missing configuration.

    Raised instead of sys.exit so library callers (the GUI's bootstrap thread,
    tests) can handle it; CLI entry points catch it, print, and exit 1.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "Configuration errors:\n" + "\n".join(f"  - {e}" for e in self.errors)
        )


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

    # ntfy.sh push (optional second channel for friends)
    ntfy_enabled: bool
    ntfy_topics: list[str]
    ntfy_server: str
    ntfy_priority: str

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
    browser_headless: bool
    browser_navigation_timeout_seconds: int
    browser_challenge_threshold: int
    browser_challenge_retry_seconds: int
    # Challenge circuit-breaker: on a captcha/challenge (or honoring Retry-After), the
    # loop backs fully off for an exponentially-growing cooldown instead of hammering at
    # the capped cadence (which sustains the block). Resets on a clean check.
    browser_challenge_cooldown_base_seconds: int
    browser_challenge_cooldown_max_seconds: int
    browser_challenge_cooldown_escalate_after: int
    browser_challenge_cooldown_tiers_seconds: list[int]
    browser_challenge_cooldown_tier_every: int
    # Startup/recycle warmup: Ticketmaster blocks heavily right after launch. During this
    # window blind checks don't trip outage/degraded and the challenge cooldown stays at
    # its base, so the monitor can break in instead of flagging a false "blocked".
    browser_startup_grace_seconds: int
    browser_stealth_enabled: bool
    browser_locale: str
    browser_timezone_id: str
    browser_host_enabled: bool
    browser_host_chrome_executable_path: str
    browser_host_user_data_dir: str
    browser_host_remote_debugging_port: int

    # Alerts / detection
    alerts_ticket_cooldown_seconds: int
    alerts_operational_heartbeat_hours: int
    alerts_event_check_stale_seconds: int
    alerts_operational_state_cooldown_seconds: int
    # Non-BINGO ("not a match") availability alerts — global off-switch.
    alerts_non_bingo_enabled: bool
    # How long the monitor must stay degraded before pinging for manual action.
    alerts_manual_action_after_seconds: int
    # Whether routine operational/self-heal messages go to Discord (default: log only).
    alerts_operational_to_discord: bool

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
    auth_session_recheck_base_seconds: int
    auth_session_recheck_max_seconds: int
    auth_session_logout_confirmations_required: int

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
    bingo_configs: list[TicketPreferences]

    # General
    timezone: str
    log_level: str
    log_file: str
    log_max_file_size_mb: int
    log_backup_count: int

    # ntfy app deep link (optional; defaulted so existing constructions are unaffected).
    # Template that opens the native app — for Ticketmaster, an AppsFlyer OneLink.
    # Supports {url_encoded}, {url}, {event_id}. Empty = no "Open in App" button.
    ntfy_app_deep_link: str = ""
    browser_per_event_poll_min_seconds: int = 60
    browser_per_event_poll_max_seconds: int = 120
    browser_per_event_min_gap_between_checks_seconds: int = 60
    browser_per_event_max_gap_between_checks_seconds: int = 120
    browser_event_weights: dict[str, float] = field(default_factory=dict)
    browser_single_event_page: bool = True
    browser_event_dwell_min_seconds: int = 3
    browser_event_dwell_max_seconds: int = 8
    browser_homepage_warmup_interval_seconds: int = 1800
    # Variation probe (block-scope diagnosis on escalated pauses; src/variation_probe.py)
    browser_variation_probe_enabled: bool = True
    browser_variation_probe_min_interval_seconds: int = 1800
    browser_variation_probe_event_url: str = ""
    # Last-resort reboot tier (guardian): only for IP/device-scoped blocks that outlast
    # every lighter remedy, and hard rate-limited so it can never loop.
    watchdog_reboot_enabled: bool = False
    watchdog_reboot_after_impaired_seconds: int = 1800
    watchdog_reboot_min_system_uptime_seconds: int = 1800
    watchdog_reboot_min_spacing_seconds: int = 7200
    watchdog_reboot_max_per_day: int = 3
    # macOS launchd integration: wrap monitor with caffeinate during setup.
    macos_prevent_idle_sleep: bool = True


# No default weights: event priority bias comes only from browser.event_weights
# in config.yaml. Absent config means uniform weights for every event.
DEFAULT_EVENT_WEIGHTS: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Safe type conversion helpers — collect errors instead of crashing.
# ---------------------------------------------------------------------------


def safe_int(section: dict[str, Any], key: str, default: int, label: str, errors: list[str]) -> int:
    val = section.get(key, default)
    try:
        return int(val)
    except (ValueError, TypeError):
        errors.append(f"{label} must be an integer, got: {val!r}")
        return default


def safe_float(section: dict[str, Any], key: str, default: float, label: str, errors: list[str]) -> float:
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


def safe_int_list(
    section: dict[str, Any], key: str, default: list[int], label: str, errors: list[str]
) -> list[int]:
    val = section.get(key, default)
    if not isinstance(val, list):
        errors.append(f"{label} must be a list of integers")
        return list(default)
    values: list[int] = []
    for i, raw in enumerate(val):
        try:
            values.append(int(raw))
        except (ValueError, TypeError):
            errors.append(f"{label}[{i}] must be an integer, got: {raw!r}")
    return values


# ---------------------------------------------------------------------------
# Per-section loaders. Each takes the relevant raw section dict(s) plus the
# shared `errors` list and returns a dict of parsed values, keyed by the
# original local-variable name used in load_config (which, for most fields,
# is also the MonitorConfig field name).
# ---------------------------------------------------------------------------


def _warn_legacy_config_keys(raw: dict[str, Any]) -> None:
    """Backward compat: ignore legacy API keys if present in old config files."""
    if raw.get("ticketmaster", {}).get("api_key") or os.environ.get("TM_API_KEY"):
        logger.info("Ignoring legacy ticketmaster.api_key (Discovery API is no longer used)")
    if raw.get("optional", {}).get("discovery_telemetry_enabled") is not None:
        logger.info(
            "Ignoring legacy optional.discovery_telemetry_enabled (Discovery API is no longer used)"
        )


def _load_discord(discord: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or discord.get("webhook_url", "")).strip()
    if not webhook_url or webhook_url == "YOUR_WEBHOOK_URL_HERE":
        errors.append("discord.webhook_url is required — create one in Server Settings > Integrations > Webhooks")
    return {
        "webhook_url": webhook_url,
        "discord_username": str(discord.get("username", "Ticket Monitor")),
        "discord_ping_user_id": str(discord.get("ping_user_id", "")).strip(),
    }


def _load_ntfy(ntfy: dict[str, Any]) -> dict[str, Any]:
    # ntfy.sh — optional. Env NTFY_TOPIC (comma-separated) overrides config.
    # Accept either `topic:` (string) or `topics:` (list). Opt-in: never blocks
    # startup, so it is intentionally excluded from required-field validation.
    env_topics = os.environ.get("NTFY_TOPIC", "")
    if env_topics.strip():
        ntfy_topics = [t.strip() for t in env_topics.split(",") if t.strip()]
    else:
        raw_topics = ntfy.get("topics")
        if isinstance(raw_topics, list):
            ntfy_topics = [str(t).strip() for t in raw_topics if str(t).strip()]
        else:
            single = str(ntfy.get("topic", "")).strip()
            ntfy_topics = [single] if single else []
    ntfy_enabled = bool(ntfy.get("enabled", True)) and bool(ntfy_topics)
    ntfy_server = str(ntfy.get("server", "https://ntfy.sh")).strip() or "https://ntfy.sh"
    ntfy_priority = str(ntfy.get("priority", "high")).strip() or "high"
    ntfy_app_deep_link = str(ntfy.get("app_deep_link", "")).strip()
    return {
        "ntfy_enabled": ntfy_enabled,
        "ntfy_topics": ntfy_topics,
        "ntfy_server": ntfy_server,
        "ntfy_priority": ntfy_priority,
        "ntfy_app_deep_link": ntfy_app_deep_link,
    }


def _load_events(events_raw: list[Any], errors: list[str]) -> list[EventConfig]:
    if not events_raw:
        errors.append("events: at least one event must be configured")

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
    return events


def _validate_timezone(polling: dict[str, Any], errors: list[str]) -> str:
    timezone_str = str(polling.get("timezone", "US/Eastern"))
    if tz.gettz(timezone_str) is None:
        errors.append(f"polling.timezone is invalid: {timezone_str!r}")
    return timezone_str


def _load_browser(browser: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    storage_state_path = str(browser.get("storage_state_path", "secrets/tm_storage_state.json")).strip()
    browser_session_mode = str(browser.get("session_mode", "storage_state")).strip().lower()
    browser_user_data_dir = str(browser.get("user_data_dir", "secrets/tm_profile")).strip()
    browser_channel = str(browser.get("channel", "")).strip()
    browser_cdp_endpoint_url = str(browser.get("cdp_endpoint_url", "http://127.0.0.1:9222")).strip()
    browser_cdp_connect_timeout_seconds = safe_int(
        browser,
        "cdp_connect_timeout_seconds",
        10,
        "browser.cdp_connect_timeout_seconds",
        errors,
    )
    browser_reuse_event_tabs = safe_bool(browser, "reuse_event_tabs", True)
    browser_per_event_poll_min_seconds = safe_int(
        browser,
        "per_event_poll_min_seconds",
        60,
        "browser.per_event_poll_min_seconds",
        errors,
    )
    browser_per_event_poll_max_seconds = safe_int(
        browser,
        "per_event_poll_max_seconds",
        120,
        "browser.per_event_poll_max_seconds",
        errors,
    )
    browser_per_event_min_gap_between_checks_seconds = safe_int(
        browser,
        "per_event_min_gap_between_checks_seconds",
        60,
        "browser.per_event_min_gap_between_checks_seconds",
        errors,
    )
    browser_per_event_max_gap_between_checks_seconds = safe_int(
        browser,
        "per_event_max_gap_between_checks_seconds",
        120,
        "browser.per_event_max_gap_between_checks_seconds",
        errors,
    )
    browser_single_event_page = safe_bool(browser, "single_event_page", True)
    browser_event_dwell_min_seconds = safe_int(
        browser,
        "event_dwell_min_seconds",
        3,
        "browser.event_dwell_min_seconds",
        errors,
    )
    browser_event_dwell_max_seconds = safe_int(
        browser,
        "event_dwell_max_seconds",
        8,
        "browser.event_dwell_max_seconds",
        errors,
    )
    browser_homepage_warmup_interval_seconds = safe_int(
        browser,
        "homepage_warmup_interval_seconds",
        1800,
        "browser.homepage_warmup_interval_seconds",
        errors,
    )
    variation_probe = browser.get("variation_probe", {}) or {}
    browser_variation_probe_enabled = safe_bool(variation_probe, "enabled", True)
    browser_variation_probe_min_interval_seconds = safe_int(
        variation_probe,
        "min_interval_seconds",
        1800,
        "browser.variation_probe.min_interval_seconds",
        errors,
    )
    browser_variation_probe_event_url = str(variation_probe.get("event_url_override", "")).strip()
    browser_event_weights = dict(DEFAULT_EVENT_WEIGHTS)
    raw_event_weights = browser.get("event_weights")
    if raw_event_weights is not None:
        if isinstance(raw_event_weights, dict):
            browser_event_weights = {}
            for event_id, raw_weight in raw_event_weights.items():
                event_id_str = str(event_id).strip()
                if not event_id_str:
                    errors.append("browser.event_weights keys must be non-empty event IDs")
                    continue
                try:
                    weight = float(raw_weight)
                except (ValueError, TypeError):
                    errors.append(
                        f"browser.event_weights[{event_id_str!r}] must be a number, got: {raw_weight!r}"
                    )
                    continue
                if not math.isfinite(weight) or weight <= 0:
                    errors.append(f"browser.event_weights[{event_id_str!r}] must be > 0")
                    continue
                browser_event_weights[event_id_str] = weight
        else:
            errors.append("browser.event_weights must be a mapping of event_id to positive weight")

    browser_headless = safe_bool(browser, "headless", True)
    browser_navigation_timeout_seconds = safe_int(
        browser, "navigation_timeout_seconds", 20, "browser.navigation_timeout_seconds", errors
    )
    browser_challenge_threshold = safe_int(
        browser, "challenge_threshold", 5, "browser.challenge_threshold", errors
    )
    browser_challenge_retry_seconds = safe_int(
        browser, "challenge_retry_seconds", 60, "browser.challenge_retry_seconds", errors
    )
    browser_challenge_cooldown_base_seconds = safe_int(
        browser, "challenge_cooldown_base_seconds", 60, "browser.challenge_cooldown_base_seconds", errors
    )
    browser_challenge_cooldown_max_seconds = safe_int(
        browser, "challenge_cooldown_max_seconds", 300, "browser.challenge_cooldown_max_seconds", errors
    )
    browser_challenge_cooldown_escalate_after = safe_int(
        browser,
        "challenge_cooldown_escalate_after",
        6,
        "browser.challenge_cooldown_escalate_after",
        errors,
    )
    browser_challenge_cooldown_tiers_seconds = safe_int_list(
        browser,
        "challenge_cooldown_tiers_seconds",
        [300, 900, 1800],
        "browser.challenge_cooldown_tiers_seconds",
        errors,
    )
    browser_challenge_cooldown_tier_every = safe_int(
        browser,
        "challenge_cooldown_tier_every",
        3,
        "browser.challenge_cooldown_tier_every",
        errors,
    )
    browser_startup_grace_seconds = safe_int(
        browser, "startup_grace_seconds", 180, "browser.startup_grace_seconds", errors
    )
    browser_stealth_enabled = safe_bool(browser, "stealth_enabled", True)
    browser_locale = str(browser.get("locale", "en-US")).strip() or "en-US"
    browser_timezone_id = (
        str(browser.get("timezone_id", "America/New_York")).strip() or "America/New_York"
    )

    return {
        "storage_state_path": storage_state_path,
        "browser_session_mode": browser_session_mode,
        "browser_user_data_dir": browser_user_data_dir,
        "browser_channel": browser_channel,
        "browser_cdp_endpoint_url": browser_cdp_endpoint_url,
        "browser_cdp_connect_timeout_seconds": browser_cdp_connect_timeout_seconds,
        "browser_reuse_event_tabs": browser_reuse_event_tabs,
        "browser_per_event_poll_min_seconds": browser_per_event_poll_min_seconds,
        "browser_per_event_poll_max_seconds": browser_per_event_poll_max_seconds,
        "browser_per_event_min_gap_between_checks_seconds": browser_per_event_min_gap_between_checks_seconds,
        "browser_per_event_max_gap_between_checks_seconds": browser_per_event_max_gap_between_checks_seconds,
        "browser_single_event_page": browser_single_event_page,
        "browser_event_dwell_min_seconds": browser_event_dwell_min_seconds,
        "browser_event_dwell_max_seconds": browser_event_dwell_max_seconds,
        "browser_homepage_warmup_interval_seconds": browser_homepage_warmup_interval_seconds,
        "browser_variation_probe_enabled": browser_variation_probe_enabled,
        "browser_variation_probe_min_interval_seconds": browser_variation_probe_min_interval_seconds,
        "browser_variation_probe_event_url": browser_variation_probe_event_url,
        "browser_event_weights": browser_event_weights,
        "browser_headless": browser_headless,
        "browser_navigation_timeout_seconds": browser_navigation_timeout_seconds,
        "browser_challenge_threshold": browser_challenge_threshold,
        "browser_challenge_retry_seconds": browser_challenge_retry_seconds,
        "browser_challenge_cooldown_base_seconds": browser_challenge_cooldown_base_seconds,
        "browser_challenge_cooldown_max_seconds": browser_challenge_cooldown_max_seconds,
        "browser_challenge_cooldown_escalate_after": browser_challenge_cooldown_escalate_after,
        "browser_challenge_cooldown_tiers_seconds": browser_challenge_cooldown_tiers_seconds,
        "browser_challenge_cooldown_tier_every": browser_challenge_cooldown_tier_every,
        "browser_startup_grace_seconds": browser_startup_grace_seconds,
        "browser_stealth_enabled": browser_stealth_enabled,
        "browser_locale": browser_locale,
        "browser_timezone_id": browser_timezone_id,
    }


def _load_browser_host(
    browser_host: dict[str, Any], browser_session_mode: str, errors: list[str]
) -> dict[str, Any]:
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
        errors,
    )
    return {
        "browser_host_enabled": browser_host_enabled,
        "browser_host_chrome_executable_path": browser_host_chrome_executable_path,
        "browser_host_user_data_dir": browser_host_user_data_dir,
        "browser_host_remote_debugging_port": browser_host_remote_debugging_port,
    }


def _load_alerts(alerts: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    alerts_ticket_cooldown_seconds = safe_int(
        alerts, "ticket_cooldown_seconds", 180, "alerts.ticket_cooldown_seconds", errors
    )
    alerts_operational_heartbeat_hours = safe_int(
        alerts, "operational_heartbeat_hours", 6, "alerts.operational_heartbeat_hours", errors
    )
    alerts_event_check_stale_seconds = safe_int(
        alerts, "event_check_stale_seconds", 360, "alerts.event_check_stale_seconds", errors
    )
    alerts_operational_state_cooldown_seconds = safe_int(
        alerts,
        "operational_state_cooldown_seconds",
        1800,
        "alerts.operational_state_cooldown_seconds",
        errors,
    )
    alerts_non_bingo_enabled = safe_bool(alerts, "non_bingo_enabled", False)
    alerts_manual_action_after_seconds = safe_int(
        alerts,
        "manual_action_after_seconds",
        900,
        "alerts.manual_action_after_seconds",
        errors,
    )
    alerts_operational_to_discord = safe_bool(alerts, "operational_to_discord", False)
    return {
        "alerts_ticket_cooldown_seconds": alerts_ticket_cooldown_seconds,
        "alerts_operational_heartbeat_hours": alerts_operational_heartbeat_hours,
        "alerts_event_check_stale_seconds": alerts_event_check_stale_seconds,
        "alerts_operational_state_cooldown_seconds": alerts_operational_state_cooldown_seconds,
        "alerts_non_bingo_enabled": alerts_non_bingo_enabled,
        "alerts_manual_action_after_seconds": alerts_manual_action_after_seconds,
        "alerts_operational_to_discord": alerts_operational_to_discord,
    }


def _load_retry(polling: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    backoff_multiplier = safe_float(polling, "backoff_multiplier", 2.0, "polling.backoff_multiplier", errors)
    max_backoff_seconds = safe_int(polling, "max_backoff_seconds", 120, "polling.max_backoff_seconds", errors)
    return {
        "backoff_multiplier": backoff_multiplier,
        "max_backoff_seconds": max_backoff_seconds,
    }


def _load_self_heal(self_heal: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    self_heal_browser_restart_threshold = safe_int(
        self_heal, "browser_restart_threshold", 3, "self_heal.browser_restart_threshold", errors
    )
    self_heal_browser_restart_window_seconds = safe_int(
        self_heal,
        "browser_restart_window_seconds",
        600,
        "self_heal.browser_restart_window_seconds",
        errors,
    )
    self_heal_process_restart_threshold = safe_int(
        self_heal, "process_restart_threshold", 6, "self_heal.process_restart_threshold", errors
    )
    self_heal_process_restart_window_seconds = safe_int(
        self_heal,
        "process_restart_window_seconds",
        1800,
        "self_heal.process_restart_window_seconds",
        errors,
    )
    self_heal_error_alert_cooldown_seconds = safe_int(
        self_heal,
        "error_alert_cooldown_seconds",
        1800,
        "self_heal.error_alert_cooldown_seconds",
        errors,
    )
    return {
        "self_heal_browser_restart_threshold": self_heal_browser_restart_threshold,
        "self_heal_browser_restart_window_seconds": self_heal_browser_restart_window_seconds,
        "self_heal_process_restart_threshold": self_heal_process_restart_threshold,
        "self_heal_process_restart_window_seconds": self_heal_process_restart_window_seconds,
        "self_heal_error_alert_cooldown_seconds": self_heal_error_alert_cooldown_seconds,
    }


def _load_auth(auth: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    auth_auto_login_enabled = safe_bool(auth, "auto_login_enabled", False)
    auth_keychain_service = str(auth.get("keychain_service", "ticket-monitor")).strip()
    auth_keychain_email_account = str(auth.get("keychain_email_account", "ticketmaster-email")).strip()
    auth_keychain_password_account = str(auth.get("keychain_password_account", "ticketmaster-password")).strip()
    auth_max_auto_login_attempts_per_hour = safe_int(
        auth,
        "max_auto_login_attempts_per_hour",
        3,
        "auth.max_auto_login_attempts_per_hour",
        errors,
    )
    auth_auto_login_cooldown_seconds = safe_int(
        auth,
        "auto_login_cooldown_seconds",
        1800,
        "auth.auto_login_cooldown_seconds",
        errors,
    )
    auth_session_health_check_interval_seconds = safe_int(
        auth,
        "session_health_check_interval_seconds",
        3600,
        "auth.session_health_check_interval_seconds",
        errors,
    )
    auth_session_health_check_url = str(
        auth.get("session_health_check_url", "https://www.ticketmaster.com/my-account")
    ).strip()
    auth_session_recheck_base_seconds = safe_int(
        auth,
        "session_recheck_base_seconds",
        120,
        "auth.session_recheck_base_seconds",
        errors,
    )
    auth_session_recheck_max_seconds = safe_int(
        auth,
        "session_recheck_max_seconds",
        900,
        "auth.session_recheck_max_seconds",
        errors,
    )
    auth_session_logout_confirmations_required = safe_int(
        auth,
        "session_logout_confirmations_required",
        2,
        "auth.session_logout_confirmations_required",
        errors,
    )
    return {
        "auth_auto_login_enabled": auth_auto_login_enabled,
        "auth_keychain_service": auth_keychain_service,
        "auth_keychain_email_account": auth_keychain_email_account,
        "auth_keychain_password_account": auth_keychain_password_account,
        "auth_max_auto_login_attempts_per_hour": auth_max_auto_login_attempts_per_hour,
        "auth_auto_login_cooldown_seconds": auth_auto_login_cooldown_seconds,
        "auth_session_health_check_interval_seconds": auth_session_health_check_interval_seconds,
        "auth_session_health_check_url": auth_session_health_check_url,
        "auth_session_recheck_base_seconds": auth_session_recheck_base_seconds,
        "auth_session_recheck_max_seconds": auth_session_recheck_max_seconds,
        "auth_session_logout_confirmations_required": auth_session_logout_confirmations_required,
    }


def _load_watchdog(watchdog: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    watchdog_enabled = safe_bool(watchdog, "enabled", True)
    watchdog_interval_seconds = safe_int(watchdog, "interval_seconds", 120, "watchdog.interval_seconds", errors)
    watchdog_stale_after_seconds = safe_int(
        watchdog, "stale_after_seconds", 600, "watchdog.stale_after_seconds", errors
    )
    watchdog_max_fix_attempts_per_hour = safe_int(
        watchdog,
        "max_fix_attempts_per_hour",
        6,
        "watchdog.max_fix_attempts_per_hour",
        errors,
    )
    watchdog_reboot = watchdog.get("reboot", {}) or {}
    watchdog_reboot_enabled = safe_bool(watchdog_reboot, "enabled", False)
    watchdog_reboot_after_impaired_seconds = safe_int(
        watchdog_reboot, "after_impaired_seconds", 1800, "watchdog.reboot.after_impaired_seconds", errors
    )
    watchdog_reboot_min_system_uptime_seconds = safe_int(
        watchdog_reboot, "min_system_uptime_seconds", 1800, "watchdog.reboot.min_system_uptime_seconds", errors
    )
    watchdog_reboot_min_spacing_seconds = safe_int(
        watchdog_reboot, "min_spacing_seconds", 7200, "watchdog.reboot.min_spacing_seconds", errors
    )
    watchdog_reboot_max_per_day = safe_int(
        watchdog_reboot, "max_per_day", 3, "watchdog.reboot.max_per_day", errors
    )
    return {
        "watchdog_enabled": watchdog_enabled,
        "watchdog_interval_seconds": watchdog_interval_seconds,
        "watchdog_stale_after_seconds": watchdog_stale_after_seconds,
        "watchdog_max_fix_attempts_per_hour": watchdog_max_fix_attempts_per_hour,
        "watchdog_reboot_enabled": watchdog_reboot_enabled,
        "watchdog_reboot_after_impaired_seconds": watchdog_reboot_after_impaired_seconds,
        "watchdog_reboot_min_system_uptime_seconds": watchdog_reboot_min_system_uptime_seconds,
        "watchdog_reboot_min_spacing_seconds": watchdog_reboot_min_spacing_seconds,
        "watchdog_reboot_max_per_day": watchdog_reboot_max_per_day,
    }


def _load_updates(updates: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    updates_enabled = safe_bool(updates, "enabled", True)
    updates_interval_seconds = safe_int(updates, "interval_seconds", 60, "updates.interval_seconds", errors)
    updates_stability_delay_seconds = safe_int(
        updates, "stability_delay_seconds", 20, "updates.stability_delay_seconds", errors
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
    return {
        "updates_enabled": updates_enabled,
        "updates_interval_seconds": updates_interval_seconds,
        "updates_stability_delay_seconds": updates_stability_delay_seconds,
        "updates_watch_globs": updates_watch_globs,
    }


def _load_bingo_configs(raw: dict[str, Any], errors: list[str]) -> tuple[TicketPreferences, list[TicketPreferences]]:
    """Ticket preferences (configurable BINGO rules)."""
    preferences_raw = raw.get("preferences", {}) or {}
    bingo_configs_raw = raw.get("bingo_configs")
    bingo_configs: list[TicketPreferences] = []

    if bingo_configs_raw is None:
        try:
            preferences = TicketPreferences.from_dict(preferences_raw)
            bingo_configs = [preferences]
        except Exception as pref_exc:
            errors.append(f"preferences: invalid value — {pref_exc}")
            preferences = TicketPreferences()
            bingo_configs = [preferences]
    elif isinstance(bingo_configs_raw, list):
        for i, pref_raw in enumerate(bingo_configs_raw):
            if not isinstance(pref_raw, dict):
                errors.append(f"bingo_configs[{i}] must be a mapping")
                continue
            try:
                pref = TicketPreferences.from_dict(pref_raw)
                if not pref.name or pref.name == "BINGO":
                    pref.name = f"BINGO {i + 1}"
                bingo_configs.append(pref)
            except Exception as pref_exc:
                errors.append(f"bingo_configs[{i}]: invalid value — {pref_exc}")
        if not bingo_configs:
            errors.append("bingo_configs must include at least one valid BINGO config")
            preferences = TicketPreferences()
            bingo_configs = [preferences]
        else:
            preferences = bingo_configs[0]
    else:
        errors.append("bingo_configs must be a list of BINGO config mappings")
        preferences = TicketPreferences()
        bingo_configs = [preferences]

    return preferences, bingo_configs


def _load_logging(logging_cfg: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    log_max_file_size_mb = safe_int(logging_cfg, "max_file_size_mb", 10, "logging.max_file_size_mb", errors)
    log_backup_count = safe_int(logging_cfg, "backup_count", 3, "logging.backup_count", errors)
    return {
        "log_level": str(logging_cfg.get("level", "INFO")).upper(),
        "log_file": str(logging_cfg.get("file", "logs/monitor.log")),
        "log_max_file_size_mb": log_max_file_size_mb,
        "log_backup_count": log_backup_count,
    }


def _load_macos(macos: dict[str, Any]) -> dict[str, Any]:
    return {"macos_prevent_idle_sleep": safe_bool(macos, "prevent_idle_sleep", True)}


def load_config(path: str = "config.yaml") -> MonitorConfig:
    """Load and validate configuration from a YAML file."""
    if not os.path.exists(path):
        raise ConfigError([f"Config file not found: {path}"])

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    errors: list[str] = []

    # Required top-level keys
    discord = raw.get("discord", {})
    ntfy = raw.get("ntfy", {})
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
    macos = raw.get("macos", {})

    _warn_legacy_config_keys(raw)

    discord_values = _load_discord(discord, errors)
    webhook_url = discord_values["webhook_url"]

    ntfy_values = _load_ntfy(ntfy)

    events = _load_events(events_raw, errors)

    # Validate timezone
    timezone_str = _validate_timezone(polling, errors)

    # Browser
    browser_values = _load_browser(browser, errors)
    storage_state_path = browser_values["storage_state_path"]
    browser_session_mode = browser_values["browser_session_mode"]
    browser_user_data_dir = browser_values["browser_user_data_dir"]
    browser_cdp_endpoint_url = browser_values["browser_cdp_endpoint_url"]
    browser_cdp_connect_timeout_seconds = browser_values["browser_cdp_connect_timeout_seconds"]
    browser_per_event_poll_min_seconds = browser_values["browser_per_event_poll_min_seconds"]
    browser_per_event_poll_max_seconds = browser_values["browser_per_event_poll_max_seconds"]
    browser_per_event_min_gap_between_checks_seconds = browser_values[
        "browser_per_event_min_gap_between_checks_seconds"
    ]
    browser_per_event_max_gap_between_checks_seconds = browser_values[
        "browser_per_event_max_gap_between_checks_seconds"
    ]
    browser_event_dwell_min_seconds = browser_values["browser_event_dwell_min_seconds"]
    browser_event_dwell_max_seconds = browser_values["browser_event_dwell_max_seconds"]
    browser_homepage_warmup_interval_seconds = browser_values["browser_homepage_warmup_interval_seconds"]
    browser_navigation_timeout_seconds = browser_values["browser_navigation_timeout_seconds"]
    browser_challenge_threshold = browser_values["browser_challenge_threshold"]
    browser_challenge_retry_seconds = browser_values["browser_challenge_retry_seconds"]
    browser_challenge_cooldown_escalate_after = browser_values["browser_challenge_cooldown_escalate_after"]
    browser_challenge_cooldown_tier_every = browser_values["browser_challenge_cooldown_tier_every"]
    browser_challenge_cooldown_tiers_seconds = browser_values["browser_challenge_cooldown_tiers_seconds"]

    # Browser host (CDP attach)
    browser_host_values = _load_browser_host(browser_host, browser_session_mode, errors)
    browser_host_enabled = browser_host_values["browser_host_enabled"]
    browser_host_chrome_executable_path = browser_host_values["browser_host_chrome_executable_path"]
    browser_host_user_data_dir = browser_host_values["browser_host_user_data_dir"]
    browser_host_remote_debugging_port = browser_host_values["browser_host_remote_debugging_port"]

    # Alerts
    alerts_values = _load_alerts(alerts, errors)
    alerts_ticket_cooldown_seconds = alerts_values["alerts_ticket_cooldown_seconds"]
    alerts_operational_heartbeat_hours = alerts_values["alerts_operational_heartbeat_hours"]
    alerts_event_check_stale_seconds = alerts_values["alerts_event_check_stale_seconds"]
    alerts_operational_state_cooldown_seconds = alerts_values["alerts_operational_state_cooldown_seconds"]
    alerts_manual_action_after_seconds = alerts_values["alerts_manual_action_after_seconds"]

    # Retry
    retry_values = _load_retry(polling, errors)
    backoff_multiplier = retry_values["backoff_multiplier"]
    max_backoff_seconds = retry_values["max_backoff_seconds"]

    # Self healing
    self_heal_values = _load_self_heal(self_heal, errors)
    self_heal_browser_restart_threshold = self_heal_values["self_heal_browser_restart_threshold"]
    self_heal_browser_restart_window_seconds = self_heal_values["self_heal_browser_restart_window_seconds"]
    self_heal_process_restart_threshold = self_heal_values["self_heal_process_restart_threshold"]
    self_heal_process_restart_window_seconds = self_heal_values["self_heal_process_restart_window_seconds"]
    self_heal_error_alert_cooldown_seconds = self_heal_values["self_heal_error_alert_cooldown_seconds"]

    # Auth session auto-fix
    auth_values = _load_auth(auth, errors)
    auth_auto_login_enabled = auth_values["auth_auto_login_enabled"]
    auth_keychain_service = auth_values["auth_keychain_service"]
    auth_keychain_email_account = auth_values["auth_keychain_email_account"]
    auth_keychain_password_account = auth_values["auth_keychain_password_account"]
    auth_max_auto_login_attempts_per_hour = auth_values["auth_max_auto_login_attempts_per_hour"]
    auth_auto_login_cooldown_seconds = auth_values["auth_auto_login_cooldown_seconds"]
    auth_session_health_check_interval_seconds = auth_values["auth_session_health_check_interval_seconds"]
    auth_session_recheck_base_seconds = auth_values["auth_session_recheck_base_seconds"]
    auth_session_recheck_max_seconds = auth_values["auth_session_recheck_max_seconds"]
    auth_session_logout_confirmations_required = auth_values["auth_session_logout_confirmations_required"]

    # Watchdog
    watchdog_values = _load_watchdog(watchdog, errors)
    watchdog_interval_seconds = watchdog_values["watchdog_interval_seconds"]
    watchdog_stale_after_seconds = watchdog_values["watchdog_stale_after_seconds"]
    watchdog_max_fix_attempts_per_hour = watchdog_values["watchdog_max_fix_attempts_per_hour"]

    # Updates
    updates_values = _load_updates(updates, errors)
    updates_interval_seconds = updates_values["updates_interval_seconds"]
    updates_stability_delay_seconds = updates_values["updates_stability_delay_seconds"]
    updates_watch_globs = updates_values["updates_watch_globs"]

    # Ticket preferences (configurable BINGO rules)
    preferences, bingo_configs = _load_bingo_configs(raw, errors)

    # Logging
    logging_values = _load_logging(logging_cfg, errors)
    log_max_file_size_mb = logging_values["log_max_file_size_mb"]
    log_backup_count = logging_values["log_backup_count"]

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
    if browser_per_event_poll_min_seconds < 1:
        errors.append("browser.per_event_poll_min_seconds must be >= 1")
    if browser_per_event_poll_max_seconds < 1:
        errors.append("browser.per_event_poll_max_seconds must be >= 1")
    if browser_per_event_poll_min_seconds > browser_per_event_poll_max_seconds:
        errors.append("browser.per_event_poll_min_seconds must be <= browser.per_event_poll_max_seconds")
    if browser_per_event_min_gap_between_checks_seconds < 0:
        errors.append("browser.per_event_min_gap_between_checks_seconds must be >= 0")
    # Anti-block guard: floors under 45s burned DataDome/IP reputation once already
    # (June 2026 pause spike). Loud warning so an aggressive cadence can't sneak back.
    for floor_name, floor_value in (
        ("browser.per_event_poll_min_seconds", browser_per_event_poll_min_seconds),
        ("browser.per_event_min_gap_between_checks_seconds", browser_per_event_min_gap_between_checks_seconds),
    ):
        if 0 < floor_value < 45:
            logger.warning(
                "%s=%d is aggressive (< 45s) — cadences this fast have previously "
                "triggered Ticketmaster bot-detection pauses; 60-120s is the safe range",
                floor_name,
                floor_value,
            )
    if browser_per_event_max_gap_between_checks_seconds < browser_per_event_min_gap_between_checks_seconds:
        errors.append(
            "browser.per_event_max_gap_between_checks_seconds must be >= "
            "browser.per_event_min_gap_between_checks_seconds"
        )
    if browser_event_dwell_min_seconds < 0:
        errors.append("browser.event_dwell_min_seconds must be >= 0")
    if browser_event_dwell_max_seconds < 0:
        errors.append("browser.event_dwell_max_seconds must be >= 0")
    if browser_event_dwell_min_seconds > browser_event_dwell_max_seconds:
        errors.append("browser.event_dwell_min_seconds must be <= browser.event_dwell_max_seconds")
    if browser_homepage_warmup_interval_seconds < 0:
        errors.append("browser.homepage_warmup_interval_seconds must be >= 0")
    if browser_navigation_timeout_seconds < 1:
        errors.append("browser.navigation_timeout_seconds must be >= 1")
    if browser_challenge_threshold < 1:
        errors.append("browser.challenge_threshold must be >= 1")
    if browser_challenge_retry_seconds < 1:
        errors.append("browser.challenge_retry_seconds must be >= 1")
    if browser_challenge_cooldown_escalate_after < 1:
        errors.append("browser.challenge_cooldown_escalate_after must be >= 1")
    if browser_challenge_cooldown_tier_every < 1:
        errors.append("browser.challenge_cooldown_tier_every must be >= 1")
    if not browser_challenge_cooldown_tiers_seconds:
        errors.append("browser.challenge_cooldown_tiers_seconds must be non-empty")
    prev_tier = 0
    for i, value in enumerate(browser_challenge_cooldown_tiers_seconds):
        if value < 1:
            errors.append(f"browser.challenge_cooldown_tiers_seconds[{i}] must be >= 1")
        if prev_tier and value <= prev_tier:
            errors.append("browser.challenge_cooldown_tiers_seconds must be ascending")
        prev_tier = value
    if alerts_ticket_cooldown_seconds < 1:
        errors.append("alerts.ticket_cooldown_seconds must be >= 1")
    if alerts_operational_heartbeat_hours < 1:
        errors.append("alerts.operational_heartbeat_hours must be >= 1")
    if alerts_event_check_stale_seconds < 1:
        errors.append("alerts.event_check_stale_seconds must be >= 1")
    if alerts_operational_state_cooldown_seconds < 0:
        errors.append("alerts.operational_state_cooldown_seconds must be >= 0")
    if alerts_manual_action_after_seconds < 0:
        errors.append("alerts.manual_action_after_seconds must be >= 0")
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
    if auth_session_recheck_base_seconds < 30:
        errors.append("auth.session_recheck_base_seconds must be >= 30")
    if auth_session_recheck_max_seconds < auth_session_recheck_base_seconds:
        errors.append("auth.session_recheck_max_seconds must be >= auth.session_recheck_base_seconds")
    if auth_session_logout_confirmations_required < 1:
        errors.append("auth.session_logout_confirmations_required must be >= 1")
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

    # macOS launchd integration
    macos_values = _load_macos(macos)

    if errors:
        raise ConfigError(errors)

    return MonitorConfig(
        discord_webhook_url=webhook_url,
        discord_username=discord_values["discord_username"],
        discord_ping_user_id=discord_values["discord_ping_user_id"],
        ntfy_enabled=ntfy_values["ntfy_enabled"],
        ntfy_topics=ntfy_values["ntfy_topics"],
        ntfy_server=ntfy_values["ntfy_server"],
        ntfy_priority=ntfy_values["ntfy_priority"],
        ntfy_app_deep_link=ntfy_values["ntfy_app_deep_link"],
        events=events,
        browser_storage_state_path=storage_state_path,
        browser_session_mode=browser_session_mode,
        browser_user_data_dir=browser_user_data_dir,
        browser_channel=browser_values["browser_channel"],
        browser_cdp_endpoint_url=browser_cdp_endpoint_url,
        browser_cdp_connect_timeout_seconds=browser_cdp_connect_timeout_seconds,
        browser_reuse_event_tabs=browser_values["browser_reuse_event_tabs"],
        browser_headless=browser_values["browser_headless"],
        browser_navigation_timeout_seconds=browser_navigation_timeout_seconds,
        browser_challenge_threshold=browser_challenge_threshold,
        browser_challenge_retry_seconds=browser_challenge_retry_seconds,
        browser_challenge_cooldown_base_seconds=browser_values["browser_challenge_cooldown_base_seconds"],
        browser_challenge_cooldown_max_seconds=browser_values["browser_challenge_cooldown_max_seconds"],
        browser_challenge_cooldown_escalate_after=browser_challenge_cooldown_escalate_after,
        browser_challenge_cooldown_tiers_seconds=browser_challenge_cooldown_tiers_seconds,
        browser_challenge_cooldown_tier_every=browser_challenge_cooldown_tier_every,
        browser_startup_grace_seconds=browser_values["browser_startup_grace_seconds"],
        browser_stealth_enabled=browser_values["browser_stealth_enabled"],
        browser_locale=browser_values["browser_locale"],
        browser_timezone_id=browser_values["browser_timezone_id"],
        browser_host_enabled=browser_host_enabled,
        browser_host_chrome_executable_path=browser_host_chrome_executable_path,
        browser_host_user_data_dir=browser_host_user_data_dir,
        browser_host_remote_debugging_port=browser_host_remote_debugging_port,
        alerts_ticket_cooldown_seconds=alerts_ticket_cooldown_seconds,
        alerts_operational_heartbeat_hours=alerts_operational_heartbeat_hours,
        alerts_event_check_stale_seconds=alerts_event_check_stale_seconds,
        alerts_operational_state_cooldown_seconds=alerts_operational_state_cooldown_seconds,
        alerts_non_bingo_enabled=alerts_values["alerts_non_bingo_enabled"],
        alerts_manual_action_after_seconds=alerts_manual_action_after_seconds,
        alerts_operational_to_discord=alerts_values["alerts_operational_to_discord"],
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
        auth_session_health_check_url=auth_values["auth_session_health_check_url"],
        auth_session_recheck_base_seconds=auth_session_recheck_base_seconds,
        auth_session_recheck_max_seconds=auth_session_recheck_max_seconds,
        auth_session_logout_confirmations_required=auth_session_logout_confirmations_required,
        watchdog_enabled=watchdog_values["watchdog_enabled"],
        watchdog_interval_seconds=watchdog_interval_seconds,
        watchdog_stale_after_seconds=watchdog_stale_after_seconds,
        watchdog_max_fix_attempts_per_hour=watchdog_max_fix_attempts_per_hour,
        updates_enabled=updates_values["updates_enabled"],
        updates_interval_seconds=updates_interval_seconds,
        updates_stability_delay_seconds=updates_stability_delay_seconds,
        updates_watch_globs=updates_watch_globs,
        macos_prevent_idle_sleep=macos_values["macos_prevent_idle_sleep"],
        preferences=preferences,
        bingo_configs=bingo_configs,
        timezone=timezone_str,
        log_level=logging_values["log_level"],
        log_file=logging_values["log_file"],
        log_max_file_size_mb=log_max_file_size_mb,
        log_backup_count=log_backup_count,
        browser_per_event_poll_min_seconds=browser_per_event_poll_min_seconds,
        browser_per_event_poll_max_seconds=browser_per_event_poll_max_seconds,
        browser_per_event_min_gap_between_checks_seconds=browser_per_event_min_gap_between_checks_seconds,
        browser_per_event_max_gap_between_checks_seconds=browser_per_event_max_gap_between_checks_seconds,
        browser_event_weights=browser_values["browser_event_weights"],
        browser_single_event_page=browser_values["browser_single_event_page"],
        browser_event_dwell_min_seconds=browser_event_dwell_min_seconds,
        browser_event_dwell_max_seconds=browser_event_dwell_max_seconds,
        browser_homepage_warmup_interval_seconds=browser_homepage_warmup_interval_seconds,
        browser_variation_probe_enabled=browser_values["browser_variation_probe_enabled"],
        browser_variation_probe_min_interval_seconds=browser_values["browser_variation_probe_min_interval_seconds"],
        browser_variation_probe_event_url=browser_values["browser_variation_probe_event_url"],
        watchdog_reboot_enabled=watchdog_values["watchdog_reboot_enabled"],
        watchdog_reboot_after_impaired_seconds=watchdog_values["watchdog_reboot_after_impaired_seconds"],
        watchdog_reboot_min_system_uptime_seconds=watchdog_values["watchdog_reboot_min_system_uptime_seconds"],
        watchdog_reboot_min_spacing_seconds=watchdog_values["watchdog_reboot_min_spacing_seconds"],
        watchdog_reboot_max_per_day=watchdog_values["watchdog_reboot_max_per_day"],
    )
