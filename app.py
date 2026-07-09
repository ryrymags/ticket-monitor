#!/usr/bin/env python3
"""Ticket Monitor — GUI

A friendly desktop app for monitoring Ticketmaster Face Value Exchange tickets.
Paste a Ticketmaster event URL, set your seating preferences, connect Discord,
and let the monitor run 24/7 while you get notified the moment tickets appear.

Requirements: customtkinter  (pip install customtkinter)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, simpledialog

from monitor import run_bootstrap_session
from src.history_stats import count_bingo_in_history, count_recent_appearances
from src.preferences import TicketPreferences
from src.state import summarize_check_stats
from src.uptime import (
    current_status as uptime_current_status,
    load_uptime_segments,
    summarize_uptime,
    timeline as uptime_timeline,
)

# ── Appearance ──────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_NAME = "Ticket Monitor"
try:
    from src._version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = "1.3.0"
CONFIG_FILE = "config.yaml"
STATE_FILE = "state.json"
HISTORY_FILE = "ticket_history.json"
UPTIME_FILE = "uptime_log.json"
HISTORY_RENDER_PAGE_SIZE = 25
HISTORY_RENDER_BATCH_SIZE = 8


def uptime_event_file(event_id: str) -> str:
    """Per-concert uptime ledger filename (co-located with uptime_log.json)."""
    return f"uptime_log_{event_id}.json"


def monitor_running_state(launchd_state: bool | None, monitor_proc: subprocess.Popen | None) -> bool:
    """Return whether monitoring is running, preferring launchd when installed."""
    if launchd_state is not None:
        return launchd_state
    return bool(monitor_proc and monitor_proc.poll() is None)


def is_visible_history_entry(entry: dict) -> bool:
    """Return whether a history row should appear in user-facing history views."""
    if not isinstance(entry, dict):
        return False
    if str(entry.get("event_name", "")).strip().lower() == "test":
        return False
    return bool(str(entry.get("event_id", "")).strip())


def visible_history_entries(history: list[dict]) -> list[dict]:
    """Filter local test/example history pollution out of real ticket history."""
    return [entry for entry in history or [] if is_visible_history_entry(entry)]


def monitor_event_status_text(
    ev_state: dict,
    now: datetime,
    *,
    stale_threshold: int = 180,
    manual_action_after_seconds: int = 900,
) -> str:
    """Human status for one event row in the Monitor tab.

    The low stale threshold is an automation signal, not a user-facing emergency.
    Only show orange/red when the stored state says checks are blocked/outage, or
    when the last check is old enough to need manual attention.
    """
    in_outage = bool(ev_state.get("in_outage_state"))
    consec_blocked = int(ev_state.get("consecutive_blocked", 0) or 0)
    last_ts = ev_state.get("last_check")
    action_after = max(int(manual_action_after_seconds or 900), int(stale_threshold or 180))

    if last_ts:
        try:
            dt = datetime.fromisoformat(str(last_ts))
            age_s = max(0, int((now - dt).total_seconds()))
        except Exception:
            age_s = None
        if age_s is not None:
            if in_outage:
                return f"🔴  Blocked / no data — recovering (last check {age_s}s ago)"
            if consec_blocked > 0:
                return f"🟠  Blocked — retrying ({consec_blocked} in a row, last check {age_s}s ago)"
            if age_s >= action_after:
                return f"🔴  Last check: {age_s//60}m ago (needs attention)"
            if age_s < stale_threshold:
                return f"🟢  Last check: {age_s}s ago"
            return f"⚪  Last check: {age_s//60}m ago"
        return "⚪  Status unknown"
    if in_outage:
        return "🔴  Blocked / no data — recovering"
    if consec_blocked > 0:
        return f"🟠  Blocked — retrying ({consec_blocked} in a row)"
    return "⚪  Not yet checked"


LOG_FILE = os.path.join("logs", "monitor.log")
LAUNCHD_MONITOR_LABEL = "com.ticketmonitor"
LAUNCHD_OUT_LOG = os.path.join("logs", "launchd.out.log")
LAUNCHD_MONITOR_PLIST = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCHD_MONITOR_LABEL}.plist"
)
# Held (flock) for the GUI's whole lifetime. Two jobs: only one GUI instance can
# ever run, and the guardian probes this lock to enforce "monitoring only runs
# while the app is open" — if the GUI dies (even a crash releases an flock), the
# guardian stops the whole monitoring stack instead of keeping it alive headless.
GUI_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "gui.lock"
)


def acquire_gui_single_instance_lock(lock_path: str = GUI_LOCK_FILE):
    """Take the exclusive GUI lock. Returns the open handle to keep alive for the
    process lifetime, or None when another GUI instance already holds it.
    Cross-platform via src.state's lock shim (fcntl on Unix, msvcrt on Windows)."""
    from src.state import try_lock_file_exclusive

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    if not try_lock_file_exclusive(handle):
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle

# Colors
COLOR_GREEN = "#2ECC71"
COLOR_ORANGE = "#F39C12"
COLOR_RED = "#E74C3C"
COLOR_BLUE = "#3498DB"
COLOR_GRAY = "#7F8C8D"
COLOR_BG_PANEL = "#1e1e2e"
COLOR_BG_SIDEBAR = "#181825"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def resource_path(rel: str) -> str:
    """Return absolute path relative to app root (works for frozen + dev)."""
    base = getattr(sys, "_MEIPASS", Path(__file__).parent)
    return os.path.join(base, rel)


def detect_chrome_path() -> str:
    """Auto-detect Google Chrome executable for this platform."""
    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def load_yaml_raw(path: str) -> dict[str, Any]:
    """Load a YAML file without importing the full monitor config stack."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def save_yaml_raw(path: str, data: dict[str, Any]):
    """Write a YAML file."""
    import yaml
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def inspect_tm_login(user_data_dir: str) -> tuple[bool | None, str]:
    """Check whether the Chrome profile is logged into Ticketmaster by reading its
    cookie DB directly (read-only — no browser launch, so no profile-lock conflict with
    a running monitor). Returns (logged_in, detail); logged_in is None when the profile
    couldn't be read.

    Looks for durable cookies on the auth/identity domains; their presence with a future
    expiry is the reliable logged-in marker.
    """
    import sqlite3

    cookies_db = Path(user_data_dir) / "Default" / "Cookies"
    if not cookies_db.exists():
        return False, "No saved login yet — click 'Log In to Ticketmaster' below."

    # 1601 epoch → unix seconds: subtract seconds between 1601-01-01 and 1970-01-01.
    EPOCH_DIFF = 11644473600
    now_us = (datetime.now(timezone.utc).timestamp() + EPOCH_DIFF) * 1_000_000

    conn = None
    try:
        # immutable=1 lets us read even while Chrome/the monitor holds the profile.
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro&immutable=1", uri=True, timeout=2)
        rows = conn.execute(
            "SELECT expires_utc FROM cookies "
            "WHERE (host_key LIKE '%auth.ticketmaster.com' "
            "   OR host_key LIKE '%identity.ticketmaster.com') "
        ).fetchall()
    except Exception as exc:
        return None, f"Couldn't read the Chrome profile ({exc}). Try again in a moment."
    finally:
        if conn is not None:
            conn.close()

    # A future or session (0) expiry counts as a live auth cookie.
    live = [r[0] for r in rows if (r[0] == 0 or r[0] > now_us)]
    if not live:
        return False, "Not logged in (no valid Ticketmaster session) — click 'Log In to Ticketmaster'."

    dated = [e for e in live if e and e > now_us]
    if dated:
        soonest = min(dated)
        expiry = datetime.fromtimestamp(soonest / 1_000_000 - EPOCH_DIFF, tz=timezone.utc).astimezone()
        return True, f"Logged in to Ticketmaster — session valid until {expiry:%b %d, %Y}."
    return True, "Logged in to Ticketmaster (session cookies present)."


def python_exe() -> str:
    """Return the Python executable to use for subprocess calls."""
    # Prefer the venv Python if it exists.
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.join("venv", "Scripts", "python.exe"),
            sys.executable,
        ]
    else:
        candidates = [
            os.path.join("venv", "bin", "python3"),
            os.path.join("venv", "bin", "python"),
            sys.executable,
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return sys.executable


# ─────────────────────────────────────────────────────────────────────────────
# Config builder — turns GUI fields into config.yaml dict
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "discord": {
        "webhook_url": "",
        "username": "Ticket Monitor",
        "ping_user_id": "",
    },
    "ntfy": {
        "enabled": False,
        "topic": "",
        "priority": "high",
    },
    "events": [],
    "preferences": {
        "name": "BINGO 1",
        "min_tickets": 1,
        "max_price_per_ticket": 500.0,
        "preferred_sections": [],
        "require_preferred_only": False,
        "alert_on_any_availability": True,
    },
    "bingo_configs": [
        {
            "name": "BINGO 1",
            "min_tickets": 1,
            "max_price_per_ticket": 500.0,
            "preferred_sections": [],
            "require_preferred_only": False,
            "alert_on_any_availability": True,
        }
    ],
    "browser": {
        "session_mode": "persistent_profile",
        "storage_state_path": "secrets/tm_storage_state.json",
        "user_data_dir": "secrets/tm_profile",
        "channel": "chrome",
        "poll_min_seconds": 60,
        "poll_max_seconds": 120,
        "per_event_scheduler_enabled": True,
        "per_event_poll_min_seconds": 60,
        "per_event_poll_max_seconds": 120,
        "per_event_min_gap_between_checks_seconds": 60,
        "per_event_max_gap_between_checks_seconds": 120,
        "event_weights": {
            "EXAMPLEEVENT0001": 2,
            "EXAMPLEEVENT0002": 1,
        },
        "headless": False,
        "reuse_event_tabs": True,
        "single_event_page": True,
        "event_dwell_min_seconds": 3,
        "event_dwell_max_seconds": 8,
        "homepage_warmup_interval_seconds": 1800,
        "navigation_timeout_seconds": 20,
        "challenge_threshold": 5,
        "challenge_retry_seconds": 60,
        "challenge_cooldown_base_seconds": 60,
        "challenge_cooldown_max_seconds": 300,
        "challenge_cooldown_escalate_after": 6,
        "challenge_cooldown_tiers_seconds": [300, 900, 1800],
        "challenge_cooldown_tier_every": 3,
        "startup_grace_seconds": 180,
        "event_stagger_seconds": 6,
        "cdp_endpoint_url": "http://127.0.0.1:9222",
        "cdp_connect_timeout_seconds": 10,
        "poll_interval_seconds": 12,
        "poll_jitter_seconds": 2,
        "adaptive_backoff_enabled": True,
        "adaptive_backoff_multiplier": 2.0,
        "adaptive_recover_factor": 0.5,
        "adaptive_max_seconds": 300,
        "stealth_enabled": True,
        "locale": "en-US",
        "timezone_id": "America/New_York",
    },
    "browser_host": {
        "enabled": False,
        "chrome_executable_path": detect_chrome_path(),
        "user_data_dir": "secrets/tm_chrome_profile",
        "remote_debugging_port": 9222,
    },
    "alerts": {
        "ticket_cooldown_seconds": 180,
        "operational_heartbeat_hours": 6,
        "event_check_stale_seconds": 360,
        "operational_state_cooldown_seconds": 1800,
        "non_bingo_enabled": False,
        "manual_action_after_seconds": 900,
        "operational_to_discord": False,
    },
    "polling": {
        "timezone": "US/Eastern",
        "backoff_multiplier": 2.0,
        "max_backoff_seconds": 120,
    },
    "self_heal": {
        "browser_restart_threshold": 3,
        "browser_restart_window_seconds": 600,
        "process_restart_threshold": 6,
        "process_restart_window_seconds": 1800,
        "error_alert_cooldown_seconds": 1800,
    },
    "auth": {
        "auto_login_enabled": False,
        "keychain_service": "ticket-monitor",
        "keychain_email_account": "ticketmaster-email",
        "keychain_password_account": "ticketmaster-password",
        "max_auto_login_attempts_per_hour": 3,
        "auto_login_cooldown_seconds": 1800,
        "session_health_check_interval_seconds": 3600,
        "session_health_check_url": "https://www.ticketmaster.com/my-account",
        "session_recheck_base_seconds": 120,
        "session_recheck_max_seconds": 900,
        "session_logout_confirmations_required": 2,
    },
    "watchdog": {
        "enabled": True,
        "interval_seconds": 120,
        "stale_after_seconds": 600,
        "max_fix_attempts_per_hour": 6,
    },
    "updates": {
        "enabled": True,
        "interval_seconds": 60,
        "stability_delay_seconds": 20,
        "watch_globs": ["monitor.py", "src/**/*.py", "config.yaml", "requirements.txt"],
    },
    "macos": {
        "prevent_idle_sleep": True,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/monitor.log",
        "max_file_size_mb": 10,
        "backup_count": 3,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────────────────────────────────────

class TicketMonitorApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("960x680")
        self.minsize(860, 580)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # State
        self._monitor_proc: subprocess.Popen | None = None
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._log_tail_thread: threading.Thread | None = None
        self._log_pos = 0
        self._status_poll_id: str | None = None
        self._events: list[dict[str, str]] = []  # [{event_id, name, date, url}]
        self._current_tab: str = "events"
        self._history_sig: tuple | None = None  # ((mtime, size), render_limit) last rendered
        self._history_render_limit = HISTORY_RENDER_PAGE_SIZE
        self._history_render_after_id: str | None = None
        self._history_render_generation = 0
        self._uptime_outage_sig: tuple | None = None  # signature of last-rendered outages

        # Load or init config
        self._cfg: dict[str, Any] = {}
        self._load_config()

        # Build UI
        self._build_layout()
        self._populate_from_config()

        # Start periodic status refresh
        self._schedule_status_poll()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_layout(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # Sidebar
        self._sidebar = ctk.CTkFrame(self, width=180, corner_radius=0, fg_color=COLOR_BG_SIDEBAR)
        self._sidebar.grid(row=0, column=0, sticky="nsew")
        self._sidebar.grid_rowconfigure(10, weight=1)

        logo_label = ctk.CTkLabel(
            self._sidebar, text="🎫  Ticket\nMonitor",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("events", "🎵  Events"),
            ("preferences", "🎫  Preferences"),
            ("notifications", "🔔  Notifications"),
            ("login", "🔐  Login"),
            ("history", "📋  History"),
            ("uptime", "📊  Uptime"),
            ("monitor", "▶   Monitor"),
        ]
        for i, (key, label) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                self._sidebar,
                text=label,
                anchor="w",
                corner_radius=8,
                height=40,
                fg_color="transparent",
                text_color=("gray70", "gray90"),
                hover_color=("gray30", "gray25"),
                command=lambda k=key: self._show_tab(k),
            )
            btn.grid(row=i, column=0, padx=10, pady=3, sticky="ew")
            self._nav_buttons[key] = btn

        ver_label = ctk.CTkLabel(
            self._sidebar, text=f"v{APP_VERSION}",
            font=ctk.CTkFont(size=11), text_color="gray50",
        )
        ver_label.grid(row=11, column=0, padx=10, pady=(0, 12), sticky="sw")

        # Main content area
        self._content = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self._content.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

        # Build tab frames
        self._tabs: dict[str, ctk.CTkFrame] = {}
        self._build_events_tab()
        self._build_preferences_tab()
        self._build_notifications_tab()
        self._build_login_tab()
        self._build_history_tab()
        self._build_uptime_tab()
        self._build_monitor_tab()

        # Bottom status bar
        self._statusbar = ctk.CTkFrame(self, height=44, corner_radius=0, fg_color=COLOR_BG_SIDEBAR)
        self._statusbar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._statusbar.grid_columnconfigure(1, weight=1)
        self._status_dot = ctk.CTkLabel(self._statusbar, text="⬤", font=ctk.CTkFont(size=14), text_color=COLOR_GRAY)
        self._status_dot.grid(row=0, column=0, padx=(14, 4), pady=10)
        self._status_label = ctk.CTkLabel(self._statusbar, text="Monitor stopped", anchor="w")
        self._status_label.grid(row=0, column=1, padx=0, pady=10, sticky="w")
        self._start_stop_btn = ctk.CTkButton(
            self._statusbar, text="▶  Start Monitor",
            width=150, height=30,
            fg_color=COLOR_GREEN, hover_color="#27ae60",
            command=self._toggle_monitor,
        )
        self._start_stop_btn.grid(row=0, column=2, padx=14, pady=7)

        # Show events tab first
        self._show_tab("events")

    def _show_tab(self, key: str):
        for k, frame in self._tabs.items():
            frame.grid_remove()
        self._tabs[key].grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        # Highlight active nav button
        for k, btn in self._nav_buttons.items():
            btn.configure(
                fg_color=("gray25", "gray20") if k == key else "transparent",
                text_color=("white", "white") if k == key else ("gray70", "gray90"),
            )
        self._current_tab = key
        # Refresh data-backed tabs on entry so they're fresh the moment you open them
        # (the periodic poll only touches whichever tab is currently visible).
        if key == "history":
            self._refresh_history_tab()
        elif key == "uptime":
            self._refresh_uptime_tab()

    # ── Events Tab ───────────────────────────────────────────────────────────

    def _build_events_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["events"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        _section_header(frame, "🎵  Events to Monitor", row=0)

        ctk.CTkLabel(
            frame,
            text="Paste Ticketmaster event URLs below. The monitor will check all of them 24/7.",
            text_color="gray60", wraplength=600, justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 8), sticky="w")

        # Event list container
        self._events_list_frame = ctk.CTkScrollableFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._events_list_frame.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="nsew")
        self._events_list_frame.grid_columnconfigure(0, weight=1)
        self._event_rows: list[dict] = []  # each: {frame, id_var, name_var, date_var, url_var}

        add_btn = ctk.CTkButton(
            frame, text="＋  Add Event URL", width=160,
            fg_color=COLOR_BLUE, hover_color="#2980b9",
            command=self._add_event_dialog,
        )
        add_btn.grid(row=3, column=0, padx=20, pady=(0, 14), sticky="w")

    def _refresh_event_rows(self):
        for widget in self._events_list_frame.winfo_children():
            widget.destroy()
        self._event_rows.clear()

        if not self._events:
            ctk.CTkLabel(
                self._events_list_frame,
                text="No events added yet.\nClick '＋ Add Event URL' to get started.",
                text_color="gray50", justify="center",
            ).grid(row=0, column=0, padx=20, pady=40)
            return

        for i, ev in enumerate(self._events):
            row_frame = ctk.CTkFrame(self._events_list_frame, fg_color=("gray17", "gray17"), corner_radius=6)
            row_frame.grid(row=i, column=0, padx=8, pady=4, sticky="ew")
            row_frame.grid_columnconfigure(0, weight=1)

            info_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
            info_frame.grid(row=0, column=0, padx=10, pady=8, sticky="ew")
            info_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                info_frame,
                text=ev.get("name") or ev.get("url", "(Unknown event)"),
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w")

            details = []
            if ev.get("date"):
                details.append(f"📅 {ev['date']}")
            url = ev.get("url", "")
            if url:
                details.append(url[:70] + ("..." if len(url) > 70 else ""))
            ctk.CTkLabel(
                info_frame,
                text="  |  ".join(details),
                text_color="gray55", font=ctk.CTkFont(size=11), anchor="w",
            ).grid(row=1, column=0, sticky="w")

            idx = i
            del_btn = ctk.CTkButton(
                row_frame, text="✕", width=32, height=32,
                fg_color="transparent", hover_color=COLOR_RED,
                text_color="gray60",
                command=lambda ix=idx: self._remove_event(ix),
            )
            del_btn.grid(row=0, column=1, padx=(0, 6), pady=4)

    def _add_event_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Add Event")
        dialog.geometry("560x260")
        dialog.grab_set()
        dialog.lift()

        ctk.CTkLabel(dialog, text="Add a Ticketmaster Event", font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(18, 4))
        ctk.CTkLabel(
            dialog,
            text="Paste the full Ticketmaster event URL.\nThe name and date will be detected automatically.",
            text_color="gray55",
        ).pack(pady=(0, 10))

        url_var = ctk.StringVar()
        url_entry = ctk.CTkEntry(dialog, textvariable=url_var, placeholder_text="https://www.ticketmaster.com/event/...", width=500)
        url_entry.pack(padx=20, pady=(0, 6))
        url_entry.focus_set()

        status_label = ctk.CTkLabel(dialog, text="", text_color="gray55", font=ctk.CTkFont(size=11))
        status_label.pack()

        def do_add():
            url = url_var.get().strip()
            if not url:
                status_label.configure(text="Please paste a URL.", text_color=COLOR_RED)
                return

            # Extract event ID from URL
            event_id = ""
            m = re.search(r"/event/([A-Z0-9]+)", url, re.IGNORECASE)
            if m:
                event_id = m.group(1)
            if not event_id:
                # Try query param or last segment
                m2 = re.search(r"[?&]eid=([A-Z0-9]+)", url, re.IGNORECASE)
                if m2:
                    event_id = m2.group(1)

            if not event_id:
                status_label.configure(text="Couldn't find event ID in that URL. Make sure it's a Ticketmaster event link.", text_color=COLOR_ORANGE)
                # Allow adding anyway with placeholder ID
                event_id = f"custom_{len(self._events)+1}"

            # Try to extract event name from URL slug
            name = _guess_event_name(url)
            date = _guess_event_date(url)

            ev = {"event_id": event_id, "name": name, "date": date, "url": url}
            self._events.append(ev)
            self._refresh_event_rows()
            self._save_config()
            dialog.destroy()

        ctk.CTkButton(dialog, text="Add Event", fg_color=COLOR_BLUE, command=do_add).pack(pady=12)
        dialog.bind("<Return>", lambda e: do_add())

    def _remove_event(self, idx: int):
        if 0 <= idx < len(self._events):
            name = self._events[idx].get("name", "this event")
            if messagebox.askyesno("Remove Event", f"Remove '{name}'?", parent=self):
                del self._events[idx]
                self._refresh_event_rows()
                self._save_config()

    # ── Preferences Tab ───────────────────────────────────────────────────────

    def _build_preferences_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["preferences"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        _section_header(frame, "🎫  BINGO Configs", row=0)

        ctk.CTkLabel(
            frame,
            text="Define one or more ideal-ticket categories. A Discord alert leads with the first\n"
                 "BINGO config that matches, so put your highest-priority category first.",
            text_color="gray60", justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        self._bingo_cards_frame = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self._bingo_cards_frame.grid(row=2, column=0, padx=20, pady=(0, 12), sticky="nsew")
        self._bingo_cards_frame.grid_columnconfigure(0, weight=1)
        self._bingo_config_widgets: list[dict[str, Any]] = []

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.grid(row=3, column=0, padx=20, pady=(0, 14), sticky="w")
        ctk.CTkButton(btn_row, text="＋  Add BINGO Config", command=self._add_bingo_config, fg_color=COLOR_GRAY).pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_row, text="💾  Save Preferences", command=self._save_config, fg_color=COLOR_BLUE).pack(side="left")

        self._render_bingo_config_cards(self._configured_bingo_configs())

    def _blank_bingo_config(self, index: int) -> dict[str, Any]:
        return {
            "name": f"BINGO {index + 1}",
            "min_tickets": 1,
            "max_price_per_ticket": 500.0,
            "preferred_sections": [],
            "require_preferred_only": False,
            "alert_on_any_availability": True,
        }

    def _configured_bingo_configs(self) -> list[dict[str, Any]]:
        raw_configs = self._cfg.get("bingo_configs")
        if isinstance(raw_configs, list) and raw_configs:
            configs = [cfg for cfg in raw_configs if isinstance(cfg, dict)]
        else:
            legacy = self._cfg.get("preferences", {})
            configs = [legacy if isinstance(legacy, dict) else {}]
        if not configs:
            configs = [self._blank_bingo_config(0)]
        return [self._normalized_bingo_config(cfg, i) for i, cfg in enumerate(configs)]

    def _normalized_bingo_config(self, cfg: dict[str, Any], index: int) -> dict[str, Any]:
        blank = self._blank_bingo_config(index)
        merged = {**blank, **cfg}
        sections = merged.get("preferred_sections", [])
        if isinstance(sections, str):
            sections = [s.strip() for s in sections.split(",") if s.strip()]
        elif isinstance(sections, list):
            sections = [str(s).strip() for s in sections if str(s).strip()]
        else:
            sections = []
        merged["preferred_sections"] = sections
        merged["name"] = str(merged.get("name", "")).strip() or f"BINGO {index + 1}"
        merged["min_tickets"] = max(1, int(merged.get("min_tickets", 1)))
        merged["max_price_per_ticket"] = max(25.0, min(750.0, float(merged.get("max_price_per_ticket", 500.0))))
        merged["require_preferred_only"] = bool(merged.get("require_preferred_only", merged.get("require_section_match", False)))
        merged["alert_on_any_availability"] = bool(merged.get("alert_on_any_availability", True))
        return merged

    def _render_bingo_config_cards(self, configs: list[dict[str, Any]]):
        for child in self._bingo_cards_frame.winfo_children():
            child.destroy()
        self._bingo_config_widgets = []
        for i, cfg in enumerate(configs):
            self._build_bingo_config_card(i, cfg, len(configs) > 1)

    def _build_bingo_config_card(self, index: int, cfg: dict[str, Any], allow_remove: bool):
        card = ctk.CTkFrame(self._bingo_cards_frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        card.grid(row=index, column=0, padx=0, pady=(0, 12), sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        name_var = ctk.StringVar(value=str(cfg.get("name", f"BINGO {index + 1}")))
        min_var = ctk.IntVar(value=int(cfg.get("min_tickets", 1)))
        max_price = float(cfg.get("max_price_per_ticket", 500.0))
        max_price_var = ctk.DoubleVar(value=max_price)
        sections_var = ctk.StringVar(value=", ".join(cfg.get("preferred_sections", [])))

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, padx=16, pady=(14, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text=f"Config {index + 1}", font=ctk.CTkFont(weight="bold"), anchor="w").grid(row=0, column=0, padx=(0, 10), sticky="w")
        ctk.CTkEntry(header, textvariable=name_var, placeholder_text="e.g. LOGE pairs", width=220).grid(row=0, column=1, sticky="ew")
        if allow_remove:
            ctk.CTkButton(header, text="Remove", width=84, fg_color=COLOR_GRAY, command=lambda i=index: self._remove_bingo_config(i)).grid(row=0, column=2, padx=(10, 0), sticky="e")

        ctk.CTkLabel(card, text="Tickets needed together", anchor="w").grid(row=1, column=0, padx=18, pady=(8, 4), sticky="w")
        ctk.CTkLabel(
            card,
            text="Minimum adjacent seats in the same section, row, and price group.",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=2, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="w")
        ticket_row = ctk.CTkFrame(card, fg_color="transparent")
        ticket_row.grid(row=1, column=1, padx=12, pady=(8, 4), sticky="e")
        ctk.CTkButton(ticket_row, text="−", width=30, command=lambda i=index: self._adjust_min_tickets(i, -1)).pack(side="left")
        min_label = ctk.CTkLabel(ticket_row, text=str(min_var.get()), width=30, font=ctk.CTkFont(size=14, weight="bold"))
        min_label.pack(side="left", padx=6)
        ctk.CTkButton(ticket_row, text="＋", width=30, command=lambda i=index: self._adjust_min_tickets(i, 1)).pack(side="left")

        _divider(card, row=3)

        ctk.CTkLabel(card, text="Max price per ticket  ($)", anchor="w").grid(row=4, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(card, text="Face value ceiling for this BINGO config.", text_color="gray55", font=ctk.CTkFont(size=11), anchor="w").grid(row=5, column=0, columnspan=2, padx=18, pady=(0, 6), sticky="w")
        price_row = ctk.CTkFrame(card, fg_color="transparent")
        price_row.grid(row=4, column=1, padx=12, pady=(12, 4), sticky="e")
        price_label = ctk.CTkLabel(price_row, text=f"${int(max_price)}", width=55, font=ctk.CTkFont(size=13, weight="bold"))
        price_label.pack(side="left", padx=(0, 6))
        ctk.CTkSlider(
            price_row,
            from_=25,
            to=750,
            number_of_steps=145,
            variable=max_price_var,
            command=lambda value, i=index: self._on_price_slider(i, value),
            width=200,
        ).pack(side="left")

        _divider(card, row=6)

        ctk.CTkLabel(card, text="Preferred sections  (optional)", anchor="w").grid(row=7, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(
            card,
            text="Comma-separated section names, e.g. LOGE, FLOOR, PIT. Leave blank to accept any section.",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=8, column=0, columnspan=2, padx=18, pady=(0, 6), sticky="w")
        ctk.CTkEntry(card, textvariable=sections_var, placeholder_text="e.g. LOGE, FLOOR, PIT", width=300).grid(row=7, column=1, padx=12, pady=(12, 12), sticky="e")

        self._bingo_config_widgets.append(
            {
                "name_var": name_var,
                "min_var": min_var,
                "min_label": min_label,
                "max_price_var": max_price_var,
                "max_price_label": price_label,
                "sections_var": sections_var,
            }
        )

    def _bingo_configs_from_widgets(self) -> list[dict[str, Any]]:
        # Non-BINGO alerts are governed by a single global switch (alerts.non_bingo_enabled),
        # not per-config. Mirror it into each config's alert_on_any_availability so the
        # legacy fields stay consistent; the scheduler enforces the global gate regardless.
        non_bingo = bool(getattr(self, "_non_bingo_var", None) and self._non_bingo_var.get())
        configs: list[dict[str, Any]] = []
        for i, widget in enumerate(self._bingo_config_widgets):
            sections_text = widget["sections_var"].get().strip()
            sections_list = [s.strip() for s in sections_text.split(",") if s.strip()]
            configs.append(
                {
                    "name": widget["name_var"].get().strip() or f"BINGO {i + 1}",
                    "min_tickets": widget["min_var"].get(),
                    "max_price_per_ticket": round(widget["max_price_var"].get(), 2),
                    "preferred_sections": sections_list,
                    "require_preferred_only": False,
                    "alert_on_any_availability": non_bingo,
                }
            )
        return configs or [self._blank_bingo_config(0)]

    def _add_bingo_config(self):
        configs = self._bingo_configs_from_widgets()
        configs.append(self._blank_bingo_config(len(configs)))
        self._render_bingo_config_cards(configs)

    def _remove_bingo_config(self, index: int):
        configs = self._bingo_configs_from_widgets()
        if len(configs) <= 1:
            return
        configs.pop(index)
        self._render_bingo_config_cards(configs)

    def _adjust_min_tickets(self, index: int, delta: int):
        widget = self._bingo_config_widgets[index]
        val = max(1, min(12, widget["min_var"].get() + delta))
        widget["min_var"].set(val)
        widget["min_label"].configure(text=str(val))

    def _on_price_slider(self, index: int, value):
        self._bingo_config_widgets[index]["max_price_label"].configure(text=f"${int(value)}")

    # ── Notifications Tab ─────────────────────────────────────────────────────

    def _build_notifications_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["notifications"] = frame
        frame.grid_columnconfigure(0, weight=1)

        _section_header(frame, "🔔  Discord Notifications", row=0)

        ctk.CTkLabel(
            frame,
            text="Alerts are sent via a Discord Webhook. You'll need a Discord server (free) and a webhook URL.\nClick the help link below if you're not sure how to set this up.",
            text_color="gray60", justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        ctk.CTkButton(
            frame, text="📖  How to create a Discord Webhook  →", width=280,
            fg_color="transparent", text_color=COLOR_BLUE, hover_color="gray20", anchor="w",
            command=lambda: webbrowser.open("https://support.discord.com/hc/en-us/articles/228383668"),
        ).grid(row=2, column=0, padx=20, pady=(0, 10), sticky="w")

        notif_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        notif_frame.grid(row=3, column=0, padx=20, pady=(0, 12), sticky="ew")
        notif_frame.grid_columnconfigure(1, weight=1)

        # Webhook URL
        _field_label(notif_frame, "Webhook URL *", row=0)
        self._webhook_var = ctk.StringVar()
        ctk.CTkEntry(notif_frame, textvariable=self._webhook_var, placeholder_text="https://discord.com/api/webhooks/...", show="").grid(row=0, column=1, padx=12, pady=(16, 6), sticky="ew")

        _divider(notif_frame, row=1)

        # Bot username
        _field_label(notif_frame, "Bot display name", row=2)
        ctk.CTkLabel(notif_frame, text="The name shown in Discord for monitor messages.", text_color="gray55", font=ctk.CTkFont(size=11)).grid(row=3, column=0, columnspan=2, padx=18, pady=(0, 6), sticky="w")
        self._bot_username_var = ctk.StringVar(value="Ticket Monitor")
        ctk.CTkEntry(notif_frame, textvariable=self._bot_username_var, placeholder_text="Ticket Monitor").grid(row=2, column=1, padx=12, pady=(12, 4), sticky="ew")

        _divider(notif_frame, row=4)

        # Ping user ID
        _field_label(notif_frame, "Discord User ID  (for @mentions)", row=5)
        ctk.CTkLabel(
            notif_frame,
            text="Your numeric Discord user ID so the bot can @mention you when tickets appear.\n"
                 "Enable Developer Mode in Discord → Settings → Advanced to find your ID.",
            text_color="gray55", font=ctk.CTkFont(size=11), justify="left",
        ).grid(row=6, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="w")
        self._ping_id_var = ctk.StringVar()
        ctk.CTkEntry(notif_frame, textvariable=self._ping_id_var, placeholder_text="e.g. 123456789012345678").grid(row=5, column=1, padx=12, pady=(12, 4), sticky="ew")

        _divider(notif_frame, row=7)

        # Global non-BINGO alert switch (default OFF). When off, you only get pinged
        # for BINGO matches — never "not a match" / non-preferred-section availability.
        ctk.CTkLabel(notif_frame, text="Alert for non-BINGO availability", anchor="w").grid(row=8, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(
            notif_frame,
            text="When OFF (recommended), you're only notified for true BINGO matches.\n"
                 "When ON, you'll also get 🟡 alerts for tickets in other sections or over budget.",
            text_color="gray55", font=ctk.CTkFont(size=11), justify="left",
        ).grid(row=9, column=0, columnspan=2, padx=18, pady=(0, 10), sticky="w")
        self._non_bingo_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(notif_frame, text="", variable=self._non_bingo_var).grid(row=8, column=1, padx=12, pady=(12, 4), sticky="e")

        # ── ntfy.sh push (optional second channel) ──────────────────────────
        _section_header(frame, "📱  ntfy Push  (optional)", row=4)
        ctk.CTkLabel(
            frame,
            text="A second channel so friends get phone pushes without Discord. They install the\n"
                 "free ntfy app and subscribe to your topic (no account needed).",
            text_color="gray60", justify="left",
        ).grid(row=5, column=0, padx=20, pady=(0, 12), sticky="w")

        ntfy_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        ntfy_frame.grid(row=6, column=0, padx=20, pady=(0, 12), sticky="ew")
        ntfy_frame.grid_columnconfigure(1, weight=1)

        # Enable switch
        ctk.CTkLabel(ntfy_frame, text="Enable ntfy push", anchor="w").grid(row=0, column=0, padx=18, pady=(16, 4), sticky="w")
        self._ntfy_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(ntfy_frame, text="", variable=self._ntfy_enabled_var).grid(row=0, column=1, padx=12, pady=(16, 4), sticky="e")

        _divider(ntfy_frame, row=1)

        # Topic
        _field_label(ntfy_frame, "Topic", row=2)
        ctk.CTkLabel(
            ntfy_frame,
            text="An UNGUESSABLE name — anyone who knows it can read your alerts.\n"
                 "Subscribers open the ntfy app and add this exact topic.",
            text_color="gray55", font=ctk.CTkFont(size=11), justify="left",
        ).grid(row=3, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="w")
        self._ntfy_topic_var = ctk.StringVar()
        ctk.CTkEntry(ntfy_frame, textvariable=self._ntfy_topic_var, placeholder_text="e.g. bingo-tix-a1B2c3D4").grid(row=2, column=1, padx=12, pady=(12, 4), sticky="ew")

        _divider(ntfy_frame, row=4)

        # Priority
        _field_label(ntfy_frame, "Priority", row=5)
        self._ntfy_priority_var = ctk.StringVar(value="high")
        ctk.CTkOptionMenu(
            ntfy_frame, variable=self._ntfy_priority_var,
            values=["urgent", "high", "default", "low", "min"],
        ).grid(row=5, column=1, padx=12, pady=(12, 12), sticky="e")

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.grid(row=7, column=0, padx=20, pady=(0, 14), sticky="w")
        ctk.CTkButton(btn_row, text="💾  Save", command=self._save_config, fg_color=COLOR_BLUE).pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_row, text="🧪  Send Test Message", command=self._test_discord, fg_color=COLOR_GRAY).pack(side="left")

        self._discord_status_label = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=11))
        self._discord_status_label.grid(row=8, column=0, padx=20, pady=0, sticky="w")

    def _test_discord(self):
        self._save_config()
        self._discord_status_label.configure(text="Sending test message...", text_color="gray55")
        self.update()

        def run():
            try:
                result = subprocess.run(
                    [python_exe(), "monitor.py", "--test", "--config", CONFIG_FILE],
                    capture_output=True, text=True, timeout=30,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                )
                ok = result.returncode == 0
                msg = "✅  Test message sent to Discord!" if ok else f"❌  Test failed:\n{(result.stdout + result.stderr)[:200]}"
                color = COLOR_GREEN if ok else COLOR_RED
            except Exception as exc:
                msg = f"❌  Error: {exc}"
                color = COLOR_RED
            # Tk is not thread-safe — marshal the label update to the main loop.
            self.after(0, lambda m=msg, c=color: self._discord_status_label.configure(text=m, text_color=c))

        threading.Thread(target=run, daemon=True).start()

    # ── Login Tab ─────────────────────────────────────────────────────────────

    def _build_login_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["login"] = frame
        frame.grid_columnconfigure(0, weight=1)

        _section_header(frame, "🔐  Ticketmaster Login  (optional)", row=0)

        ctk.CTkLabel(
            frame,
            text="Logging in is optional but recommended.\n\n"
                 "• Without login: The monitor runs anonymously. Ticket availability is still visible,\n"
                 "  but Ticketmaster may occasionally rate-limit anonymous checks.\n\n"
                 "• With login: The monitor uses your saved session to appear more like a normal\n"
                 "  browser, reducing the chance of getting temporarily blocked.\n\n"
                 "Your login is saved locally on your computer only.",
            text_color="gray60", justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        # Status card
        self._login_status_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._login_status_frame.grid(row=2, column=0, padx=20, pady=(0, 16), sticky="ew")
        self._login_status_frame.grid_columnconfigure(0, weight=1)
        self._login_status_icon = ctk.CTkLabel(self._login_status_frame, text="ℹ️", font=ctk.CTkFont(size=28))
        self._login_status_icon.grid(row=0, column=0, padx=20, pady=(16, 4))
        self._login_status_text = ctk.CTkLabel(
            self._login_status_frame,
            text="No session — the monitor will run anonymously. That's fine to start!",
            font=ctk.CTkFont(size=13), text_color="gray60",
        )
        self._login_status_text.grid(row=1, column=0, padx=20, pady=(0, 16))

        login_btn = ctk.CTkButton(
            frame, text="🔑  Log In to Ticketmaster",
            fg_color=COLOR_BLUE, hover_color="#2980b9",
            height=42, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_bootstrap_session,
        )
        login_btn.grid(row=3, column=0, padx=20, pady=(0, 10), sticky="w")

        ctk.CTkButton(
            frame, text="🔍  Verify login", fg_color=COLOR_GRAY, command=self._verify_login,
        ).grid(row=3, column=0, padx=(230, 0), pady=(0, 10), sticky="w")

        ctk.CTkButton(
            frame, text="🖥  Install Desktop shortcuts", fg_color=COLOR_GRAY,
            command=self._install_desktop_shortcuts,
        ).grid(row=3, column=0, padx=(360, 0), pady=(0, 10), sticky="w")

        ctk.CTkLabel(
            frame,
            text="A browser window will open. Log in to Ticketmaster normally, then\ncome back here and click 'Done — I'm logged in'.\n\nYour session is stored locally and reused on every restart — you won't need\nto log in again unless your session expires (usually every few weeks).\n\nThe only thing you ever need to do by hand is log back in if you get signed out —\nuse the double-click “Ticket Monitor Reauth.command” on your Desktop (install it with\nthe button above). Everything else (blocks, slow pages) self-heals automatically.\n\nNote: only one program can use the saved Chrome profile at a time — don't open\nChrome on that profile while the monitor is running.",
            text_color="gray55", justify="left",
        ).grid(row=4, column=0, padx=20, pady=(0, 14), sticky="w")

        self._bootstrap_status_label = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=11))
        self._bootstrap_status_label.grid(row=5, column=0, padx=20, pady=0, sticky="w")

        self._update_login_status()

    def _update_login_status(self):
        cfg = self._cfg
        session_mode = cfg.get("browser", {}).get("session_mode", "persistent_profile")
        if session_mode == "persistent_profile":
            profile_dir = cfg.get("browser", {}).get("user_data_dir", "secrets/tm_profile")
            logged_in, detail = inspect_tm_login(profile_dir)
        else:
            # storage_state mode: presence of the file is the available signal.
            state_path = cfg.get("browser", {}).get("storage_state_path", "secrets/tm_storage_state.json")
            if os.path.exists(state_path):
                logged_in, detail = True, "Session file found — the monitor will use your saved account."
            else:
                logged_in, detail = False, "No session file — click 'Log In to Ticketmaster'."

        if logged_in is True:
            self._login_status_icon.configure(text="✅")
            self._login_status_text.configure(text=detail, text_color=COLOR_GREEN)
        elif logged_in is False:
            self._login_status_icon.configure(text="ℹ️")
            self._login_status_text.configure(
                text=detail + "\nLogin is optional but reduces the chance of being rate-limited.",
                text_color="gray55",
            )
        else:  # None — couldn't read the profile
            self._login_status_icon.configure(text="⚠️")
            self._login_status_text.configure(text=detail, text_color=COLOR_ORANGE)

    def _verify_login(self):
        """Re-run the real login check on demand and surface the detail."""
        self._update_login_status()
        detail = self._login_status_text.cget("text").split("\n")[0]
        self._bootstrap_status_label.configure(text=f"🔍  {detail}", text_color="gray55")

    def _install_desktop_shortcuts(self):
        """Run scripts/install_desktop_shortcuts.sh so the double-click .command files
        (incl. 'Ticket Monitor Reauth.command') land on the Desktop."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(repo_root, "scripts", "install_desktop_shortcuts.sh")
        if not os.path.exists(script):
            self._bootstrap_status_label.configure(text=f"❌  Not found: {script}", text_color=COLOR_RED)
            return
        self._bootstrap_status_label.configure(text="⏳  Installing Desktop shortcuts…", text_color="gray55")
        self.update()

        def run():
            try:
                result = subprocess.run(
                    ["bash", script], capture_output=True, text=True, timeout=60, cwd=repo_root,
                )
                ok = result.returncode == 0
                msg = ("✅  Desktop shortcuts installed (incl. Ticket Monitor Reauth.command)."
                       if ok else f"❌  Install failed: {(result.stdout + result.stderr)[:200]}")
                color = COLOR_GREEN if ok else COLOR_RED
            except Exception as exc:
                msg, color = f"❌  Error: {exc}", COLOR_RED
            self.after(0, lambda: self._bootstrap_status_label.configure(text=msg, text_color=color))

        threading.Thread(target=run, daemon=True).start()

    def _start_bootstrap_session(self):
        if not self._events:
            messagebox.showwarning("No Events", "Please add at least one event first (Events tab) so the browser knows where to go.", parent=self)
            return

        self._save_config()
        self._bootstrap_status_label.configure(text="⏳  Opening browser…", text_color="gray55")
        self.update()

        # Use an in-process thread + Event so the browser opens in the same process.
        # This avoids Windows-specific subprocess/pipe deadlocks that prevented the
        # browser window from appearing when stdout was piped to the parent GUI process.
        self._bootstrap_stop_event = threading.Event()
        self._bootstrap_error: str | None = None

        # Show the "Done" dialog first so the user sees feedback immediately.
        dialog = ctk.CTkToplevel(self)
        dialog.title("Complete Login")
        dialog.geometry("480x220")
        dialog.grab_set()
        dialog.lift()

        dialog_status = ctk.CTkLabel(
            dialog,
            text="Complete your Ticketmaster login in the browser\nthat just opened.",
            font=ctk.CTkFont(size=14),
        )
        dialog_status.pack(pady=(24, 8))
        ctk.CTkLabel(dialog, text="Once you can see your account page normally, come back here.", text_color="gray55").pack()

        def _run_bootstrap():
            try:
                run_bootstrap_session(CONFIG_FILE, stop_event=self._bootstrap_stop_event)
            except SystemExit:
                pass
            except Exception as exc:
                self._bootstrap_error = str(exc)
                # Notify the GUI immediately — browser failed to open.
                self.after(0, lambda: dialog_status.configure(
                    text=f"⚠️  Browser failed to open:\n{self._bootstrap_error}",
                    text_color=COLOR_ORANGE,
                ))

        self._bootstrap_thread = threading.Thread(target=_run_bootstrap, daemon=True)
        self._bootstrap_thread.start()

        def done():
            # Signal the browser thread to save state and close the browser.
            self._bootstrap_stop_event.set()
            # Wait briefly for the thread to finish saving.
            self._bootstrap_thread.join(timeout=20)
            dialog.destroy()
            if self._bootstrap_error:
                self._bootstrap_status_label.configure(
                    text=f"⚠️  Bootstrap failed: {self._bootstrap_error}", text_color=COLOR_ORANGE
                )
            else:
                self._bootstrap_status_label.configure(text="✅  Login saved! You can start monitoring.", text_color=COLOR_GREEN)
            self._update_login_status()

        ctk.CTkButton(dialog, text="✅  Done — I'm Logged In", fg_color=COLOR_GREEN, hover_color="#27ae60", command=done).pack(pady=18)

    # ── History Tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["history"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        _section_header(frame, "📋  Ticket History", row=0)

        desc_row = ctk.CTkFrame(frame, fg_color="transparent")
        desc_row.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="ew")
        desc_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            desc_row,
            text="Every ticket appearance the monitor has detected — BINGO and non-BINGO alike.\n"
                 "Use this to calibrate your budget and section preferences over time.",
            text_color="gray60", justify="left",
        ).grid(row=0, column=0, sticky="w")

        btn_row = ctk.CTkFrame(desc_row, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(
            btn_row, text="↺  Refresh", width=90, height=28,
            fg_color="gray25", hover_color="gray30",
            command=lambda: self._refresh_history_tab(force=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="🗑  Clear", width=80, height=28,
            fg_color="gray25", hover_color=COLOR_RED,
            command=self._clear_history,
        ).pack(side="left")

        # Stat row: BINGO-under-current-configs counter + recent-appearances tally.
        stat_col = ctk.CTkFrame(frame, fg_color="transparent")
        stat_col.grid(row=2, column=0, padx=20, pady=(0, 8), sticky="w")
        self._history_bingo_label = ctk.CTkLabel(
            stat_col, text="🟢 BINGO under current configs: —",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_GREEN, anchor="w",
        )
        self._history_bingo_label.grid(row=0, column=0, pady=(0, 2), sticky="w")
        # Peace-of-mind: how many appearances the monitor has caught recently.
        self._history_seen_label = ctk.CTkLabel(
            stat_col, text="🎟 Tickets seen: —",
            font=ctk.CTkFont(size=12), text_color="gray70", anchor="w",
        )
        self._history_seen_label.grid(row=1, column=0, sticky="w")

        # Scrollable list of history entries
        self._history_list = ctk.CTkScrollableFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._history_list.grid(row=3, column=0, padx=20, pady=(0, 14), sticky="nsew")
        self._history_list.grid_columnconfigure(0, weight=1)

        self._history_empty_label: ctk.CTkLabel | None = None
        self._render_history_placeholder("Open this tab to load ticket history.")

    @staticmethod
    def _format_ts(ts_raw: str) -> str:
        """Format an ISO timestamp for display in the history tab."""
        if not ts_raw:
            return ""
        try:
            dt = datetime.fromisoformat(ts_raw).astimezone()
            _day_fmt = "%#d" if os.name == "nt" else "%-d"
            return dt.strftime(f"%b {_day_fmt}  %I:%M %p")
        except Exception:
            return ts_raw[:16]

    def _current_bingo_configs(self) -> list[TicketPreferences]:
        """Current BINGO configs as TicketPreferences objects (all of them)."""
        raw = self._cfg.get("bingo_configs", [])
        if not isinstance(raw, list) or not raw:
            prefs = self._cfg.get("preferences")
            raw = [prefs] if isinstance(prefs, dict) else []
        configs: list[TicketPreferences] = []
        for cfg in raw:
            if isinstance(cfg, dict):
                try:
                    configs.append(TicketPreferences.from_dict(cfg))
                except Exception:
                    continue
        return configs

    @staticmethod
    def _load_history() -> list[dict]:
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                return history if isinstance(history, list) else []
        except Exception:
            pass
        return []

    def _bingo_history_summary(self) -> dict:
        return count_bingo_in_history(
            visible_history_entries(self._load_history()), self._current_bingo_configs()
        )

    def _update_history_bingo_label(self, history: list[dict]):
        summary = count_bingo_in_history(history, self._current_bingo_configs())
        parts = " · ".join(f"{name}: {n}" for name, n in summary["per_config"].items())
        text = f"🟢 BINGO under current configs: {summary['total']} total"
        if parts:
            text += f"  —  {parts}"
        self._history_bingo_label.configure(text=text)

    @staticmethod
    def _tickets_seen_text(history: list[dict]) -> str:
        """One-line 'tickets seen in 48h / 7d' peace-of-mind summary."""
        now = datetime.now(timezone.utc)
        d2 = count_recent_appearances(history, now, hours=48)
        d7 = count_recent_appearances(history, now, hours=24 * 7)
        return (
            f"🎟 Tickets seen: {d2['total']} in 48h ({d2['bingo']} BINGO) · "
            f"{d7['total']} in 7d ({d7['bingo']} BINGO)"
        )

    def _update_history_seen_label(self, history: list[dict]):
        try:
            self._history_seen_label.configure(text=self._tickets_seen_text(history))
        except Exception:
            pass

    @staticmethod
    def _history_file_sig() -> tuple | None:
        """Cheap change signature for ticket_history.json (mtime, size)."""
        try:
            st = os.stat(HISTORY_FILE)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def _render_history_placeholder(self, text: str):
        self._cancel_history_render()
        for widget in self._history_list.winfo_children():
            widget.destroy()
        self._history_empty_label = ctk.CTkLabel(
            self._history_list,
            text=text,
            text_color="gray50", justify="center",
        )
        self._history_empty_label.grid(row=0, column=0, padx=20, pady=40)

    def _cancel_history_render(self):
        after_id = getattr(self, "_history_render_after_id", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._history_render_after_id = None
        self._history_render_generation += 1

    def _refresh_history_tab(self, force: bool = False):
        """Reload ticket_history.json and re-render the history list.

        Skips the (expensive) widget rebuild when the file hasn't changed since the
        last render — this is what stops the every-10s churn that crashed the tab.
        The Refresh/Clear buttons pass ``force=True``.
        """
        sig = self._history_file_sig()
        render_sig = (sig, self._history_render_limit)
        if not force and render_sig == self._history_sig:
            return
        self._history_sig = render_sig

        self._cancel_history_render()
        for widget in self._history_list.winfo_children():
            widget.destroy()
        self._history_empty_label = None

        history: list[dict] = visible_history_entries(self._load_history())
        self._update_history_bingo_label(history)
        self._update_history_seen_label(history)

        if not history:
            self._history_empty_label = ctk.CTkLabel(
                self._history_list,
                text="No real ticket appearances recorded yet.\nStart the monitor and come back here when alerts fire.",
                text_color="gray50", justify="center",
            )
            self._history_empty_label.grid(row=0, column=0, padx=20, pady=40)
            return

        # Show newest first, but do not materialize the entire history file into
        # Tk widgets at once. Large scrollable frames have crashed macOS Tk before.
        entries = list(reversed(history))
        visible_entries = entries[: self._history_render_limit]

        self._render_history_batch(visible_entries, 0, self._history_render_generation)

        if len(entries) > len(visible_entries):
            self._render_history_more_row(len(visible_entries), len(entries))

    def _render_history_batch(self, entries: list[dict], start: int, generation: int):
        if generation != self._history_render_generation:
            return
        end = min(start + HISTORY_RENDER_BATCH_SIZE, len(entries))
        for i in range(start, end):
            try:
                self._render_history_card(i, entries[i])
            except Exception:
                logging.exception("Failed to render history entry %d", i)
        if end < len(entries):
            self._history_render_after_id = self.after(
                1, lambda: self._render_history_batch(entries, end, generation)
            )
        else:
            self._history_render_after_id = None

    def _render_history_more_row(self, row: int, total: int):
        frame = ctk.CTkFrame(self._history_list, fg_color="transparent")
        frame.grid(row=row, column=0, padx=8, pady=(8, 14), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        shown = min(self._history_render_limit, total)
        ctk.CTkLabel(
            frame,
            text=f"Showing latest {shown} of {total} appearances",
            text_color="gray55",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            frame,
            text="Show older",
            width=110,
            height=28,
            fg_color="gray25",
            hover_color="gray30",
            command=self._show_older_history,
        ).grid(row=0, column=1, padx=(8, 0), sticky="e")

    def _show_older_history(self):
        self._history_render_limit += HISTORY_RENDER_PAGE_SIZE
        self._refresh_history_tab(force=True)

    def _render_history_card(self, i: int, entry: dict):
        is_bingo = bool(entry.get("bingo", False))
        badge_color = COLOR_GREEN if is_bingo else COLOR_ORANGE
        badge_text = "🟢 BINGO" if is_bingo else "🟡 Available"
        event_name = entry.get("event_name", "Unknown event")
        event_date = entry.get("event_date", "")
        # Distinguish a fresh listing from the same one lingering across re-checks.
        seen_count = int(entry.get("seen_count", 1) or 1)
        first_ts = entry.get("first_seen", entry.get("timestamp", ""))
        ts_display = self._format_ts(first_ts)
        if seen_count > 1:
            last_disp = self._format_ts(entry.get("last_seen", first_ts))
            ts_display += f"  · seen {seen_count}× · last {last_disp}"

        # Get listings — new format has "listings" array, old has single fields.
        listings = entry.get("listings", [])
        if not listings:
            # Backward compat: single-listing old format.
            sect = entry.get("section", "?")
            if sect and sect != "?":
                listings = [{
                    "section": sect,
                    "row": entry.get("row", "?"),
                    "price": entry.get("price", 0),
                    "count": entry.get("count", 0),
                }]

        # ── Card frame ────────────────────────────────────────────────
        card = ctk.CTkFrame(
            self._history_list,
            fg_color=("gray17", "gray17"),
            corner_radius=6,
        )
        card.grid(row=i, column=0, padx=8, pady=4, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        # Row 0: badge | event name + date | timestamp
        ctk.CTkLabel(
            card, text=badge_text,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=badge_color, width=80, anchor="center",
        ).grid(row=0, column=0, padx=(10, 6), pady=(8, 2), sticky="w")

        header = event_name[:55]
        if event_date:
            header += f"  ({event_date})"
        ctk.CTkLabel(
            card, text=header,
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=0, column=1, padx=4, pady=(8, 2), sticky="w")

        ctk.CTkLabel(
            card, text=ts_display,
            font=ctk.CTkFont(size=10), text_color="gray50", anchor="e",
        ).grid(row=0, column=2, padx=(4, 10), pady=(8, 2), sticky="e")

        # Rows 1+: one line per listing
        if listings:
            for j, lis in enumerate(listings):
                sect = lis.get("section", "?")
                row_val = lis.get("row", "?")
                price = lis.get("price", 0)
                count = lis.get("count", 0)
                row_str = f" · Row {row_val}" if row_val and row_val != "?" else ""
                line = f"  {sect}{row_str} · {count} ticket{'s' if count != 1 else ''} · ${float(price):,.2f} each"
                ctk.CTkLabel(
                    card, text=line,
                    font=ctk.CTkFont(family="Courier", size=11),
                    text_color="gray60", anchor="w",
                ).grid(row=1 + j, column=0, columnspan=3, padx=(16, 10), pady=(0, 2 if j < len(listings) - 1 else 6), sticky="w")
        else:
            # No structured data — show label as fallback.
            label = entry.get("label", "Tickets detected (no detail)")
            ctk.CTkLabel(
                card, text=f"  {label}",
                font=ctk.CTkFont(size=11), text_color="gray60", anchor="w",
            ).grid(row=1, column=0, columnspan=3, padx=(16, 10), pady=(0, 6), sticky="w")

    def _clear_history(self):
        from tkinter import messagebox as _mb
        if not _mb.askyesno("Clear History", "Delete all ticket history? This cannot be undone.", parent=self):
            return
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
        except Exception as exc:
            _mb.showerror("Error", f"Could not clear history:\n{exc}", parent=self)
            return
        self._history_render_limit = HISTORY_RENDER_PAGE_SIZE
        self._refresh_history_tab(force=True)

    # ── Uptime Tab ────────────────────────────────────────────────────────────

    # state → (emoji, label, color) for the uptime views
    _UPTIME_STYLE = {
        "healthy": ("🟢", "Monitoring", COLOR_GREEN),
        "impaired": ("🟠", "Impaired", COLOR_ORANGE),
        "down": ("⚪", "Down", COLOR_GRAY),
    }
    _UPTIME_REASON_TEXT = {
        "blocked": "blocked by Ticketmaster",
        "challenge": "blocked by Ticketmaster",
        "outage": "blocked by Ticketmaster",
        "stale": "no fresh data",
        "logged_out": "signed out",
        "auth_paused": "auth cooldown",
        "error": "monitor error",
        "offline": "monitor offline",
        "no_internet": "no internet connection",
        "starting": "browser starting up",
    }

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Compact human duration: '6h 9m', '3m', '2d 4h', '45s'."""
        s = int(max(0, seconds))
        d, rem = divmod(s, 86400)
        h, rem = divmod(rem, 3600)
        m, sec = divmod(rem, 60)
        if d:
            return f"{d}d {h}h" if h else f"{d}d"
        if h:
            return f"{h}h {m}m" if m else f"{h}h"
        if m:
            return f"{m}m"
        return f"{sec}s"

    def _build_uptime_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["uptime"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(7, weight=1)

        _section_header(frame, "📊  Uptime & Downtime", row=0)

        ctk.CTkLabel(
            frame,
            text="A timestamped record of when the monitor was actually working.\n"
                 "🟢 Monitoring = Ticketmaster loaded · 🟠 Impaired = no load/stale/signed-out · "
                 "⚪ Down = full sleep, lid close/logout, app stopped, or no internet.",
            text_color="gray60", justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        # Current status
        self._uptime_status_label = ctk.CTkLabel(
            frame, text="⚪  Down", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLOR_GRAY, anchor="w",
        )
        self._uptime_status_label.grid(row=2, column=0, padx=20, pady=(0, 12), sticky="w")

        # 24h + 7d summary blocks (label + stacked bar) live in one container.
        summ_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        summ_frame.grid(row=3, column=0, padx=20, pady=(0, 10), sticky="ew")
        summ_frame.grid_columnconfigure(0, weight=1)
        self._uptime_bars: dict[str, dict] = {}
        for i, (key, title) in enumerate((("24h", "Past 24 hours"), ("7d", "Past 7 days"))):
            ctk.CTkLabel(
                summ_frame, text=title, font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
            ).grid(row=i * 3, column=0, padx=14, pady=(10 if i == 0 else 6, 2), sticky="w")
            text_lbl = ctk.CTkLabel(
                summ_frame, text="—", font=ctk.CTkFont(size=11), text_color="gray70", anchor="w",
            )
            text_lbl.grid(row=i * 3 + 1, column=0, padx=14, pady=(0, 4), sticky="w")
            bar = ctk.CTkFrame(summ_frame, fg_color="gray25", corner_radius=4, height=14)
            bar.grid(row=i * 3 + 2, column=0, padx=14, pady=(0, 12 if i == 1 else 8), sticky="ew")
            bar.grid_propagate(False)
            self._uptime_bars[key] = {"text": text_lbl, "bar": bar, "segs": []}

        # Per-concert breakdown — one healthy%/impaired/down line per event, so you can
        # see which show is getting blocked more. The block above is the combined view.
        ctk.CTkLabel(
            frame, text="By concert (past 24 hours)",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=4, column=0, padx=20, pady=(2, 4), sticky="w")
        self._uptime_per_event_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._uptime_per_event_frame.grid(row=5, column=0, padx=20, pady=(0, 10), sticky="ew")
        self._uptime_per_event_frame.grid_columnconfigure(0, weight=1)

        # Full timeline (healthy + impaired + down) — combined across all concerts.
        ctk.CTkLabel(
            frame, text="Timeline — every stretch of monitoring & downtime (last 7 days, all concerts)",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=6, column=0, padx=20, pady=(0, 4), sticky="w")
        self._uptime_list = ctk.CTkScrollableFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._uptime_list.grid(row=7, column=0, padx=20, pady=(0, 14), sticky="nsew")
        self._uptime_list.grid_columnconfigure(0, weight=1)

        self._refresh_uptime_tab(force=True)

    def _render_uptime_bar(self, bar: ctk.CTkFrame, summ: dict):
        """Draw a stacked healthy/impaired/down bar using place() proportions."""
        for w in bar.winfo_children():
            w.destroy()
        total = summ["total_s"]
        if total <= 0:
            return
        offset = 0.0
        for state_key, secs in (
            ("healthy", summ["healthy_s"]),
            ("impaired", summ["impaired_s"]),
            ("down", summ["down_s"]),
        ):
            frac = secs / total
            if frac <= 0:
                continue
            color = self._UPTIME_STYLE[state_key][2]
            seg = ctk.CTkFrame(bar, fg_color=color, corner_radius=0)
            seg.place(relx=offset, rely=0, relwidth=frac, relheight=1.0)
            offset += frac

    def _refresh_uptime_tab(self, force: bool = False):
        segments = self._load_segments_cached(UPTIME_FILE)
        now = datetime.now(timezone.utc)
        # Whether the monitor is alive right now. Launchd-managed installs do not
        # have a GUI-owned subprocess, so check launchd before falling back to
        # _monitor_proc. This prevents synthetic downtime while the background
        # monitor keeps running through screen lock/display sleep.
        running = self._monitor_running_state()

        # Current status line (always cheap — just a label reconfigure).
        status = uptime_current_status(segments, now, monitor_running=running)
        emoji, label, color = self._UPTIME_STYLE.get(
            status["state"], self._UPTIME_STYLE["down"]
        )
        text = f"{emoji}  {label}"
        reason = status.get("reason")
        if status["state"] != "healthy" and reason:
            text += f" ({self._UPTIME_REASON_TEXT.get(reason, reason)})"
        # Current state is ongoing — show when it started, not a creeping duration.
        since_disp = self._format_ts(status.get("since") or "")
        if since_disp:
            text += f"  ·  Ongoing since {since_disp}"
        self._uptime_status_label.configure(text=text, text_color=color)

        # 24h / 7d summaries + bars.
        for key, hours in (("24h", 24), ("7d", 24 * 7)):
            summ = summarize_uptime(segments, hours=hours, now=now, monitor_running=running)
            widgets = self._uptime_bars[key]
            widgets["text"].configure(
                text=(
                    f"{self._format_duration(summ['healthy_s'])} monitoring · "
                    f"{self._format_duration(summ['impaired_s'])} impaired · "
                    f"{self._format_duration(summ['down_s'])} down  —  "
                    f"{summ['healthy_pct']}% healthy"
                )
            )
            self._render_uptime_bar(widgets["bar"], summ)

        self._render_uptime_per_event(now, running)

        # Full timeline — rebuild only when the set of rows changes. The ongoing
        # row contributes a stable "ONGOING" token (not its creeping end time) so a
        # still-running state doesn't re-render every poll — it only re-renders when
        # the state actually switches and the segment finalizes.
        rows = uptime_timeline(
            segments, hours=24 * 7, min_seconds=120, now=now, monitor_running=running
        )
        sig = tuple(
            (r["start"], r["state"], "ONGOING" if r["ongoing"] else r["end"]) for r in rows
        )
        if not force and sig == self._uptime_outage_sig:
            return
        self._uptime_outage_sig = sig

        # Everything below rebuilds Tk widgets; a bad row or a Tk hiccup must never
        # take down the mainloop (this used to crash the whole GUI).
        try:
            self._rebuild_uptime_timeline(rows, now)
        except Exception:
            logging.exception("Uptime timeline rebuild failed")

    # Timeline rows are capped: with churn-heavy weeks the 7-day list can run to many
    # hundreds of rows, and creating that many CTk widgets stalls the Tk mainloop
    # long enough to look like a crash. The newest rows are the ones that matter.
    _UPTIME_TIMELINE_MAX_ROWS = 100

    def _load_segments_cached(self, path: str) -> list[dict]:
        """load_uptime_segments with an mtime/size cache.

        The GUI polls every 10s but the ledgers only flush every ~30s, so most
        polls can skip re-parsing ~100KB of JSON per file on the Tk main thread."""
        cache = getattr(self, "_uptime_load_cache", None)
        if cache is None:
            cache = self._uptime_load_cache = {}
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        hit = cache.get(path)
        if key is not None and hit is not None and hit[0] == key:
            return hit[1]
        segments = load_uptime_segments(path)
        cache[path] = (key, segments)
        return segments

    def _rebuild_uptime_timeline(self, rows: list[dict], now: datetime):
        # Per-event ledgers, loaded and PARSED once per rebuild — timeline rows use
        # these to say WHICH concert an impairment hit. Parsing here (not per row)
        # matters: it used to be fromisoformat per segment per row, ~1M calls on a
        # churny ledger, all on the Tk main thread.
        self._uptime_event_segs = [
            (
                self._event_short_label(ev),
                self._parse_segments(
                    self._load_segments_cached(uptime_event_file(ev.get("event_id", "")))
                ),
            )
            for ev in self._events
        ]

        # Row widgets are POOLED: created once on first use, then configure()d in
        # place on every later rebuild. Destroying/recreating ~100 CTk cards per
        # rebuild is what used to freeze (and effectively crash) the Tk mainloop.
        if not hasattr(self, "_uptime_row_pool"):
            self._uptime_row_pool: list[dict] = []
            self._uptime_empty_label = None
            self._uptime_overflow_label = None

        if not rows:
            for entry in self._uptime_row_pool:
                entry["card"].grid_remove()
            if self._uptime_overflow_label is not None:
                self._uptime_overflow_label.grid_remove()
            if self._uptime_empty_label is None:
                self._uptime_empty_label = ctk.CTkLabel(
                    self._uptime_list,
                    text="No monitoring recorded in the last 7 days yet.\n"
                         "Start the monitor and every stretch of healthy monitoring,\n"
                         "impairment (such as blocking), and downtime (full sleep, lid close/logout,\n"
                         "app stopped, or no internet) shows up here.",
                    text_color="gray50", justify="center",
                )
            self._uptime_empty_label.grid(row=0, column=0, padx=20, pady=40)
            return
        if self._uptime_empty_label is not None:
            self._uptime_empty_label.grid_remove()

        visible = rows[: self._UPTIME_TIMELINE_MAX_ROWS]
        for i, r in enumerate(visible):
            try:
                self._render_timeline_row(i, r, now)
            except Exception:
                logging.exception("Failed to render timeline row %d", i)
        for entry in self._uptime_row_pool[len(visible):]:
            entry["card"].grid_remove()

        hidden = len(rows) - len(visible)
        if hidden > 0:
            if self._uptime_overflow_label is None:
                self._uptime_overflow_label = ctk.CTkLabel(
                    self._uptime_list, text="", text_color="gray50",
                )
            self._uptime_overflow_label.configure(text=f"… {hidden} older entries not shown")
            self._uptime_overflow_label.grid(
                row=self._UPTIME_TIMELINE_MAX_ROWS, column=0, padx=20, pady=(6, 10)
            )
        elif self._uptime_overflow_label is not None:
            self._uptime_overflow_label.grid_remove()

    @staticmethod
    def _parse_segments(segments: list[dict]) -> list[tuple[datetime, datetime, str]]:
        """Segments as (start, end, state) datetime tuples; unparseable ones dropped."""
        parsed: list[tuple[datetime, datetime, str]] = []
        for seg in segments:
            try:
                start = datetime.fromisoformat(str(seg["start"]))
                end = datetime.fromisoformat(str(seg["end"]))
            except (KeyError, TypeError, ValueError):
                continue
            parsed.append((start, end, str(seg.get("state", ""))))
        return parsed

    @staticmethod
    def _event_short_label(ev: dict) -> str:
        """Short human tag for a concert — the weekday+date ("Tue Jul 28") reads much
        faster in dense rows than the full tour title."""
        date_str = str(ev.get("date") or "")[:10]
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d")
            return parsed.strftime("%a %b %d").replace(" 0", " ")
        except ValueError:
            name = str(ev.get("name") or "?")
            return name if len(name) <= 24 else name[:21] + "…"

    def _timeline_event_breakdown(self, r: dict, now: datetime) -> str:
        """For a non-healthy combined-timeline row, say which concert(s) it hit.

        Each event's own ledger is clipped to the row's window; an event counts as
        affected when most of its recorded time in that window was not healthy."""
        event_segs = getattr(self, "_uptime_event_segs", None)
        if r.get("state") == "healthy" or not event_segs or len(event_segs) < 2:
            return ""
        try:
            win_start = datetime.fromisoformat(str(r["start"]))
            win_end = now if r.get("ongoing") else datetime.fromisoformat(str(r["end"]))
        except (KeyError, TypeError, ValueError):
            return ""

        affected: list[str] = []
        fine: list[str] = []
        for short, segs in event_segs:
            healthy_s = bad_s = 0.0
            for seg_start, seg_end, seg_state in segs:
                overlap = (min(seg_end, win_end) - max(seg_start, win_start)).total_seconds()
                if overlap <= 0:
                    continue
                if seg_state == "healthy":
                    healthy_s += overlap
                else:
                    bad_s += overlap
            if healthy_s + bad_s <= 0:
                continue  # no per-event data for this window (e.g. monitor was down)
            (affected if bad_s > healthy_s else fine).append(short)

        if affected and fine:
            return f"{', '.join(affected)} affected — {', '.join(fine)} kept monitoring"
        if affected and not fine:
            return "all concerts affected"
        return ""

    def _event_uptime_summary(self, event_id: str, hours: int, now: datetime, running: bool) -> dict:
        """Per-concert 24h summary + current state, read from that event's ledger."""
        segs = self._load_segments_cached(uptime_event_file(event_id))
        summ = summarize_uptime(segs, hours=hours, now=now, monitor_running=running)
        status = uptime_current_status(segs, now, monitor_running=running)
        return {"summ": summ, "state": status.get("state", "down"), "has_data": bool(segs)}

    def _render_uptime_per_event(self, now: datetime, running: bool):
        # Build the label widgets once per event list and configure() them on every
        # refresh — destroying/recreating widgets each 10s poll is what churned the
        # Tk mainloop (same pattern as _uptime_bars above).
        event_ids = tuple(ev.get("event_id", "") for ev in self._events)
        if getattr(self, "_uptime_per_event_ids", None) != event_ids:
            self._uptime_per_event_ids = event_ids
            for w in self._uptime_per_event_frame.winfo_children():
                w.destroy()
            self._uptime_per_event_rows = []
            if not self._events:
                ctk.CTkLabel(
                    self._uptime_per_event_frame,
                    text="Add concerts in the Events tab to see per-concert uptime.",
                    text_color="gray50", anchor="w",
                ).grid(row=0, column=0, padx=14, pady=10, sticky="w")
            else:
                for i in range(len(self._events)):
                    title = ctk.CTkLabel(
                        self._uptime_per_event_frame, text="",
                        font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
                        wraplength=560, justify="left",
                    )
                    title.grid(row=i * 2, column=0, padx=14, pady=(10 if i == 0 else 6, 0), sticky="w")
                    detail = ctk.CTkLabel(
                        self._uptime_per_event_frame, text="",
                        font=ctk.CTkFont(size=11), text_color="gray70", anchor="w",
                    )
                    detail.grid(
                        row=i * 2 + 1, column=0, padx=14,
                        pady=(0, 10 if i == len(self._events) - 1 else 4), sticky="w",
                    )
                    self._uptime_per_event_rows.append((title, detail))

        for ev, (title_lbl, detail_lbl) in zip(self._events, getattr(self, "_uptime_per_event_rows", [])):
            eid = ev.get("event_id", "")
            name = ev.get("name") or ev.get("url", "Unknown")
            info = self._event_uptime_summary(eid, 24, now, running)
            summ = info["summ"]
            emoji, _label, color = self._UPTIME_STYLE.get(
                info["state"], self._UPTIME_STYLE["down"]
            )
            short = self._event_short_label(ev)
            title_lbl.configure(text=f"{emoji}  {short} — {name}", text_color=color)
            if info["has_data"]:
                detail = (
                    f"{summ['healthy_pct']}% healthy  ·  "
                    f"{self._format_duration(summ['healthy_s'])} monitoring · "
                    f"{self._format_duration(summ['impaired_s'])} impaired · "
                    f"{self._format_duration(summ['down_s'])} down"
                )
            else:
                detail = "No data yet."
            detail_lbl.configure(text=detail)

    def _render_timeline_row(self, i: int, r: dict, now: datetime):
        """Fill pool row ``i`` with this timeline entry, creating its widgets only
        the first time that pool slot is ever used."""
        pool = self._uptime_row_pool
        if i >= len(pool):
            card = ctk.CTkFrame(self._uptime_list, fg_color=("gray17", "gray17"), corner_radius=6)
            card.grid_columnconfigure(1, weight=1)
            state_lbl = ctk.CTkLabel(
                card, text="",
                font=ctk.CTkFont(size=11, weight="bold"),
                width=110, anchor="w",
            )
            state_lbl.grid(row=0, column=0, padx=(10, 6), pady=8, sticky="w")
            detail_lbl = ctk.CTkLabel(
                card, text="", font=ctk.CTkFont(size=11), text_color="gray70",
                anchor="w", justify="left",
            )
            detail_lbl.grid(row=0, column=1, padx=(4, 10), pady=8, sticky="w")
            pool.append({"card": card, "state": state_lbl, "detail": detail_lbl})

        state = r["state"]
        emoji, label, color = self._UPTIME_STYLE.get(state, self._UPTIME_STYLE["down"])
        start_disp = self._format_ts(r.get("start", ""))
        reason = r.get("reason")
        reason_txt = self._UPTIME_REASON_TEXT.get(reason, reason) if reason else ""
        breakdown = self._timeline_event_breakdown(r, now)

        # Ongoing segments show "Ongoing" instead of a duration that keeps ticking;
        # the real duration only lands once the state switches.
        if r.get("ongoing"):
            detail = f"{start_disp}  →  now   ·   Ongoing"
        else:
            end_disp = self._format_ts(r.get("end", ""))
            dur = self._format_duration(r.get("duration_s", 0))
            detail = f"{start_disp}  →  {end_disp}   ·   {dur}"
        if reason_txt:
            detail += f"   ·   {reason_txt}"
        if breakdown:
            detail += f"\n{breakdown}"

        entry = pool[i]
        entry["state"].configure(text=f"{emoji} {label.upper()}", text_color=color)
        entry["detail"].configure(text=detail)
        entry["card"].grid(row=i, column=0, padx=8, pady=4, sticky="ew")

    # ── Monitor Tab ───────────────────────────────────────────────────────────

    def _build_monitor_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["monitor"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(6, weight=1)

        _section_header(frame, "▶   Monitor", row=0)

        # ── 24/7 uptime notice ────────────────────────────────────────────────
        uptime_frame = ctk.CTkFrame(frame, fg_color="#2a1f00", corner_radius=8)
        uptime_frame.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="ew")
        uptime_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            uptime_frame,
            text="⚠️  macOS launchd keeps monitoring through screen lock/display sleep",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#F39C12", anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(10, 2), sticky="w")
        ctk.CTkLabel(
            uptime_frame,
            text=(
                "With macOS setup, the background monitor prevents idle sleep while the screen can still lock.\n"
                "Full system sleep, lid close, logout, shutdown, or internet loss still pauses checks.\n"
                "Standalone/manual runs still need this app open and the computer awake."
            ),
            font=ctk.CTkFont(size=11), text_color="#c49a2a", anchor="w", justify="left",
        ).grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")

        # ── Monitor health / effectiveness ────────────────────────────────────
        health_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        health_frame.grid(row=2, column=0, padx=20, pady=(0, 8), sticky="ew")
        health_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            health_frame,
            text="📊  Monitor Health",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(10, 2), sticky="w")
        self._health_24h_label = ctk.CTkLabel(
            health_frame, text="Last 24h: no checks recorded yet.",
            font=ctk.CTkFont(size=11), text_color="gray70", anchor="w", justify="left",
        )
        self._health_24h_label.grid(row=1, column=0, padx=14, pady=(0, 2), sticky="w")
        self._health_alltime_label = ctk.CTkLabel(
            health_frame, text="All-time: —",
            font=ctk.CTkFont(size=11), text_color="gray60", anchor="w", justify="left",
        )
        self._health_alltime_label.grid(row=2, column=0, padx=14, pady=(0, 2), sticky="w")
        self._health_bingo_label = ctk.CTkLabel(
            health_frame, text="🟢 BINGOs in history (current configs): —",
            font=ctk.CTkFont(size=11), text_color=COLOR_GREEN, anchor="w", justify="left",
        )
        self._health_bingo_label.grid(row=3, column=0, padx=14, pady=(0, 2), sticky="w")
        # Peace of mind: appearances caught recently (a 0 here across days is a hint
        # to double-check the monitor is actually working).
        self._health_seen_label = ctk.CTkLabel(
            health_frame, text="🎟 Tickets seen: —",
            font=ctk.CTkFont(size=11), text_color="gray70", anchor="w", justify="left",
        )
        self._health_seen_label.grid(row=4, column=0, padx=14, pady=(0, 2), sticky="w")
        # Per-concert healthy% (past 24h), from the same per-event uptime ledgers the
        # Uptime tab reads — so the two tabs always agree on which show is impaired.
        self._health_per_event_label = ctk.CTkLabel(
            health_frame, text="",
            font=ctk.CTkFont(size=11), text_color="gray60", anchor="w", justify="left",
        )
        self._health_per_event_label.grid(row=5, column=0, padx=14, pady=(0, 10), sticky="w")

        # ── Events status panel ───────────────────────────────────────────────
        self._monitor_events_frame = ctk.CTkScrollableFrame(
            frame, fg_color=COLOR_BG_PANEL, corner_radius=8, height=120,
        )
        self._monitor_events_frame.grid(row=3, column=0, padx=20, pady=(0, 8), sticky="ew")
        self._monitor_events_frame.grid_columnconfigure(0, weight=1)
        self._monitor_event_labels: dict[str, ctk.CTkLabel] = {}

        # ── Self-healing info ─────────────────────────────────────────────────
        info_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        info_frame.grid(row=4, column=0, padx=20, pady=(0, 8), sticky="ew")
        info_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            info_frame,
            text="🔧  What the monitor handles automatically vs. what needs you",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")
        ctk.CTkLabel(
            info_frame,
            text=(
                "✅ Auto-handled: browser crashes → restart; slow pages → retry with backoff;\n"
                "   rate-limiting / bot checks → cool-down and retry; session expiry → attempt re-login.\n\n"
                "📳 You get a Discord @mention only when something needs your help:\n"
                "   • Login expired and auto re-login failed → go to the Login tab and log in again\n"
                "   • Monitor stuck for 10+ minutes despite self-healing → restart the app\n\n"
                "💚 A heartbeat message is sent to Discord every few hours to confirm the monitor is alive.\n"
                "   If heartbeats stop, open the app and check the Live Logs.\n\n"
                "🌐 Run on your normal home internet. VPNs (incl. Proton) usually get blocked MORE by\n"
                "   Ticketmaster, and some blocks are normal for the first few minutes after starting.\n\n"
                "🪟 A Chrome window will open while monitoring — that's intentional (a real, visible\n"
                "   browser is far less likely to be blocked than a hidden one). You can minimize it;\n"
                "   just don't close it or open Chrome on the monitor's profile yourself."
            ),
            font=ctk.CTkFont(size=11), text_color="gray60", anchor="w", justify="left",
        ).grid(row=1, column=0, padx=14, pady=(0, 6), sticky="w")

        # One-click fixes — no terminal needed. These mirror scripts/monitorctl.sh.
        fix_row = ctk.CTkFrame(info_frame, fg_color="transparent")
        fix_row.grid(row=2, column=0, padx=14, pady=(0, 6), sticky="w")
        ctk.CTkButton(
            fix_row, text="🔑  Re-authenticate", fg_color=COLOR_BLUE, hover_color="#2980b9",
            command=self._start_bootstrap_session,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            fix_row, text="🩺  Run Doctor", fg_color=COLOR_GRAY,
            command=lambda: self._run_monitorctl("doctor"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            fix_row, text="📋  Status", fg_color=COLOR_GRAY,
            command=lambda: self._run_monitorctl("status"),
        ).pack(side="left")
        self._fix_status_label = ctk.CTkLabel(
            info_frame, text="", font=ctk.CTkFont(size=11), text_color="gray55",
            anchor="w", justify="left",
        )
        self._fix_status_label.grid(row=3, column=0, padx=14, pady=(0, 10), sticky="w")

        # ── Log viewer ────────────────────────────────────────────────────────
        log_header = ctk.CTkFrame(frame, fg_color="transparent")
        log_header.grid(row=5, column=0, padx=20, pady=(0, 4), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            log_header, text="Live Logs",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            log_header, text="Clear", width=60, height=24,
            fg_color="gray25", hover_color="gray30",
            command=self._clear_log,
        ).grid(row=0, column=1)

        self._log_text = ctk.CTkTextbox(
            frame, wrap="word",
            font=ctk.CTkFont(family="Courier", size=11),
            fg_color=COLOR_BG_PANEL, corner_radius=8,
        )
        self._log_text.grid(row=6, column=0, padx=20, pady=(0, 14), sticky="nsew")
        self._log_text.configure(state="disabled")

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # Cap the live-log widget so a 24/7 session doesn't grow the textbox buffer
    # unbounded (RAM creep + sluggish rendering). Keep the most recent N lines.
    _MAX_LOG_LINES = 1500

    def _append_log(self, line: str):
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line)
        # Trim from the top once we exceed the cap so memory/latency stay flat.
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > self._MAX_LOG_LINES:
            self._log_text.delete("1.0", f"{line_count - self._MAX_LOG_LINES}.0")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _run_monitorctl(self, sub: str):
        """Run scripts/monitorctl.sh <sub> from the GUI so the user never needs a
        terminal. Output goes to the status label + Live Logs. Threaded so the UI
        stays responsive (doctor stops/restarts services and can take a while)."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(repo_root, "scripts", "monitorctl.sh")
        if not os.path.exists(script):
            self._fix_status_label.configure(text=f"❌  Not found: {script}", text_color=COLOR_RED)
            return
        self._fix_status_label.configure(text=f"⏳  Running {sub}…", text_color="gray55")
        self.update()

        def run():
            try:
                result = subprocess.run(
                    ["bash", script, sub],
                    capture_output=True, text=True, timeout=180, cwd=repo_root,
                )
                out = (result.stdout + result.stderr).strip()
                ok = result.returncode == 0
                msg = f"{'✅' if ok else '❌'}  {sub}: {'done' if ok else f'exit {result.returncode}'}"
                color = COLOR_GREEN if ok else COLOR_RED
                self.after(0, lambda: self._append_log(f"\n$ monitorctl.sh {sub}\n{out}\n"))
            except Exception as exc:
                msg = f"❌  {sub} error: {exc}"
                color = COLOR_RED
            self.after(0, lambda: self._fix_status_label.configure(text=msg, text_color=color))

        threading.Thread(target=run, daemon=True).start()

    # ── Monitor process management ────────────────────────────────────────────
    #
    # Two modes:
    #  • launchd-managed (dashboard mode): the com.ticketmonitor LaunchAgent owns the
    #    monitor (survives reboots; guardian watches it). The GUI drives it through
    #    monitorctl.sh and tails logs/launchd.out.log. Never spawn a second monitor —
    #    monitor.py's single-instance lock would reject it anyway.
    #  • standalone: no LaunchAgent installed → original behavior (GUI spawns
    #    monitor.py as a subprocess and reads its pipe).

    def _launchd_monitor_state(self) -> bool | None:
        """True/False = LaunchAgent installed and running/stopped; None = not installed.

        "Installed" means the plist exists on disk, even when the service isn't
        currently loaded — `monitorctl stop` boots the services OUT of launchd, so
        judging by loaded-state alone made the next Start silently fall back to a
        bare subprocess (no caffeinate, no KeepAlive, no guardian). That fallback is
        now reserved for setups with no LaunchAgent plist at all.
        """
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_MONITOR_LABEL}"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            result = None
        if result is not None and result.returncode == 0:
            return "state = running" in (result.stdout + result.stderr)
        if os.path.exists(LAUNCHD_MONITOR_PLIST):
            return False  # installed but not loaded — Start must re-bootstrap via monitorctl
        return None

    def _monitor_running_state(self) -> bool:
        return monitor_running_state(self._launchd_monitor_state(), self._monitor_proc)

    def _toggle_monitor(self):
        launchd_state = self._launchd_monitor_state()
        if launchd_state is not None:
            if launchd_state:
                self._append_log("\n--- Stopping launchd-managed monitor ---\n")
                self._run_monitorctl("stop")
            else:
                self._save_config()
                self._append_log("\n--- Starting launchd-managed monitor ---\n")
                self._run_monitorctl("start")
            return
        if self._monitor_proc and self._monitor_proc.poll() is None:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self):
        if not self._events:
            messagebox.showwarning("No Events", "Please add at least one event to monitor (Events tab).", parent=self)
            return
        if not self._cfg.get("discord", {}).get("webhook_url", "").startswith("http"):
            messagebox.showwarning("No Webhook", "Please enter your Discord Webhook URL (Notifications tab).", parent=self)
            return

        self._save_config()
        self._show_tab("monitor")

        self._append_log(f"\n--- Monitor started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

        try:
            self._monitor_proc = subprocess.Popen(
                [python_exe(), "monitor.py", "--config", CONFIG_FILE],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Launch Error", f"Could not start monitor:\n{exc}", parent=self)
            return

        self._log_pos = 0
        self._start_log_tail()
        self._update_status_bar(running=True)

    def _stop_monitor(self):
        if self._monitor_proc:
            try:
                self._monitor_proc.terminate()
                self._monitor_proc.wait(timeout=5)
            except Exception:
                try:
                    self._monitor_proc.kill()
                except Exception:
                    pass
            self._monitor_proc = None
        self._update_status_bar(running=False)
        self._append_log(f"\n--- Monitor stopped {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    def _start_log_tail(self):
        def tail():
            while True:
                proc = self._monitor_proc
                if proc is None:
                    break
                line = proc.stdout.readline() if proc.stdout else ""
                if line:
                    self._log_queue.put(line)
                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(0.1)

        self._log_tail_thread = threading.Thread(target=tail, daemon=True)
        self._log_tail_thread.start()
        self._drain_log_queue()

    def _drain_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        if self._monitor_proc and self._monitor_proc.poll() is None:
            self.after(200, self._drain_log_queue)
        else:
            if self._monitor_proc is not None and self._monitor_proc.poll() is not None:
                self._monitor_proc = None
                self._update_status_bar(running=False)
                self._append_log("\n--- Monitor process exited ---\n")

    # ── Status bar & polling ──────────────────────────────────────────────────

    def _update_status_bar(self, running: bool):
        # Surface HOW the monitor is managed: launchd mode has the guardian,
        # caffeinate, and crash-restart behind it; standalone mode has none of that.
        if os.path.exists(LAUNCHD_MONITOR_PLIST):
            mode_suffix = "  (launchd-managed)"
        else:
            mode_suffix = "  (standalone — no guardian/auto-restart)"
        if running:
            self._status_dot.configure(text_color=COLOR_GREEN)
            self._status_label.configure(
                text=f"Monitor running — checking for tickets…{mode_suffix}"
            )
            self._start_stop_btn.configure(
                text="⏹  Stop Monitor",
                fg_color=COLOR_RED, hover_color="#c0392b",
            )
        else:
            self._status_dot.configure(text_color=COLOR_GRAY)
            self._status_label.configure(text=f"Monitor stopped{mode_suffix}")
            self._start_stop_btn.configure(
                text="▶  Start Monitor",
                fg_color=COLOR_GREEN, hover_color="#27ae60",
            )

    def _schedule_status_poll(self):
        self._poll_status()
        self._status_poll_id = self.after(10_000, self._schedule_status_poll)

    def _poll_launchd_log(self):
        """Append new lines from the launchd monitor log to Live Logs (dashboard mode)."""
        try:
            size = os.path.getsize(LAUNCHD_OUT_LOG)
        except OSError:
            return
        pos = getattr(self, "_launchd_log_pos", None)
        if pos is None or pos > size:
            # First poll (or log rotated): start from the tail, don't dump history.
            self._launchd_log_pos = size
            return
        if size == pos:
            return
        try:
            with open(LAUNCHD_OUT_LOG, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read(min(size - pos, 65536))
                self._launchd_log_pos = fh.tell()
        except OSError:
            return
        if chunk:
            self._append_log(chunk)

    def _poll_status(self):
        launchd_state = self._launchd_monitor_state()
        running = monitor_running_state(launchd_state, self._monitor_proc)
        if launchd_state is not None and running:
            self._poll_launchd_log()
        self._update_status_bar(running)
        self._refresh_monitor_events_panel()
        self._refresh_monitor_health_stats()
        # Only refresh the currently-visible data tab. History/uptime rebuild their
        # widget lists only when the underlying file actually changed (see the
        # signature guards), so this is cheap and avoids the churn that used to
        # rebuild the whole history list every 10s (and crash on macOS Tk).
        if self._current_tab == "history":
            self._refresh_history_tab()
        elif self._current_tab == "uptime":
            self._refresh_uptime_tab()

    def _refresh_monitor_health_stats(self):
        """Update the live "Monitor Health" panel from state.json + history."""
        state = load_state()
        health = state.get("health", {}) or {}
        stats = summarize_check_stats(health, hours=24)
        if stats["total"]:
            self._health_24h_label.configure(
                text=(
                    f"Last 24h: {stats['total']} checks · {stats['healthy_pct']}% healthy · "
                    f"{stats['blocked']} blocked · {stats['challenge']} challenge · "
                    f"{stats['stale']} stale · block-rate {stats['block_pct']}%"
                )
            )
        else:
            self._health_24h_label.configure(text="Last 24h: no checks recorded yet.")

        totals = health.get("check_totals", {}) or {}
        lt_total = sum(int(totals.get(k, 0)) for k in ("healthy", "blocked", "challenge", "stale"))
        if lt_total:
            lt_healthy = round(100.0 * int(totals.get("healthy", 0)) / lt_total, 1)
            self._health_alltime_label.configure(
                text=f"All-time: {lt_total} checks · {lt_healthy}% healthy"
            )
        else:
            self._health_alltime_label.configure(text="All-time: —")

        try:
            history = visible_history_entries(self._load_history())
            summary = count_bingo_in_history(history, self._current_bingo_configs())
            self._health_bingo_label.configure(
                text=f"🟢 BINGOs in history (current configs): {summary['total']}"
            )
            self._health_seen_label.configure(text=self._tickets_seen_text(history))
        except Exception:
            pass

        # Per-concert healthy% (past 24h) — same source as the Uptime tab.
        try:
            now = datetime.now(timezone.utc)
            running = self._monitor_running_state()
            lines = []
            for ev in self._events:
                info = self._event_uptime_summary(ev.get("event_id", ""), 24, now, running)
                name = (ev.get("name") or ev.get("url", "Unknown"))[:40]
                if info["has_data"]:
                    emoji = self._UPTIME_STYLE.get(info["state"], self._UPTIME_STYLE["down"])[0]
                    lines.append(f"{emoji} {name}: {info['summ']['healthy_pct']}% healthy")
                else:
                    lines.append(f"⚪ {name}: no data yet")
            self._health_per_event_label.configure(
                text=("By concert (24h):  " + "     ".join(lines)) if lines else ""
            )
        except Exception:
            self._health_per_event_label.configure(text="")

    def _degraded_banner_text(self, health: dict) -> tuple[str, str] | None:
        """Banner mirrors the monitor's persisted status. Blocks/stale are amber and
        self-healing (no action); only a dead login is red + actionable.
        Returns (text, color) or None when healthy."""
        if not health.get("degraded"):
            return None
        reason = health.get("reason")

        if reason in ("logged_out", "auth_paused"):
            attempted = bool(health.get("attention_alerted"))
            delivered = bool(health.get("attention_alert_delivered"))
            attempts = int(health.get("attention_alert_attempts", 0) or 0)
            head = "🔴  Signed out of Ticketmaster — please log back in."
            fix = ("Double-click “Ticket Monitor Reauth.command” on your Desktop, or use the "
                   "Login tab → “Log In to Ticketmaster”.")
            if delivered:
                tail = "A log-in alert has been sent to Discord."
            elif attempted and attempts >= 6:
                tail = "⚠️ Couldn't reach Discord — check your webhook URL in the Notifications tab."
            elif attempted:
                tail = "Sending a Discord alert…"
            else:
                tail = "This is the only thing that needs you — everything else self-heals."
            return (f"{head}\n{fix}\n{tail}", COLOR_RED)

        # Blocks / stale: self-healing, no action needed → amber, informational.
        if reason == "stale":
            head = "🟠  No fresh data right now — auto-recovery is working on it."
        else:
            head = "🟠  Ticketmaster is blocking checks — recovering automatically."
        tail = "Backing off and retrying until it clears. No action needed."
        return (f"{head}\n{tail}", COLOR_ORANGE)

    def _refresh_monitor_events_panel(self):
        state = load_state()
        events_state = state.get("events", {})
        health = state.get("health", {}) if isinstance(state.get("health"), dict) else {}
        now = datetime.now(timezone.utc)
        try:
            stale_threshold = int(self._cfg.get("alerts", {}).get("event_check_stale_seconds", 180))
        except (TypeError, ValueError):
            stale_threshold = 180
        try:
            manual_action_after = int(self._cfg.get("alerts", {}).get("manual_action_after_seconds", 900))
        except (TypeError, ValueError):
            manual_action_after = 900

        # Widgets are POOLED: built once (rebuilt only when the event list
        # changes), then configure()d in place on every 10s poll. Destroying and
        # recreating CTk widgets each poll is the churn pattern that used to
        # freeze/crash macOS Tk in the History and Uptime tabs.
        self._ensure_monitor_panel_widgets()

        # Top-level health banner mirrors the persisted degraded state (incl.
        # auth-pause and staleness, which the per-event rows can't see).
        banner = self._degraded_banner_text(health)
        if banner is not None:
            text, color = banner
            banner_bg = "#3a1416" if color == COLOR_RED else "#3a2c12"
            self._monitor_banner_frame.configure(fg_color=banner_bg)
            self._monitor_banner_label.configure(text=text, text_color=color)
            self._monitor_banner_frame.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        else:
            self._monitor_banner_frame.grid_remove()

        if not self._events:
            webhook = self._cfg.get("discord", {}).get("webhook_url", "")
            if not webhook:
                self._monitor_placeholder_label.configure(
                    text=(
                        "Welcome to Ticket Monitor!\n\n"
                        "To get started:\n"
                        "  1.  Events — paste a Ticketmaster event URL\n"
                        "  2.  Notifications — add your Discord webhook URL\n"
                        "  3.  Login — log in to Ticketmaster\n"
                        "  4.  Come back here and hit Start Monitor"
                    ),
                    text_color=COLOR_BLUE,
                )
            else:
                self._monitor_placeholder_label.configure(
                    text="No events added yet. Go to the Events tab to add a Ticketmaster URL.",
                    text_color="gray50",
                )
            self._monitor_placeholder_label.grid(row=1, column=0, padx=16, pady=16, sticky="w")
            return
        self._monitor_placeholder_label.grid_remove()

        for ev, (name_lbl, status_lbl) in zip(self._events, self._monitor_event_rows):
            eid = ev.get("event_id", "")
            name = ev.get("name", ev.get("url", "Unknown"))
            status = monitor_event_status_text(
                events_state.get(eid, {}),
                now,
                stale_threshold=stale_threshold,
                manual_action_after_seconds=manual_action_after,
            )
            name_lbl.configure(text=name[:55])
            status_lbl.configure(text=status)

    def _ensure_monitor_panel_widgets(self):
        """Create the pooled banner/placeholder/per-event widgets once, and
        rebuild the per-event rows only when the configured events change."""
        if not hasattr(self, "_monitor_banner_frame"):
            self._monitor_banner_frame = ctk.CTkFrame(
                self._monitor_events_frame, fg_color="#3a2c12", corner_radius=8
            )
            self._monitor_banner_frame.grid_columnconfigure(0, weight=1)
            self._monitor_banner_label = ctk.CTkLabel(
                self._monitor_banner_frame, text="", anchor="w", justify="left",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            self._monitor_banner_label.grid(row=0, column=0, padx=12, pady=10, sticky="w")
            self._monitor_placeholder_label = ctk.CTkLabel(
                self._monitor_events_frame, text="", anchor="w", justify="left",
                font=ctk.CTkFont(size=12),
            )
            self._monitor_event_rows: list[tuple[ctk.CTkLabel, ctk.CTkLabel]] = []
            self._monitor_panel_event_ids: tuple | None = None

        event_ids = tuple(ev.get("event_id", "") for ev in self._events)
        if event_ids == self._monitor_panel_event_ids:
            return
        self._monitor_panel_event_ids = event_ids
        for name_lbl, status_lbl in self._monitor_event_rows:
            name_lbl.destroy()
            status_lbl.destroy()
        self._monitor_event_rows = []
        # Row 0 is the banner slot, row 1 the placeholder; event rows follow.
        for i in range(len(self._events)):
            name_lbl = ctk.CTkLabel(
                self._monitor_events_frame, text="", anchor="w",
                font=ctk.CTkFont(weight="bold"),
            )
            name_lbl.grid(row=2 + i * 2, column=0, padx=16, pady=(10 if i == 0 else 2, 0), sticky="w")
            status_lbl = ctk.CTkLabel(
                self._monitor_events_frame, text="", anchor="w",
                text_color="gray55", font=ctk.CTkFont(size=11),
            )
            status_lbl.grid(row=2 + i * 2 + 1, column=0, padx=16, pady=(0, 2), sticky="w")
            self._monitor_event_rows.append((name_lbl, status_lbl))

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config(self):
        import copy
        import yaml
        self._cfg = copy.deepcopy(DEFAULT_CONFIG)

        # Try to load existing config.yaml
        if os.path.exists(CONFIG_FILE):
            try:
                import yaml as _yaml
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = _yaml.safe_load(f) or {}
                # Deep-merge loaded into defaults
                _deep_merge(self._cfg, loaded)
                if "bingo_configs" not in loaded and isinstance(loaded.get("preferences"), dict):
                    self._cfg["bingo_configs"] = [dict(self._cfg.get("preferences", {}))]
            except Exception:
                pass
        elif os.path.exists("config.example.yaml"):
            # First run — copy defaults from example but don't overwrite
            pass

        # Sync events list
        raw_events = self._cfg.get("events", [])
        self._events = [
            {
                "event_id": str(ev.get("event_id", "")),
                "name": str(ev.get("name", "")),
                "date": str(ev.get("date", "")),
                "url": str(ev.get("url", "")),
            }
            for ev in raw_events
            if ev.get("url") or ev.get("event_id")
        ]

    def _populate_from_config(self):
        """Populate all GUI widgets from self._cfg."""
        discord = self._cfg.get("discord", {})
        self._webhook_var.set(str(discord.get("webhook_url", "")))
        self._bot_username_var.set(str(discord.get("username", "Ticket Monitor")))
        self._ping_id_var.set(str(discord.get("ping_user_id", "")))

        alerts = self._cfg.get("alerts", {})
        self._non_bingo_var.set(bool(alerts.get("non_bingo_enabled", False)))

        ntfy = self._cfg.get("ntfy", {})
        self._ntfy_enabled_var.set(bool(ntfy.get("enabled", False)))
        topics = ntfy.get("topics")
        topic = topics[0] if isinstance(topics, list) and topics else str(ntfy.get("topic", ""))
        self._ntfy_topic_var.set(topic)
        self._ntfy_priority_var.set(str(ntfy.get("priority", "high")) or "high")

        self._render_bingo_config_cards(self._configured_bingo_configs())

        self._refresh_event_rows()
        self._update_login_status()

    def _save_config(self):
        """Collect GUI field values and write config.yaml."""
        cfg = self._cfg

        # Discord
        cfg.setdefault("discord", {})
        cfg["discord"]["webhook_url"] = self._webhook_var.get().strip()
        cfg["discord"]["username"] = self._bot_username_var.get().strip() or "Ticket Monitor"
        cfg["discord"]["ping_user_id"] = self._ping_id_var.get().strip()

        # Events
        cfg["events"] = [
            {
                "event_id": ev["event_id"],
                "name": ev["name"],
                "date": ev["date"],
                "url": ev["url"],
            }
            for ev in self._events
        ]

        # Global non-BINGO alert switch.
        cfg.setdefault("alerts", {})
        cfg["alerts"]["non_bingo_enabled"] = bool(self._non_bingo_var.get())

        # ntfy push. GUI is authoritative for the single-topic form.
        cfg.setdefault("ntfy", {})
        cfg["ntfy"]["enabled"] = bool(self._ntfy_enabled_var.get())
        cfg["ntfy"]["topic"] = self._ntfy_topic_var.get().strip()
        cfg["ntfy"]["priority"] = self._ntfy_priority_var.get().strip() or "high"
        cfg["ntfy"].pop("topics", None)

        # BINGO configs. Keep legacy preferences in sync with the first config.
        bingo_configs = self._bingo_configs_from_widgets()
        cfg["bingo_configs"] = bingo_configs
        cfg["preferences"] = dict(bingo_configs[0])

        try:
            save_yaml_raw(CONFIG_FILE, cfg)
        except Exception as exc:
            messagebox.showerror("Save Error", f"Could not save config:\n{exc}", parent=self)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self):
        # Monitoring must always coincide with the GUI: quitting the app stops the
        # whole launchd stack (monitor + guardian + reloader) too. The guardian also
        # enforces this from its side via the GUI lock, so even a crashed/force-quit
        # GUI can't leave monitoring running headless in the background.
        if self._launchd_monitor_state():
            if not messagebox.askyesno(
                "Quit",
                "Monitoring only runs while this app is open.\n"
                "Quitting will stop ticket monitoring too.\n\nStop monitoring and quit?",
                parent=self,
            ):
                return
            repo_root = os.path.dirname(os.path.abspath(__file__))
            script = os.path.join(repo_root, "scripts", "monitorctl.sh")
            try:
                subprocess.run(
                    ["bash", script, "stop"],
                    capture_output=True, text=True, timeout=90, cwd=repo_root,
                )
            except Exception:
                logging.exception("monitorctl stop failed during app close")
        if self._monitor_proc and self._monitor_proc.poll() is None:
            if not messagebox.askyesno("Quit", "The monitor is running. Stop it and quit?", parent=self):
                return
            self._stop_monitor()
        if self._status_poll_id:
            self.after_cancel(self._status_poll_id)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# UI Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section_header(parent: ctk.CTkFrame, text: str, row: int):
    ctk.CTkLabel(
        parent, text=text,
        font=ctk.CTkFont(size=18, weight="bold"), anchor="w",
    ).grid(row=row, column=0, padx=20, pady=(16, 8), sticky="w")


def _field_label(parent: ctk.CTkFrame, text: str, row: int):
    ctk.CTkLabel(parent, text=text, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
        row=row, column=0, padx=18, pady=(12, 4), sticky="w"
    )


def _divider(parent: ctk.CTkFrame, row: int):
    ctk.CTkFrame(parent, height=1, fg_color="gray25").grid(row=row, column=0, columnspan=2, padx=12, pady=0, sticky="ew")


def _deep_merge(base: dict, override: dict):
    """Merge override into base in-place (recursive)."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _guess_event_name(url: str) -> str:
    """Try to extract a human-readable name from a Ticketmaster URL slug."""
    # e.g. https://www.ticketmaster.com/artist-name-city-state-mm-dd-yyyy/event/...
    m = re.search(r"ticketmaster\.com/([^/?#]+)/", url)
    if not m:
        return "New Event"
    slug = m.group(1)
    # Remove trailing date-like tokens
    slug = re.sub(r"[-–]\d{2}-\d{2}-\d{4}$", "", slug)
    slug = re.sub(r"[-–]\d{4}-\d{2}-\d{2}$", "", slug)
    # Title-case the slug
    return slug.replace("-", " ").title()


def _guess_event_date(url: str) -> str:
    """Try to extract a date string from a Ticketmaster URL."""
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", url)
    if m:
        month, day, year = m.group(1), m.group(2), m.group(3)
        return f"{year}-{month}-{day}"
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", url)
    if m2:
        return m2.group(0)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Make sure we're running from the project root
    project_root = Path(__file__).parent
    os.chdir(project_root)

    # Single instance: a second GUI would fight the first over config writes and
    # monitor control. The handle must stay referenced for the process lifetime.
    gui_lock = acquire_gui_single_instance_lock()
    if gui_lock is None:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                "Ticket Monitor",
                "Ticket Monitor is already running — check your Dock/window list.",
            )
            root.destroy()
        except Exception:
            print("Ticket Monitor is already running.")
        sys.exit(0)

    app = TicketMonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
