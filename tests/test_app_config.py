"""Tests for app.py's pure helper functions.

The GUI itself isn't unit-tested (it needs a live Tk display), but the
display-free helpers — config deep-merge, URL slug/date parsing, and the
per-event uptime filename — are pure and are where a silent config-mapping bug
would originate, so they're pinned down here. Importing ``app`` pulls
customtkinter; if that ever fails headless these tests are skipped rather than
failing the suite.
"""

from __future__ import annotations

import pytest

app = pytest.importorskip("app")


# ── _deep_merge ──────────────────────────────────────────────────────────────

def test_deep_merge_overrides_scalars_and_adds_keys():
    base = {"a": 1, "b": 2}
    app._deep_merge(base, {"b": 3, "c": 4})
    assert base == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_recurses_into_nested_dicts():
    base = {"browser": {"headful": True, "timeout": 30}}
    app._deep_merge(base, {"browser": {"timeout": 60}})
    assert base == {"browser": {"headful": True, "timeout": 60}}


def test_deep_merge_replaces_dict_with_scalar_when_types_differ():
    base = {"x": {"nested": 1}}
    app._deep_merge(base, {"x": "flat"})
    assert base == {"x": "flat"}


# ── _guess_event_name ────────────────────────────────────────────────────────

def test_guess_event_name_from_slug():
    url = "https://www.ticketmaster.com/some-cool-concert/event/12345"
    assert app._guess_event_name(url) == "Some Cool Concert"


def test_guess_event_name_strips_trailing_mdy_date():
    url = "https://www.ticketmaster.com/artist-name-06-15-2026/event/999"
    assert app._guess_event_name(url) == "Artist Name"


def test_guess_event_name_fallback_when_no_slug():
    assert app._guess_event_name("https://example.com") == "New Event"


# ── _guess_event_date ────────────────────────────────────────────────────────

def test_guess_event_date_mdy_normalized_to_iso():
    url = "https://www.ticketmaster.com/artist-06-15-2026/event/1"
    assert app._guess_event_date(url) == "2026-06-15"


def test_guess_event_date_iso_passthrough():
    url = "https://www.ticketmaster.com/artist-2026-06-15/event/1"
    assert app._guess_event_date(url) == "2026-06-15"


def test_guess_event_date_empty_when_absent():
    assert app._guess_event_date("https://www.ticketmaster.com/artist/event/1") == ""


# ── uptime_event_file ────────────────────────────────────────────────────────

def test_uptime_event_file_matches_gitignore_pattern():
    # Filename must start with "uptime_log_" so the .gitignore "uptime_log_*.json"
    # rule keeps these per-event ledgers out of version control.
    name = app.uptime_event_file("EXAMPLEEVENT0002")
    assert name == "uptime_log_EXAMPLEEVENT0002.json"
    assert name.startswith("uptime_log_") and name.endswith(".json")


# ── monitor_running_state ────────────────────────────────────────────────────

class _Proc:
    def __init__(self, poll_value):
        self._poll_value = poll_value

    def poll(self):
        return self._poll_value


def test_monitor_running_state_prefers_launchd_running():
    assert app.monitor_running_state(True, None) is True
    assert app.monitor_running_state(True, _Proc(0)) is True


def test_monitor_running_state_prefers_launchd_stopped():
    assert app.monitor_running_state(False, _Proc(None)) is False


def test_monitor_running_state_falls_back_to_gui_process():
    assert app.monitor_running_state(None, _Proc(None)) is True
    assert app.monitor_running_state(None, _Proc(0)) is False
    assert app.monitor_running_state(None, None) is False
