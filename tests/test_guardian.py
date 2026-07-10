"""Tests for external watchdog guardian logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts import guardian
from src.config import EventConfig, MonitorConfig
from src.preferences import TicketPreferences
from src.state import MonitorState


# Captured before the autouse fixture patches the module attribute, so the flock
# test below can exercise the real implementation.
_real_gui_is_running = guardian.gui_is_running


@pytest.fixture(autouse=True)
def _gui_present(monkeypatch):
    """Default every test to 'the GUI app is open' — monitoring only runs while
    the GUI is up, and most tests exercise behavior in that normal state. The
    GUI-coincidence tests override this per-test."""
    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: True)


def _make_config(**overrides) -> MonitorConfig:
    defaults = dict(
        discord_webhook_url="https://discord.test/webhook",
        discord_username="Test",
        discord_ping_user_id="",
        ntfy_enabled=False,
        ntfy_topics=[],
        ntfy_server="https://ntfy.sh",
        ntfy_priority="high",
        events=[EventConfig(event_id="event-1", name="Night 1", date="2030-01-01", url="http://event")],
        browser_storage_state_path="secrets/test_state.json",
        browser_session_mode="storage_state",
        browser_user_data_dir="secrets/test_profile",
        browser_channel="chrome",
        browser_cdp_endpoint_url="http://127.0.0.1:9222",
        browser_cdp_connect_timeout_seconds=10,
        browser_reuse_event_tabs=True,
        browser_headless=True,
        browser_navigation_timeout_seconds=20,
        browser_challenge_threshold=5,
        browser_challenge_retry_seconds=60,
        browser_challenge_cooldown_base_seconds=60,
        browser_challenge_cooldown_max_seconds=1800,
        browser_challenge_cooldown_escalate_after=6,
        browser_challenge_cooldown_tiers_seconds=[300, 900, 1800],
        browser_challenge_cooldown_tier_every=3,
        browser_startup_grace_seconds=180,
        browser_stealth_enabled=False,
        browser_locale="en-US",
        browser_timezone_id="America/New_York",
        browser_host_enabled=False,
        browser_host_chrome_executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        browser_host_user_data_dir="secrets/tm_chrome_profile",
        browser_host_remote_debugging_port=9222,
        alerts_ticket_cooldown_seconds=180,
        alerts_operational_heartbeat_hours=6,
        alerts_event_check_stale_seconds=180,
        alerts_operational_state_cooldown_seconds=1800,
        alerts_non_bingo_enabled=False,
        alerts_manual_action_after_seconds=900,
        alerts_operational_to_discord=False,
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
        auth_session_recheck_base_seconds=120,
        auth_session_recheck_max_seconds=900,
        auth_session_logout_confirmations_required=2,
        preferences=TicketPreferences(),
        bingo_configs=[TicketPreferences()],
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


# ── Last-resort reboot tier ──────────────────────────────────────────────────


def _window_summary(total_s=3600, healthy_s=0):
    """summarize_uptime-shaped dict for the reboot window."""
    bad = max(0, total_s - healthy_s)
    pct = round(100.0 * healthy_s / total_s, 1) if total_s else 0.0
    return {
        "healthy_s": healthy_s,
        "impaired_s": bad,
        "down_s": 0,
        "total_s": total_s,
        "healthy_pct": pct,
    }


def _reboot_kwargs(now, **overrides):
    """All-guards-pass baseline for evaluate_reboot; tests flip one guard at a time."""
    kwargs = dict(
        config=_make_config(watchdog_reboot_enabled=True),
        now=now,
        window_summary=_window_summary(),
        probe_scope="ip_device",
        system_uptime_seconds=7200.0,
        last_reboot_at=None,
        reboots_last_day=0,
        fix_attempts_last_hour=2,
    )
    kwargs.update(overrides)
    return kwargs


def test_evaluate_reboot_all_guards_pass():
    now = datetime.now(timezone.utc)
    should, reason = guardian.evaluate_reboot(**_reboot_kwargs(now))
    assert should is True
    assert "ip_device" in reason


def test_evaluate_reboot_disabled_by_default():
    now = datetime.now(timezone.utc)
    should, reason = guardian.evaluate_reboot(
        **_reboot_kwargs(now, config=_make_config())
    )
    assert should is False
    assert reason == "reboot_disabled"


def test_evaluate_reboot_requires_unhealthy_window():
    now = datetime.now(timezone.utc)
    # Not enough recorded time in the window yet.
    should, reason = guardian.evaluate_reboot(
        **_reboot_kwargs(now, window_summary=_window_summary(total_s=600))
    )
    assert should is False
    assert reason == "window_only_600s_recorded"

    # Plenty recorded, but meaningfully healthy → no reboot.
    should, reason = guardian.evaluate_reboot(
        **_reboot_kwargs(now, window_summary=_window_summary(total_s=3600, healthy_s=1200))
    )
    assert should is False
    assert reason == "healthy_33pct_in_window"

    # Healthy blips under the 10% fraction do NOT reset eligibility — this is the
    # thrash case the old contiguous-stretch rule could never fire on.
    should, _ = guardian.evaluate_reboot(
        **_reboot_kwargs(now, window_summary=_window_summary(total_s=3600, healthy_s=300))
    )
    assert should is True


def test_evaluate_reboot_skips_targeted_remedy_scopes():
    now = datetime.now(timezone.utc)
    for scope in ("profile", "account", "none"):
        should, reason = guardian.evaluate_reboot(**_reboot_kwargs(now, probe_scope=scope))
        assert should is False, scope
        assert "targeted_remedy" in reason
    # ip_device, unknown, or no probe at all → reboot may proceed.
    for scope in ("ip_device", "unknown", None):
        should, _ = guardian.evaluate_reboot(**_reboot_kwargs(now, probe_scope=scope))
        assert should is True, scope


def test_evaluate_reboot_requires_lighter_remedies_first():
    now = datetime.now(timezone.utc)
    should, reason = guardian.evaluate_reboot(**_reboot_kwargs(now, fix_attempts_last_hour=0))
    assert should is False
    assert reason == "lighter_remedies_not_tried_yet"


def test_evaluate_reboot_loop_guards():
    now = datetime.now(timezone.utc)
    # Fresh boot must get its chance first (default min uptime 1800s).
    should, _ = guardian.evaluate_reboot(**_reboot_kwargs(now, system_uptime_seconds=300.0))
    assert should is False
    # Spacing: last self-heal reboot too recent (default spacing 7200s).
    should, _ = guardian.evaluate_reboot(
        **_reboot_kwargs(now, last_reboot_at=now - timedelta(seconds=3600))
    )
    assert should is False
    # Daily cap (default 3/day).
    should, reason = guardian.evaluate_reboot(**_reboot_kwargs(now, reboots_last_day=3))
    assert should is False
    assert reason == "daily_reboot_cap_reached"
    # Spacing satisfied → allowed again.
    should, _ = guardian.evaluate_reboot(
        **_reboot_kwargs(now, last_reboot_at=now - timedelta(seconds=7300))
    )
    assert should is True


def test_reboot_history_survives_in_state(tmp_path):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    state.record_selfheal_reboot(now - timedelta(hours=3))
    state.record_selfheal_reboot(now - timedelta(hours=1))

    # Re-open from disk — the history must persist across the reboot itself.
    reopened = MonitorState(state_file=str(tmp_path / "state.json"))
    assert reopened.get_selfheal_reboots_recent(86400, now=now) == 2
    assert reopened.get_selfheal_reboots_recent(7200, now=now) == 1
    last = reopened.get_last_selfheal_reboot_at()
    assert last is not None and abs((last - (now - timedelta(hours=1))).total_seconds()) < 2


def test_impaired_since_walks_contiguous_non_healthy_segments():
    now = datetime.now(timezone.utc)

    def seg(state, start_min, end_min):
        return {
            "state": state,
            "start": (now - timedelta(minutes=start_min)).isoformat(),
            "end": (now - timedelta(minutes=end_min)).isoformat(),
        }

    segments = [
        seg("healthy", 180, 90),
        seg("impaired", 90, 40),
        seg("down", 40, 10),
        seg("impaired", 10, 0),
    ]
    since = guardian.impaired_since(now, segments=segments)
    assert since is not None
    assert abs((since - (now - timedelta(minutes=90))).total_seconds()) < 2

    # Currently healthy → no impairment window.
    assert guardian.impaired_since(now, segments=[seg("healthy", 60, 0)]) is None


def test_maybe_selfheal_reboot_notifies_when_reboot_unavailable(tmp_path, monkeypatch):
    cfg = _make_config(watchdog_reboot_enabled=True)
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    state.record_guardian_fix_attempt(datetime.now(timezone.utc))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(guardian, "load_uptime_segments", lambda _path: [])
    monkeypatch.setattr(
        guardian, "summarize_uptime", lambda *_a, **_k: _window_summary(total_s=3600)
    )
    monkeypatch.setattr(
        guardian, "_run_variation_probe_for_reboot", lambda _cfg, _state: None
    )
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(
        guardian, "reboot_available", lambda: (False, "FileVault is on")
    )

    rebooted = guardian.maybe_selfheal_reboot(cfg, state, notifier, now)
    assert rebooted is False
    assert len(notifier.critical_calls) == 1
    assert "FileVault is on" in notifier.critical_calls[0][0]
    # No reboot recorded — nothing actually happened.
    assert state.get_last_selfheal_reboot_at() is None


def test_maybe_selfheal_reboot_records_before_rebooting(tmp_path, monkeypatch):
    cfg = _make_config(watchdog_reboot_enabled=True)
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    state.record_guardian_fix_attempt(datetime.now(timezone.utc))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(guardian, "load_uptime_segments", lambda _path: [])
    monkeypatch.setattr(
        guardian, "summarize_uptime", lambda *_a, **_k: _window_summary(total_s=3600)
    )
    monkeypatch.setattr(
        guardian, "_run_variation_probe_for_reboot", lambda _cfg, _state: None
    )
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(guardian, "reboot_available", lambda: (True, "ok"))

    commands = []

    def _fake_run(cmd, **kwargs):
        commands.append(cmd)

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(guardian.subprocess, "run", _fake_run)

    rebooted = guardian.maybe_selfheal_reboot(cfg, state, notifier, now)
    assert rebooted is True
    # Fixed command via a scoped sudoers NOPASSWD rule — no stored credentials.
    assert commands and commands[0] == guardian.REBOOT_COMMAND
    assert commands[0] == ["sudo", "-n", "/sbin/shutdown", "-r", "now"]
    # Reboot history + Discord notice recorded before the restart call.
    assert state.get_last_selfheal_reboot_at() is not None
    assert notifier.auto_fix_calls and notifier.auto_fix_calls[0][0] == "selfheal_reboot"


def test_reboot_available_requires_filevault_off(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class _Proc:
            stdout = "FileVault is On.\n"
            stderr = ""

        return _Proc()

    monkeypatch.setattr(guardian.subprocess, "run", _fake_run)
    available, detail = guardian.reboot_available()
    assert available is False
    assert "FileVault" in detail


def test_reboot_available_requires_autologin(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class _Proc:
            stdout = "FileVault is Off.\n" if cmd[:2] == ["fdesetup", "status"] else ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(guardian.subprocess, "run", _fake_run)
    available, detail = guardian.reboot_available()
    assert available is False
    assert "automatic login" in detail.lower()


def test_reboot_available_when_filevault_off_and_autologin_set(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class _Proc:
            stdout = "FileVault is Off.\n" if cmd[:2] == ["fdesetup", "status"] else "testuser\n"
            stderr = ""

        return _Proc()

    monkeypatch.setattr(guardian.subprocess, "run", _fake_run)
    available, _detail = guardian.reboot_available()
    assert available is True


def test_is_stale_uses_cycle_start_as_progress(tmp_path):
    """A recently STARTED cycle counts as alive even if none completed lately —
    slow Chrome launches and challenge-cooldown sleeps must not read as hung."""
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    state.set_last_cycle_completed_at(now - timedelta(seconds=900))
    state.set_last_cycle_started_at(now - timedelta(seconds=30))
    stale, age = guardian.is_stale(state=state, stale_after_seconds=600, now=now)
    assert stale is False
    assert age < 600

    # And a genuinely hung monitor (both marks old) is still stale.
    state.set_last_cycle_started_at(now - timedelta(seconds=900))
    stale, _age = guardian.is_stale(state=state, stale_after_seconds=600, now=now)
    assert stale is True


def test_confirm_staleness_requires_two_strikes(tmp_path):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    kwargs = dict(
        state=state,
        stale=True,
        stale_age_seconds=700.0,
        service_running=True,
        force_fix=False,
        now=now,
    )
    assert guardian._confirm_staleness(**kwargs) is False  # strike 1: wait a pass
    assert state.get_guardian_stale_strikes() == 1
    assert guardian._confirm_staleness(**kwargs) is True  # strike 2: remediate

    # A healthy pass resets the strike counter.
    assert guardian._confirm_staleness(**{**kwargs, "stale": False}) is False
    assert state.get_guardian_stale_strikes() == 0


def test_confirm_staleness_skipped_during_challenge_cooldown(tmp_path):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    state.set_challenge_cooldown_until(now + timedelta(seconds=300))
    demoted = guardian._confirm_staleness(
        state=state,
        stale=True,
        stale_age_seconds=700.0,
        service_running=True,
        force_fix=False,
        now=now,
    )
    assert demoted is False
    assert state.get_guardian_stale_strikes() == 0  # cooldown quiet is not a strike


def test_confirm_staleness_passthrough_for_dead_service(tmp_path):
    """A dead process is remediated immediately — no strikes, no cooldown grace."""
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    now = datetime.now(timezone.utc)
    state.set_challenge_cooldown_until(now + timedelta(seconds=300))
    confirmed = guardian._confirm_staleness(
        state=state,
        stale=True,
        stale_age_seconds=700.0,
        service_running=False,
        force_fix=False,
        now=now,
    )
    assert confirmed is True


def test_probe_report_scope_freshness():
    now = datetime.now(timezone.utc)
    fresh = {"scope": "profile", "at": (now - timedelta(minutes=30)).isoformat()}
    stale = {"scope": "profile", "at": (now - timedelta(hours=3)).isoformat()}
    assert guardian.probe_report_scope(fresh, now) == "profile"
    assert guardian.probe_report_scope(stale, now) is None
    assert guardian.probe_report_scope({}, now) is None
    assert guardian.probe_report_scope({"scope": "profile", "at": "garbage"}, now) is None


def test_maybe_selfheal_reboot_runs_probe_and_defers_to_targeted_remedy(tmp_path, monkeypatch):
    """Reboot warranted but undiagnosed → guardian runs the variation matrix; a
    profile-scoped verdict cancels the reboot (targeted remedy exists)."""
    cfg = _make_config(watchdog_reboot_enabled=True)
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    state.record_guardian_fix_attempt(datetime.now(timezone.utc))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(guardian, "load_uptime_segments", lambda _path: [])
    monkeypatch.setattr(
        guardian, "summarize_uptime", lambda *_a, **_k: _window_summary(total_s=3600)
    )
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(guardian, "reboot_available", lambda: (True, "ok"))

    probe_calls = []

    def _fake_probe(_cfg, _state):
        report = {"scope": "profile", "at": now.isoformat()}
        probe_calls.append(report)
        return report

    monkeypatch.setattr(guardian, "_run_variation_probe_for_reboot", _fake_probe)

    rebooted = guardian.maybe_selfheal_reboot(cfg, state, notifier, now)
    assert rebooted is False
    assert len(probe_calls) == 1  # diagnosis ran exactly once
    assert state.get_last_selfheal_reboot_at() is None  # no reboot slot burned


def test_maybe_selfheal_reboot_proceeds_on_ip_device_verdict(tmp_path, monkeypatch):
    cfg = _make_config(watchdog_reboot_enabled=True)
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    state.record_guardian_fix_attempt(datetime.now(timezone.utc))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)

    monkeypatch.setattr(guardian, "load_uptime_segments", lambda _path: [])
    monkeypatch.setattr(
        guardian, "summarize_uptime", lambda *_a, **_k: _window_summary(total_s=3600)
    )
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(guardian, "reboot_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        guardian,
        "_run_variation_probe_for_reboot",
        lambda _cfg, _state: {"scope": "ip_device", "at": now.isoformat()},
    )

    def _fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(guardian.subprocess, "run", _fake_run)

    rebooted = guardian.maybe_selfheal_reboot(cfg, state, notifier, now)
    assert rebooted is True
    assert state.get_last_selfheal_reboot_at() is not None


# ── GUI coincidence: monitoring only runs while the app is open ──────────────


def test_gui_is_running_reflects_flock(tmp_path):
    import fcntl

    lock_path = tmp_path / "gui.lock"
    # Nobody holds it → not running.
    assert _real_gui_is_running(lock_path) is False
    # Hold it the way the GUI does → running. (flock via a second fd of the same
    # file is denied even within one process, so this is a faithful simulation.)
    holder = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _real_gui_is_running(lock_path) is True
    finally:
        holder.close()
    assert _real_gui_is_running(lock_path) is False


def test_enforce_gui_coincidence_two_strikes_then_stop(tmp_path, monkeypatch):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: False)
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    stops = []
    monkeypatch.setattr(guardian, "stop_monitoring_stack", lambda: stops.append(True))

    # Strike 1: GUI absent, but nothing stopped yet.
    assert guardian.enforce_gui_coincidence(state, notifier, now) is True
    assert stops == []
    assert state.get_gui_absent_strikes() == 1

    # Strike 2: the stack is stopped and the user is told why.
    assert guardian.enforce_gui_coincidence(state, notifier, now) is True
    assert stops == [True]
    assert len(notifier.critical_calls) == 1
    assert "app" in notifier.critical_calls[0][0].lower()
    assert state.get_gui_absent_strikes() == 0


def test_enforce_gui_coincidence_resets_on_gui_return(tmp_path, monkeypatch):
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(guardian, "stop_monitoring_stack", lambda: (_ for _ in ()).throw(AssertionError))

    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: False)
    assert guardian.enforce_gui_coincidence(state, notifier, now) is True
    assert state.get_gui_absent_strikes() == 1

    # GUI comes back → strike forgiven, monitoring untouched.
    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: True)
    assert guardian.enforce_gui_coincidence(state, notifier, now) is False
    assert state.get_gui_absent_strikes() == 0


def test_enforce_gui_coincidence_post_boot_grace(tmp_path, monkeypatch):
    """Right after a (self-heal) reboot launchd is still opening the GUI — the
    guardian must not count strikes or stop anything yet."""
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: False)
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 60.0)
    monkeypatch.setattr(guardian, "stop_monitoring_stack", lambda: (_ for _ in ()).throw(AssertionError))

    assert guardian.enforce_gui_coincidence(state, notifier, now) is True
    assert state.get_gui_absent_strikes() == 0
    assert notifier.critical_calls == []


def test_run_guardian_skips_remediation_when_gui_absent(tmp_path, monkeypatch):
    """With the GUI closed the guardian must not kickstart a dead monitor — the
    stack is on its way to being stopped, not fixed."""
    cfg = _make_config()
    state = MonitorState(state_file=str(tmp_path / "state.json"))
    notifier = _FakeNotifier()

    monkeypatch.setattr(guardian, "MonitorState", lambda: state)
    monkeypatch.setattr(guardian, "DiscordNotifier", lambda **kwargs: notifier)
    monkeypatch.setattr(guardian, "gui_is_running", lambda *a, **k: False)
    monkeypatch.setattr(guardian, "get_system_uptime_seconds", lambda _now=None: 7200.0)
    monkeypatch.setattr(guardian, "stop_monitoring_stack", lambda: None)

    kicks = []
    monkeypatch.setattr(guardian, "kickstart_service", lambda *a, **k: kicks.append(True) or True)
    monkeypatch.setattr(
        guardian, "get_service_status", lambda: guardian.ServiceStatus(running=False, pid=None)
    )

    exit_code = guardian.run_guardian(config=cfg, force_fix=False)
    assert exit_code == 0
    assert kicks == []  # no kickstart: service down + GUI closed = stay down


def test_build_notifier_honors_operational_to_discord():
    captured = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import scripts.guardian as guardian_mod
    original = guardian_mod.DiscordNotifier
    guardian_mod.DiscordNotifier = _Recorder
    try:
        guardian_mod.build_notifier(_make_config(alerts_operational_to_discord=False))
    finally:
        guardian_mod.DiscordNotifier = original

    assert captured["operational_to_discord"] is False
