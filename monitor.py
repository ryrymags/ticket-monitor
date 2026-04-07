#!/usr/bin/env python3
"""Ticketmaster Face Value Exchange Monitor."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib import metadata

from src.browser_probe import BrowserProbe, BrowserProbeError
from src.config import load_config
from src.notifier import DiscordNotifier
from src.scheduler import BROWSER_RESTART_REQUEST_FILE, MonitorScheduler
from src.session_autofix import AutoFixCredentialError, TicketmasterSessionAutoFixer
from src.state import MonitorState

try:
    from src._version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = "1.3.0"


def setup_logging(log_level: str, log_file: str, max_mb: int, backup_count: int):
    """Configure logging to both console and rotating file."""
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    effective_level = os.environ.get("LOG_LEVEL_OVERRIDE", log_level).upper()
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, effective_level, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backup_count,
    )
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)


def run_test(config_path: str):
    """Test mode: validate config and Discord webhook."""
    print("Running setup checks...\n")
    config = load_config(config_path)

    print("[1/3] Config loaded")
    print(f"      Events: {len(config.events)}")
    print(f"      Poll interval: {config.browser_poll_min_seconds}s - {config.browser_poll_max_seconds}s")
    print()

    print("[2/3] Testing Discord webhook")
    notifier = DiscordNotifier(config.discord_webhook_url, config.discord_username, config.discord_ping_user_id)
    if not notifier.send_test():
        print("      FAILED: Could not send Discord test notification.")
        sys.exit(1)
    print("      Discord webhook working")
    print()

    print("[3/3] Checking browser session prerequisites")
    if config.browser_session_mode == "cdp_attach":
        print(f"      CDP endpoint: {config.browser_cdp_endpoint_url}")
        print(
            "      Browser host required: "
            f"{'yes' if config.browser_host_enabled else 'no'}"
        )
    elif config.browser_session_mode == "persistent_profile":
        if os.path.isdir(config.browser_user_data_dir):
            print(f"      Profile dir found: {config.browser_user_data_dir}")
        else:
            print(
                f"      Missing profile dir: {config.browser_user_data_dir} "
                "(run --bootstrap-session)"
            )
    elif os.path.exists(config.browser_storage_state_path):
        print(f"      Found: {config.browser_storage_state_path}")
    else:
        print(f"      Missing: {config.browser_storage_state_path} (run --bootstrap-session)")
    print()

    print("Setup checks complete.")


def run_bootstrap_session(config_path: str, *, stop_event=None):
    """Run interactive login to generate Playwright browser session state.

    stop_event: optional threading.Event; when provided, the browser stays open
    until the event is set (used by the GUI instead of waiting for stdin).
    """
    config = load_config(config_path)

    # Use the homepage for login — cookies are domain-scoped, so logging in here
    # authenticates all subsequent requests to any ticketmaster.com URL.
    login_url = "https://www.ticketmaster.com/"
    print("Launching browser for one-time Ticketmaster session bootstrap...")
    try:
        if config.browser_session_mode == "cdp_attach":
            BrowserProbe.save_cdp_attach_interactive(
                event_url=login_url,
                cdp_endpoint_url=config.browser_cdp_endpoint_url,
                navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
                stop_event=stop_event,
            )
        elif config.browser_session_mode == "persistent_profile":
            BrowserProbe.save_persistent_profile_interactive(
                event_url=login_url,
                user_data_dir=config.browser_user_data_dir,
                navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
                channel=config.browser_channel,
                stop_event=stop_event,
            )
        else:
            output_path = config.browser_storage_state_path
            BrowserProbe.save_storage_state_interactive(
                event_url=login_url,
                output_path=output_path,
                navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
                stop_event=stop_event,
            )
    except BrowserProbeError as exc:
        print(f"Bootstrap failed: {exc}")
        sys.exit(1)

    if config.browser_session_mode == "cdp_attach":
        print(f"\nCDP session refreshed via: {config.browser_cdp_endpoint_url}")
        return

    if config.browser_session_mode == "persistent_profile":
        print(f"\nPersistent profile initialized: {config.browser_user_data_dir}")
        return

    output_path = config.browser_storage_state_path
    print(f"\nSaved session state: {output_path}")
    print(f"Permissions set to 600: {oct(os.stat(output_path).st_mode & 0o777)}")
    print("\nIf your VM hostname is 'your-vm-host', upload with:")
    print(f"scp {output_path} $VM_USER@$VM_HOST:~/ticket-monitor/{output_path}")


def run_doctor(config_path: str):
    """Run a health check that validates auth/session + probing + Discord."""
    config = load_config(config_path)
    notifier = DiscordNotifier(config.discord_webhook_url, config.discord_username, config.discord_ping_user_id)

    print("Running doctor checks...\n")

    print("[1/3] Checking browser session prerequisites")
    if config.browser_session_mode == "cdp_attach":
        print(f"      OK: cdp endpoint {config.browser_cdp_endpoint_url}")
    elif config.browser_session_mode == "persistent_profile":
        if not os.path.isdir(config.browser_user_data_dir):
            print(f"      FAILED: Missing profile dir {config.browser_user_data_dir}")
            sys.exit(1)
        print(f"      OK: profile dir {config.browser_user_data_dir}")
    else:
        if not os.path.exists(config.browser_storage_state_path):
            print(f"      FAILED: Missing {config.browser_storage_state_path}")
            sys.exit(1)
        print(f"      OK: {config.browser_storage_state_path}")
    _validate_autologin_prereqs(config)

    print("\n[2/3] Starting browser probe")
    probe = BrowserProbe(
        storage_state_path=config.browser_storage_state_path,
        session_mode=config.browser_session_mode,
        user_data_dir=config.browser_user_data_dir,
        channel=config.browser_channel,
        cdp_endpoint_url=config.browser_cdp_endpoint_url,
        cdp_connect_timeout_seconds=config.browser_cdp_connect_timeout_seconds,
        reuse_event_tabs=config.browser_reuse_event_tabs,
        headless=config.browser_headless,
        navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
    )

    try:
        probe.start()
        blocked_count = 0
        for ev in config.events:
            result = probe.check_event(ev.event_id, ev.url)
            print(
                f"      {ev.name}: available={result.available}, blocked={result.blocked}, "
                f"challenge={result.challenge_detected}, signal={result.signal_type.value}"
            )
            if result.blocked or result.challenge_detected:
                blocked_count += 1
        if blocked_count == len(config.events):
            print("      FAILED: All events are blocked/challenged. Session likely expired.")
            sys.exit(1)
    except BrowserProbeError as exc:
        print(f"      FAILED: {exc}")
        sys.exit(1)
    finally:
        probe.close()

    print("\n[3/3] Testing Discord webhook")
    if not notifier.send_test():
        print("      FAILED: Could not send Discord test message.")
        sys.exit(1)
    print("      OK: Discord webhook reachable")

    print("\nDoctor checks passed.")


def run_doctor_lite(config_path: str):
    """Lightweight health check for automation hooks (non-interactive)."""
    print("Running doctor-lite checks...\n")
    config = load_config(config_path)

    print("[1/3] Config loaded")
    print(f"      Config path: {os.path.abspath(config_path)}")
    print(f"      Events configured: {len(config.events)}")

    print("\n[2/3] State file and session prerequisites")
    state = MonitorState()
    _ = state.get_health_snapshot()
    print(f"      State file: {os.path.abspath(state.state_file)}")
    if config.browser_session_mode == "cdp_attach":
        print(f"      CDP endpoint configured: {config.browser_cdp_endpoint_url}")
    elif config.browser_session_mode == "persistent_profile":
        if not os.path.isdir(config.browser_user_data_dir):
            print(f"      FAILED: Missing profile dir {config.browser_user_data_dir}")
            sys.exit(1)
        print(f"      Profile dir found: {config.browser_user_data_dir}")
    else:
        if not os.path.exists(config.browser_storage_state_path):
            print(f"      FAILED: Missing {config.browser_storage_state_path}")
            sys.exit(1)
        print(f"      Storage state found: {config.browser_storage_state_path}")
    _validate_autologin_prereqs(config)

    print("\n[3/3] Browser launchability")
    probe = BrowserProbe(
        storage_state_path=config.browser_storage_state_path,
        session_mode=config.browser_session_mode,
        user_data_dir=config.browser_user_data_dir,
        channel=config.browser_channel,
        cdp_endpoint_url=config.browser_cdp_endpoint_url,
        cdp_connect_timeout_seconds=config.browser_cdp_connect_timeout_seconds,
        reuse_event_tabs=config.browser_reuse_event_tabs,
        headless=config.browser_headless,
        navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
    )
    try:
        probe.start()
    except BrowserProbeError as exc:
        print(f"      FAILED: {exc}")
        sys.exit(1)
    finally:
        probe.close()

    print("      Browser context started and closed successfully")
    print("\nDoctor-lite checks passed.")


def run_health_json(config_path: str):
    """Print machine-readable health output for watchdog/ops checks."""
    config = load_config(config_path)
    state = MonitorState()
    now_utc = datetime.now(timezone.utc)
    stale_threshold = int(config.alerts_event_check_stale_seconds)
    monitor_started = state.get_monitor_start_time()
    browser_host_running = _browser_host_running() if config.browser_session_mode == "cdp_attach" else None
    cdp_connected = _cdp_endpoint_ready(config.browser_cdp_endpoint_url) if config.browser_session_mode == "cdp_attach" else None
    session_mode_effective = config.browser_session_mode
    if config.browser_session_mode == "cdp_attach" and not cdp_connected:
        session_mode_effective = "cdp_attach_disconnected"

    event_health = []
    for event in config.events:
        last_check = state.get_last_check(event.event_id)
        last_check_iso = _isoformat_or_none(last_check)
        last_check_age_seconds: int | None = None
        event_check_stale = False
        if last_check is not None:
            age_raw = int((now_utc - last_check).total_seconds())
            last_check_age_seconds = max(0, age_raw)
            event_check_stale = last_check_age_seconds > stale_threshold
        elif monitor_started is not None:
            startup_age = int((now_utc - monitor_started).total_seconds())
            event_check_stale = startup_age > stale_threshold

        event_health.append(
            {
                "event_id": event.event_id,
                "name": event.name,
                "last_check": last_check_iso,
                "last_check_age_seconds": last_check_age_seconds,
                "event_check_stale": event_check_stale,
                "last_probe_success_at": _isoformat_or_none(state.get_last_probe_success_at(event.event_id)),
                "in_outage_state": state.get_in_outage_state(event.event_id),
                "consecutive_blocked": state.get_consecutive_blocked(event.event_id),
                "last_available_at": _isoformat_or_none(state.get_last_available_at(event.event_id)),
                "last_alert_at": _isoformat_or_none(state.get_last_alert_at(event.event_id)),
            }
        )

    payload = {
        "version": APP_VERSION,
        "timestamp_utc": now_utc.isoformat(),
        "config_path": os.path.abspath(config_path),
        "browser_session_mode": config.browser_session_mode,
        "session_mode_effective": session_mode_effective,
        "browser_host_running": browser_host_running,
        "cdp_connected": cdp_connected,
        "browser_cdp_endpoint_url": config.browser_cdp_endpoint_url,
        "event_check_stale_seconds": stale_threshold,
        "python_version": sys.version.split()[0],
        "playwright_version": _playwright_version(),
        "monitor_started": _isoformat_or_none(monitor_started),
        "last_successful_check": _isoformat_or_none(state.get_last_successful_check()),
        "health": state.get_health_snapshot(),
        "events": event_health,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def run_restart_browser(config_path: str):
    """Request the running monitor process to recycle Playwright browser context."""
    _ = load_config(config_path)
    os.makedirs(os.path.dirname(BROWSER_RESTART_REQUEST_FILE), exist_ok=True)
    with open(BROWSER_RESTART_REQUEST_FILE, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    print(f"Browser restart requested: {BROWSER_RESTART_REQUEST_FILE}")


def run_version(config_path: str):
    """Print runtime/build version details."""
    details = {
        "version": APP_VERSION,
        "python_version": sys.version.split()[0],
        "playwright_version": _playwright_version(),
        "config_path": os.path.abspath(config_path),
    }
    print(json.dumps(details, indent=2, sort_keys=True))


def run_test_ticket_alert(config_path: str):
    """Send a synthetic ticket-available alert to validate mention + payload formatting."""
    config = load_config(config_path)
    notifier = DiscordNotifier(config.discord_webhook_url, config.discord_username, config.discord_ping_user_id)
    event = config.events[0] if config.events else None
    if event is None:
        print("No events configured.")
        sys.exit(1)

    if not config.discord_ping_user_id:
        print("Warning: discord.ping_user_id is empty, so no @mention will be sent.")

    print("Sending synthetic ticket-available alert...")
    sent = notifier.send_ticket_available(
        event_name=event.name,
        event_date=event.date,
        event_url=event.url,
        signal_type="synthetic",
        signal_confidence=1.0,
        price_summary="$123.45 - $234.56",
        section_summary="Section 101, Section 102",
        reason="manual_test",
    )
    if not sent:
        print("FAILED: Could not send synthetic ticket alert.")
        sys.exit(1)
    print("Synthetic ticket alert sent.")


def run_test_ticket_alert_matrix(config_path: str):
    """Send synthetic ticket alerts for both bingo types plus a non-bingo example."""
    config = load_config(config_path)
    notifier = DiscordNotifier(config.discord_webhook_url, config.discord_username, config.discord_ping_user_id)
    event = config.events[0] if config.events else None
    if event is None:
        print("No events configured.")
        sys.exit(1)

    print("Sending synthetic ticket alert matrix (3 examples)...")
    samples = [
        {
            "label": "Type 1 bingo (LOGE 4+ <= $220)",
            "price_summary": "$199.50 - $199.50",
            "section_summary": "LOGE20",
            "listing_summary": "LOGE20 / Row 14 / $199.50 x4",
            "listing_groups": [
                {"section": "LOGE20", "row": "14", "price": 199.5, "count": 4},
            ],
        },
        {
            "label": "Type 2 bingo (3+ < $125)",
            "price_summary": "$99.00 - $120.00",
            "section_summary": "BALCONY301",
            "listing_summary": "BALCONY301 / Row 6 / $120.00 x3",
            "listing_groups": [
                {"section": "BALCONY301", "row": "6", "price": 120.0, "count": 3},
            ],
        },
        {
            "label": "Non-bingo availability",
            "price_summary": "$240.00 - $260.00",
            "section_summary": "LOGE15",
            "listing_summary": "LOGE15 / Row 2 / $250.00 x2",
            "listing_groups": [
                {"section": "LOGE15", "row": "2", "price": 250.0, "count": 2},
            ],
        },
    ]

    all_sent = True
    for sample in samples:
        print(f"  - Sending: {sample['label']}")
        sent = notifier.send_ticket_available(
            event_name=event.name,
            event_date=event.date,
            event_url=event.url,
            signal_type="synthetic",
            signal_confidence=1.0,
            price_summary=sample["price_summary"],
            section_summary=sample["section_summary"],
            reason="manual_test",
            listing_summary=sample["listing_summary"],
            listing_groups=sample["listing_groups"],
            mention=True,
        )
        if not sent:
            all_sent = False
            print(f"FAILED: Could not send sample alert: {sample['label']}")

    if not all_sent:
        sys.exit(1)
    print("Synthetic ticket alert matrix sent.")


def run_monitor(config_path: str, once: bool = False):
    """Start the monitoring loop."""
    config = load_config(config_path)
    setup_logging(config.log_level, config.log_file, config.log_max_file_size_mb, config.log_backup_count)
    logger = logging.getLogger("monitor")

    notifier = DiscordNotifier(config.discord_webhook_url, config.discord_username, config.discord_ping_user_id)
    state = MonitorState()
    start_time = datetime.now(timezone.utc)
    state.set_monitor_start_time(start_time)

    scheduler = MonitorScheduler(
        config=config,
        notifier=notifier,
        state=state,
        start_time=start_time,
    )

    def handle_signal(signum, frame):
        del frame
        logger.info("Received %s — shutting down...", signal.Signals(signum).name)
        scheduler.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if once:
        logger.info("Running single check cycle (--once)")
        try:
            scheduler.run_once()
        except BrowserProbeError as exc:
            logger.error("Check failed: %s", exc)
            sys.exit(1)
        logger.info("Done.")
    else:
        logger.info(
            "Starting monitor — %d event(s), poll=%ss-%ss",
            len(config.events),
            config.browser_poll_min_seconds,
            config.browser_poll_max_seconds,
        )
        scheduler.run()
        logger.info("Monitor stopped.")


def main():
    parser = argparse.ArgumentParser(description="Ticketmaster Face Value Exchange Monitor")
    parser.add_argument("--test", action="store_true", help="Validate config and Discord webhook")
    parser.add_argument("--test-ticket-alert", action="store_true", help="Send a synthetic ticket alert (with @mention if configured)")
    parser.add_argument(
        "--test-ticket-alert-matrix",
        action="store_true",
        help="Send 3 synthetic ticket alerts: LOGE bingo, budget bingo, and non-bingo",
    )
    parser.add_argument("--doctor", action="store_true", help="Validate browser session, probe, and Discord")
    parser.add_argument("--doctor-lite", action="store_true", help="Quick local health check for automation scripts")
    parser.add_argument("--health-json", action="store_true", help="Print machine-readable health JSON")
    parser.add_argument("--bootstrap-session", action="store_true", help="Create Playwright storage state with manual login")
    parser.add_argument("--restart-browser", action="store_true", help="Request browser context recycle without full service restart")
    parser.add_argument("--version", action="store_true", help="Show runtime version info")
    parser.add_argument("--once", action="store_true", help="Run one check cycle and exit")
    parser.add_argument("--config", default="config.yaml", help="Path to config file (default: config.yaml)")
    parser.add_argument("--verbose", action="store_true", help="Override log level to DEBUG")
    args = parser.parse_args()

    if args.verbose:
        os.environ["LOG_LEVEL_OVERRIDE"] = "DEBUG"

    if args.bootstrap_session:
        run_bootstrap_session(args.config)
    elif args.test:
        run_test(args.config)
    elif args.test_ticket_alert:
        run_test_ticket_alert(args.config)
    elif args.test_ticket_alert_matrix:
        run_test_ticket_alert_matrix(args.config)
    elif args.doctor:
        run_doctor(args.config)
    elif args.doctor_lite:
        run_doctor_lite(args.config)
    elif args.health_json:
        run_health_json(args.config)
    elif args.restart_browser:
        run_restart_browser(args.config)
    elif args.version:
        run_version(args.config)
    else:
        run_monitor(args.config, once=args.once)


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _validate_autologin_prereqs(config) -> None:
    if not config.auth_auto_login_enabled:
        return

    print("      Auto-login enabled: validating Keychain credentials...")
    autofixer = TicketmasterSessionAutoFixer(
        keychain_service=config.auth_keychain_service,
        keychain_email_account=config.auth_keychain_email_account,
        keychain_password_account=config.auth_keychain_password_account,
    )
    try:
        autofixer.validate_credentials()
    except AutoFixCredentialError as exc:
        print("      FAILED: Could not read Ticketmaster auto-login credentials from Keychain.")
        print(f"      Detail: {exc}")
        print("      Add credentials with:")
        print(
            f'      security add-generic-password -U -s "{config.auth_keychain_service}" '
            f'-a "{config.auth_keychain_email_account}" -w "YOUR_EMAIL"'
        )
        print(
            f'      security add-generic-password -U -s "{config.auth_keychain_service}" '
            f'-a "{config.auth_keychain_password_account}" -w "YOUR_PASSWORD"'
        )
        sys.exit(1)
    print("      Keychain credentials are readable")


def _browser_host_running() -> bool:
    target = f"gui/{os.getuid()}/com.ticketmonitor.browser-host"
    try:
        proc = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return "state = running" in output


def _cdp_endpoint_ready(endpoint_url: str) -> bool:
    if not endpoint_url:
        return False
    url = endpoint_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return int(getattr(response, "status", 0)) == 200
    except (urllib.error.URLError, ValueError, OSError):
        return False


def _playwright_version() -> str:
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return "not-installed"


if __name__ == "__main__":
    main()
