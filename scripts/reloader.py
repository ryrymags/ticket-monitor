#!/usr/bin/env python3
"""Auto-restart monitor service when local code/config files change."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config import MonitorConfig, load_config
from src.notifier import DiscordNotifier
from src.state import MonitorState

MONITOR_LABEL = "com.ticketmonitor"
LOG_FILE = ROOT_DIR / "logs" / "reloader.log"


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] reloader: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _launchctl_target(label: str = MONITOR_LABEL) -> str:
    return f"gui/{os.getuid()}/{label}"


def _gather_files(root_dir: Path, globs: list[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in globs:
        matches = root_dir.glob(pattern)
        for path in matches:
            if path.is_file():
                paths.add(path.resolve())
    return sorted(paths)


def compute_fingerprint(root_dir: Path, globs: list[str]) -> str:
    hasher = hashlib.sha256()
    files = _gather_files(root_dir, globs)
    for path in files:
        rel_path = path.relative_to(root_dir).as_posix()
        hasher.update(rel_path.encode("utf-8"))
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 64)
                if not chunk:
                    break
                hasher.update(chunk)
    return hasher.hexdigest()


def _run_doctor_lite(config_path: str) -> tuple[bool, str]:
    python_bin = ROOT_DIR / "venv" / "bin" / "python"
    cmd = [str(python_bin), str(ROOT_DIR / "monitor.py"), "--doctor-lite", "--config", config_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, ""
    output = (proc.stdout + "\n" + proc.stderr).strip()
    preview = output.splitlines()[-1] if output else "doctor-lite failed"
    return False, preview


def _restart_service() -> bool:
    cmd = ["launchctl", "kickstart", "-k", _launchctl_target()]
    return subprocess.run(cmd).returncode == 0


def run_reloader(config: MonitorConfig, config_path: str) -> int:
    logger = logging.getLogger("reloader")
    if not config.updates_enabled:
        logger.info("Auto-reloader is disabled in config; exiting")
        return 0

    state = MonitorState()
    notifier = DiscordNotifier(
        webhook_url=config.discord_webhook_url,
        username=config.discord_username,
        ping_user_id=config.discord_ping_user_id,
    )

    current_fp = compute_fingerprint(ROOT_DIR, config.updates_watch_globs)
    last_fp = state.get_last_code_fingerprint()
    if not last_fp:
        state.set_last_code_fingerprint(current_fp)
        logger.info("Stored initial code fingerprint baseline")
        return 0

    if current_fp == last_fp:
        logger.debug("No code changes detected")
        return 0

    if config.updates_stability_delay_seconds > 0:
        logger.info("Change detected; waiting %ss for write stability", config.updates_stability_delay_seconds)
        time.sleep(config.updates_stability_delay_seconds)

    stable_fp = compute_fingerprint(ROOT_DIR, config.updates_watch_globs)
    if stable_fp == last_fp:
        logger.info("Fingerprint returned to previous state; skipping restart")
        return 0

    doctor_ok, doctor_message = _run_doctor_lite(config_path)
    if not doctor_ok:
        message = f"Reloader preflight failed. Monitor was not restarted. Details: {doctor_message}"
        logger.error(message)
        previous_type = state.get_last_error_type()
        previous_message = state.get_last_error_message()
        if previous_type != "reloader_preflight_failed" or previous_message != message:
            notifier.send_critical_attention(
                message,
                context={"reason_code": "reloader_preflight_failed"},
                next_steps=[
                    "scripts/monitorctl.sh doctor",
                    "scripts/monitorctl.sh status",
                    "scripts/monitorctl.sh logs",
                ],
            )
        state.set_last_error("reloader_preflight_failed", message)
        return 1

    if not _restart_service():
        message = "Reloader detected code changes but launchctl restart failed."
        logger.error(message)
        previous_type = state.get_last_error_type()
        previous_message = state.get_last_error_message()
        if previous_type != "reloader_restart_failed" or previous_message != message:
            notifier.send_critical_attention(
                message,
                context={"reason_code": "reloader_restart_failed"},
                next_steps=[
                    "scripts/monitorctl.sh restart",
                    "scripts/monitorctl.sh status",
                    "scripts/monitorctl.sh logs",
                ],
            )
        state.set_last_error("reloader_restart_failed", message)
        return 1

    now = datetime.now(timezone.utc)
    state.set_last_code_fingerprint(stable_fp)
    state.set_last_auto_fix_at(now)
    state.clear_last_error()
    notifier.send_auto_fix_action(
        action="code_change_restart",
        reason="Local file change detected and doctor-lite preflight passed.",
        context={"reason_code": "code_change_restart"},
        auto_fix_planned="health_recheck",
    )
    logger.warning("Restarted monitor after local code/config changes")
    return 0


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Local file-change reloader")
    parser.add_argument("--config", default="config.yaml", help="Path to monitor config")
    args = parser.parse_args()

    config = load_config(args.config)
    exit_code = run_reloader(config=config, config_path=args.config)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
