#!/usr/bin/env python3
"""Ticket Monitor — GUI

A friendly desktop app for monitoring Ticketmaster Face Value Exchange tickets.
Paste a Ticketmaster event URL, set your seating preferences, connect Discord,
and let the monitor run 24/7 while you get notified the moment tickets appear.

Requirements: customtkinter  (pip install customtkinter)
"""

from __future__ import annotations

import json
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
LOG_FILE = os.path.join("logs", "monitor.log")

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
    "events": [],
    "preferences": {
        "min_tickets": 1,
        "max_price_per_ticket": 500.0,
        "preferred_sections": [],
        "require_preferred_only": False,
        "alert_on_any_availability": True,
    },
    "browser": {
        "session_mode": "persistent_profile",
        "storage_state_path": "secrets/tm_storage_state.json",
        "user_data_dir": "secrets/tm_profile",
        "channel": "chrome",
        "poll_min_seconds": 45,
        "poll_max_seconds": 75,
        "headless": True,
        "reuse_event_tabs": True,
        "navigation_timeout_seconds": 20,
        "challenge_threshold": 5,
        "challenge_retry_seconds": 60,
        "event_stagger_seconds": 6,
        "cdp_endpoint_url": "http://127.0.0.1:9222",
        "cdp_connect_timeout_seconds": 10,
        "poll_interval_seconds": 12,
        "poll_jitter_seconds": 2,
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
        "event_check_stale_seconds": 180,
        "operational_state_cooldown_seconds": 1800,
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
    },
    "watchdog": {
        "enabled": True,
        "interval_seconds": 120,
        "stale_after_seconds": 180,
        "max_fix_attempts_per_hour": 6,
    },
    "updates": {
        "enabled": True,
        "interval_seconds": 60,
        "stability_delay_seconds": 20,
        "watch_globs": ["monitor.py", "src/**/*.py", "config.yaml", "requirements.txt"],
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

        _section_header(frame, "🎫  Ticket Preferences", row=0)

        ctk.CTkLabel(
            frame,
            text="Define your ideal tickets. You'll get a 🟢 BINGO alert when something matches, and\n"
                 "a 🟡 secondary alert for anything else (if enabled below).",
            text_color="gray60", justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 12), sticky="w")

        pref_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        pref_frame.grid(row=2, column=0, padx=20, pady=(0, 12), sticky="ew")
        pref_frame.grid_columnconfigure(1, weight=1)

        # Min tickets
        ctk.CTkLabel(pref_frame, text="Tickets needed together", anchor="w").grid(row=0, column=0, padx=18, pady=(16, 4), sticky="w")
        ctk.CTkLabel(
            pref_frame,
            text="Minimum adjacent seats in the same section, row, and price group\n"
                 "(i.e. seats that are guaranteed to be physically next to each other).",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=1, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="w")
        self._min_tickets_var = ctk.IntVar(value=1)
        ticket_row = ctk.CTkFrame(pref_frame, fg_color="transparent")
        ticket_row.grid(row=0, column=1, padx=12, pady=(16, 4), sticky="e")
        ctk.CTkButton(ticket_row, text="−", width=30, command=lambda: self._adjust_min_tickets(-1)).pack(side="left")
        self._min_tickets_label = ctk.CTkLabel(ticket_row, text="1", width=30, font=ctk.CTkFont(size=14, weight="bold"))
        self._min_tickets_label.pack(side="left", padx=6)
        ctk.CTkButton(ticket_row, text="＋", width=30, command=lambda: self._adjust_min_tickets(1)).pack(side="left")

        _divider(pref_frame, row=2)

        # Max price
        ctk.CTkLabel(pref_frame, text="Max price per ticket  ($)", anchor="w").grid(row=3, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(pref_frame, text="Face value ceiling — tickets above this price won't trigger a BINGO.", text_color="gray55", font=ctk.CTkFont(size=11), anchor="w").grid(row=4, column=0, columnspan=2, padx=18, pady=(0, 6), sticky="w")
        price_row = ctk.CTkFrame(pref_frame, fg_color="transparent")
        price_row.grid(row=3, column=1, padx=12, pady=(12, 4), sticky="e")
        self._max_price_var = ctk.DoubleVar(value=300.0)
        self._max_price_label = ctk.CTkLabel(price_row, text="$300", width=55, font=ctk.CTkFont(size=13, weight="bold"))
        self._max_price_label.pack(side="left", padx=(0, 6))
        price_slider = ctk.CTkSlider(price_row, from_=25, to=750, number_of_steps=145, variable=self._max_price_var, command=self._on_price_slider, width=200)
        price_slider.pack(side="left")

        _divider(pref_frame, row=5)

        # Preferred sections
        ctk.CTkLabel(pref_frame, text="Preferred sections  (optional)", anchor="w").grid(row=6, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(
            pref_frame,
            text="Comma-separated section names, e.g.:  LOGE, FLOOR, PIT\n"
                 "When sections are set, BINGO only fires for those sections.\n"
                 "Tip: check the event's seating chart on Ticketmaster for exact abbreviations\n"
                 "(e.g. BALC = Balcony, LOGE = Loge Level, ORCH = Orchestra).\n"
                 "Leave blank to accept any section.",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=7, column=0, columnspan=2, padx=18, pady=(0, 6), sticky="w")
        self._sections_var = ctk.StringVar()
        ctk.CTkEntry(pref_frame, textvariable=self._sections_var, placeholder_text="e.g. LOGE, FLOOR, PIT  — or leave blank", width=300).grid(row=6, column=1, padx=12, pady=(12, 4), sticky="e")

        _divider(pref_frame, row=8)

        # Require preferred-section-only alerts
        ctk.CTkLabel(pref_frame, text="Only alert for preferred sections", anchor="w").grid(row=9, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(
            pref_frame,
            text="When on: you'll only ever receive alerts when tickets appear in your\n"
                 "preferred sections (no orange alerts for other sections).\n"
                 "When off: you'll also get 🟡 orange alerts when other sections are available,\n"
                 "so you can see what's out there and update your criteria.",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=10, column=0, columnspan=2, padx=18, pady=(0, 8), sticky="w")
        self._require_section_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(pref_frame, text="", variable=self._require_section_var).grid(row=9, column=1, padx=12, pady=(12, 4), sticky="e")

        _divider(pref_frame, row=11)

        # Alert on any
        ctk.CTkLabel(pref_frame, text="Also alert when other tickets appear", anchor="w").grid(row=12, column=0, padx=18, pady=(12, 4), sticky="w")
        ctk.CTkLabel(
            pref_frame,
            text="When on, you'll get a 🟡 orange Discord alert if tickets appear that don't\n"
                 "fully match your criteria (wrong section, over budget, etc.).\n"
                 "Every appearance — BINGO or not — is also saved to the History tab,\n"
                 "which is great for calibrating your budget and section preferences.",
            text_color="gray55", font=ctk.CTkFont(size=11), anchor="w", justify="left",
        ).grid(row=13, column=0, columnspan=2, padx=18, pady=(0, 12), sticky="w")
        self._alert_any_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(pref_frame, text="", variable=self._alert_any_var).grid(row=12, column=1, padx=12, pady=(12, 4), sticky="e")

        ctk.CTkButton(frame, text="💾  Save Preferences", command=self._save_config, fg_color=COLOR_BLUE).grid(row=3, column=0, padx=20, pady=(0, 14), sticky="w")

    def _adjust_min_tickets(self, delta: int):
        val = max(1, min(12, self._min_tickets_var.get() + delta))
        self._min_tickets_var.set(val)
        self._min_tickets_label.configure(text=str(val))

    def _on_price_slider(self, value):
        self._max_price_label.configure(text=f"${int(value)}")

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

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.grid(row=4, column=0, padx=20, pady=(0, 14), sticky="w")
        ctk.CTkButton(btn_row, text="💾  Save", command=self._save_config, fg_color=COLOR_BLUE).pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_row, text="🧪  Send Test Message", command=self._test_discord, fg_color=COLOR_GRAY).pack(side="left")

        self._discord_status_label = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=11))
        self._discord_status_label.grid(row=5, column=0, padx=20, pady=0, sticky="w")

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
            self._discord_status_label.configure(text=msg, text_color=color)

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

        ctk.CTkLabel(
            frame,
            text="A browser window will open. Log in to Ticketmaster normally, then\ncome back here and click 'Done — I'm logged in'.\n\nYour session is stored locally and reused on every restart — you won't need\nto log in again unless your session expires (usually every few weeks).",
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
            has_session = os.path.isdir(profile_dir) and any(True for _ in Path(profile_dir).iterdir() if True)
        else:
            state_path = cfg.get("browser", {}).get("storage_state_path", "secrets/tm_storage_state.json")
            has_session = os.path.exists(state_path)

        if has_session:
            self._login_status_icon.configure(text="✅")
            self._login_status_text.configure(
                text="Session found! You're logged in — the monitor will use your saved account.",
                text_color=COLOR_GREEN,
            )
        else:
            self._login_status_icon.configure(text="ℹ️")
            self._login_status_text.configure(
                text="No session — running anonymously. This works fine! Login is optional but\n"
                     "reduces the chance of being rate-limited by Ticketmaster.",
                text_color="gray55",
            )

    def _start_bootstrap_session(self):
        if not self._events:
            messagebox.showwarning("No Events", "Please add at least one event first (Events tab) so the browser knows where to go.", parent=self)
            return

        self._save_config()
        self._bootstrap_status_label.configure(text="⏳  Opening browser…", text_color="gray55")
        self.update()

        # We run bootstrap as a subprocess with stdin piped so we can send Enter later.
        self._bootstrap_proc = subprocess.Popen(
            [python_exe(), "monitor.py", "--bootstrap-session", "--config", CONFIG_FILE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )

        # Show the "Done" dialog
        dialog = ctk.CTkToplevel(self)
        dialog.title("Complete Login")
        dialog.geometry("480x200")
        dialog.grab_set()
        dialog.lift()

        ctk.CTkLabel(dialog, text="Complete your Ticketmaster login in the browser\nthat just opened.", font=ctk.CTkFont(size=14)).pack(pady=(24, 8))
        ctk.CTkLabel(dialog, text="Once you can see your account page normally, come back here.", text_color="gray55").pack()

        def done():
            try:
                self._bootstrap_proc.stdin.write(b"\n")
                self._bootstrap_proc.stdin.flush()
                self._bootstrap_proc.stdin.close()
            except Exception:
                pass
            try:
                self._bootstrap_proc.wait(timeout=15)
            except Exception:
                pass
            dialog.destroy()
            self._bootstrap_status_label.configure(text="✅  Login saved! You can start monitoring.", text_color=COLOR_GREEN)
            self._update_login_status()

        ctk.CTkButton(dialog, text="✅  Done — I'm Logged In", fg_color=COLOR_GREEN, hover_color="#27ae60", command=done).pack(pady=18)

    # ── History Tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["history"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

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
            command=self._refresh_history_tab,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="🗑  Clear", width=80, height=28,
            fg_color="gray25", hover_color=COLOR_RED,
            command=self._clear_history,
        ).pack(side="left")

        # Scrollable list of history entries
        self._history_list = ctk.CTkScrollableFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        self._history_list.grid(row=2, column=0, padx=20, pady=(0, 14), sticky="nsew")
        self._history_list.grid_columnconfigure(0, weight=1)

        self._history_empty_label: ctk.CTkLabel | None = None
        self._refresh_history_tab()

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

    def _refresh_history_tab(self):
        """Reload ticket_history.json and re-render the history list."""
        for widget in self._history_list.winfo_children():
            widget.destroy()
        self._history_empty_label = None

        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history: list[dict] = json.load(f)
                if not isinstance(history, list):
                    history = []
            else:
                history = []
        except Exception:
            history = []

        if not history:
            self._history_empty_label = ctk.CTkLabel(
                self._history_list,
                text="No ticket appearances recorded yet.\nStart the monitor and come back here when alerts fire.",
                text_color="gray50", justify="center",
            )
            self._history_empty_label.grid(row=0, column=0, padx=20, pady=40)
            return

        # Show newest first
        for i, entry in enumerate(reversed(history)):
            is_bingo = bool(entry.get("bingo", False))
            badge_color = COLOR_GREEN if is_bingo else COLOR_ORANGE
            badge_text = "🟢 BINGO" if is_bingo else "🟡 Available"
            event_name = entry.get("event_name", "Unknown event")
            event_date = entry.get("event_date", "")
            ts_display = self._format_ts(entry.get("timestamp", ""))

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
        self._refresh_history_tab()

    # ── Monitor Tab ───────────────────────────────────────────────────────────

    def _build_monitor_tab(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._tabs["monitor"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        _section_header(frame, "▶   Monitor", row=0)

        # ── 24/7 uptime notice ────────────────────────────────────────────────
        uptime_frame = ctk.CTkFrame(frame, fg_color="#2a1f00", corner_radius=8)
        uptime_frame.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="ew")
        uptime_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            uptime_frame,
            text="⚠️  Keep this app open (or minimized) and your computer awake 24/7",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#F39C12", anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(10, 2), sticky="w")
        ctk.CTkLabel(
            uptime_frame,
            text=(
                "The monitor only runs while this window is open. If your computer sleeps, loses internet,\n"
                "or this app closes, monitoring stops and you could miss tickets.\n"
                "Tip: plug in your charger and disable sleep in System Settings → Battery (Mac)\n"
                "or Settings → System → Power & Sleep (Windows)."
            ),
            font=ctk.CTkFont(size=11), text_color="#c49a2a", anchor="w", justify="left",
        ).grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")

        # ── Events status panel ───────────────────────────────────────────────
        self._monitor_events_frame = ctk.CTkScrollableFrame(
            frame, fg_color=COLOR_BG_PANEL, corner_radius=8, height=120,
        )
        self._monitor_events_frame.grid(row=2, column=0, padx=20, pady=(0, 8), sticky="ew")
        self._monitor_events_frame.grid_columnconfigure(0, weight=1)
        self._monitor_event_labels: dict[str, ctk.CTkLabel] = {}

        # ── Self-healing info ─────────────────────────────────────────────────
        info_frame = ctk.CTkFrame(frame, fg_color=COLOR_BG_PANEL, corner_radius=8)
        info_frame.grid(row=3, column=0, padx=20, pady=(0, 8), sticky="ew")
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
                "   If heartbeats stop, open the app and check the Live Logs."
            ),
            font=ctk.CTkFont(size=11), text_color="gray60", anchor="w", justify="left",
        ).grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")

        # ── Log viewer ────────────────────────────────────────────────────────
        log_header = ctk.CTkFrame(frame, fg_color="transparent")
        log_header.grid(row=4, column=0, padx=20, pady=(0, 4), sticky="ew")
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
        self._log_text.grid(row=5, column=0, padx=20, pady=(0, 14), sticky="nsew")
        self._log_text.configure(state="disabled")

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _append_log(self, line: str):
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    # ── Monitor process management ────────────────────────────────────────────

    def _toggle_monitor(self):
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
        if running:
            self._status_dot.configure(text_color=COLOR_GREEN)
            self._status_label.configure(text="Monitor running — checking for tickets…")
            self._start_stop_btn.configure(
                text="⏹  Stop Monitor",
                fg_color=COLOR_RED, hover_color="#c0392b",
            )
        else:
            self._status_dot.configure(text_color=COLOR_GRAY)
            self._status_label.configure(text="Monitor stopped")
            self._start_stop_btn.configure(
                text="▶  Start Monitor",
                fg_color=COLOR_GREEN, hover_color="#27ae60",
            )

    def _schedule_status_poll(self):
        self._poll_status()
        self._status_poll_id = self.after(10_000, self._schedule_status_poll)

    def _poll_status(self):
        running = bool(self._monitor_proc and self._monitor_proc.poll() is None)
        self._update_status_bar(running)
        self._refresh_monitor_events_panel()
        # Auto-refresh history whenever the monitor is active.
        if running:
            self._refresh_history_tab()

    def _refresh_monitor_events_panel(self):
        state = load_state()
        events_state = state.get("events", {})
        now = datetime.now(timezone.utc)

        for w in self._monitor_events_frame.winfo_children():
            w.destroy()

        if not self._events:
            # Detect first-run: no events AND no webhook configured yet.
            webhook = self._cfg.get("discord", {}).get("webhook_url", "")
            if not webhook:
                ctk.CTkLabel(
                    self._monitor_events_frame,
                    text=(
                        "Welcome to Ticket Monitor!\n\n"
                        "To get started:\n"
                        "  1.  Events — paste a Ticketmaster event URL\n"
                        "  2.  Notifications — add your Discord webhook URL\n"
                        "  3.  Login — log in to Ticketmaster\n"
                        "  4.  Come back here and hit Start Monitor"
                    ),
                    text_color=COLOR_BLUE, anchor="w", justify="left",
                    font=ctk.CTkFont(size=12),
                ).grid(row=0, column=0, padx=16, pady=16, sticky="w")
            else:
                ctk.CTkLabel(self._monitor_events_frame, text="No events added yet. Go to the Events tab to add a Ticketmaster URL.", text_color="gray50").grid(row=0, column=0, padx=16, pady=16)
            return

        for i, ev in enumerate(self._events):
            eid = ev.get("event_id", "")
            name = ev.get("name", ev.get("url", "Unknown"))
            last_ts = events_state.get(eid, {}).get("last_check")
            if last_ts:
                try:
                    dt = datetime.fromisoformat(last_ts)
                    age_s = int((now - dt).total_seconds())
                    if age_s < 120:
                        status = f"🟢  Last check: {age_s}s ago"
                    elif age_s < 300:
                        status = f"🟡  Last check: {age_s//60}m ago"
                    else:
                        status = f"🔴  Last check: {age_s//60}m ago (stale)"
                except Exception:
                    status = "⚪  Status unknown"
            else:
                status = "⚪  Not yet checked"

            ctk.CTkLabel(self._monitor_events_frame, text=name[:55], anchor="w", font=ctk.CTkFont(weight="bold")).grid(row=i*2, column=0, padx=16, pady=(10 if i==0 else 2, 0), sticky="w")
            ctk.CTkLabel(self._monitor_events_frame, text=status, anchor="w", text_color="gray55", font=ctk.CTkFont(size=11)).grid(row=i*2+1, column=0, padx=16, pady=(0, 2), sticky="w")

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

        prefs = self._cfg.get("preferences", {})
        min_t = max(1, int(prefs.get("min_tickets", 1)))
        self._min_tickets_var.set(min_t)
        self._min_tickets_label.configure(text=str(min_t))

        max_p = float(prefs.get("max_price_per_ticket", 300.0))
        max_p = max(25.0, min(750.0, max_p))
        self._max_price_var.set(max_p)
        self._max_price_label.configure(text=f"${int(max_p)}")

        sections_raw = prefs.get("preferred_sections", [])
        if isinstance(sections_raw, list):
            self._sections_var.set(", ".join(sections_raw))
        else:
            self._sections_var.set(str(sections_raw))

        self._require_section_var.set(bool(prefs.get("require_preferred_only", prefs.get("require_section_match", False))))
        self._alert_any_var.set(bool(prefs.get("alert_on_any_availability", True)))

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

        # Preferences
        sections_text = self._sections_var.get().strip()
        sections_list = [s.strip() for s in sections_text.split(",") if s.strip()]
        cfg.setdefault("preferences", {})
        cfg["preferences"]["min_tickets"] = self._min_tickets_var.get()
        cfg["preferences"]["max_price_per_ticket"] = round(self._max_price_var.get(), 2)
        cfg["preferences"]["preferred_sections"] = sections_list
        cfg["preferences"]["require_preferred_only"] = self._require_section_var.get()
        cfg["preferences"]["alert_on_any_availability"] = self._alert_any_var.get()

        try:
            save_yaml_raw(CONFIG_FILE, cfg)
        except Exception as exc:
            messagebox.showerror("Save Error", f"Could not save config:\n{exc}", parent=self)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self):
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

    app = TicketMonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
