"""Tests for monitor.py CLI helper functions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import monitor


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        discord_webhook_url="https://discord.test/webhook",
        discord_username="Test",
        discord_ping_user_id="123456789",
        alerts_operational_to_discord=False,
        ntfy_enabled=False,
        ntfy_topics=[],
        ntfy_server="https://ntfy.sh",
        ntfy_priority="high",
        events=[
            SimpleNamespace(
                name="Night 1",
                date="2030-01-01",
                url="https://ticketmaster.com/event/test",
            )
        ],
    )


class _RecordingNotifier:
    def __init__(self, should_fail_on: int | None = None):
        self.calls: list[dict] = []
        self._should_fail_on = should_fail_on
        self._send_count = 0

    def send_ticket_available(self, **kwargs):
        self._send_count += 1
        self.calls.append(kwargs)
        if self._should_fail_on is not None and self._send_count == self._should_fail_on:
            return False
        return True


def test_ticket_alert_matrix_sends_three_examples(monkeypatch):
    notifier = _RecordingNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda _path: _config())
    monkeypatch.setattr(monitor, "DiscordNotifier", lambda *_args, **_kwargs: notifier)

    monitor.run_test_ticket_alert_matrix("config.yaml")

    assert len(notifier.calls) == 3
    assert all(call.get("mention") is True for call in notifier.calls)
    assert notifier.calls[0]["listing_groups"][0]["section"] == "LOGE20"
    assert notifier.calls[0]["listing_groups"][0]["count"] == 4
    assert notifier.calls[1]["listing_groups"][0]["price"] == 120.0
    assert notifier.calls[2]["listing_groups"][0]["count"] == 2


def test_ticket_alert_matrix_exits_nonzero_on_send_failure(monkeypatch):
    notifier = _RecordingNotifier(should_fail_on=2)
    monkeypatch.setattr(monitor, "load_config", lambda _path: _config())
    monkeypatch.setattr(monitor, "DiscordNotifier", lambda *_args, **_kwargs: notifier)

    with pytest.raises(SystemExit) as exc:
        monitor.run_test_ticket_alert_matrix("config.yaml")

    assert exc.value.code == 1
    assert len(notifier.calls) == 3


def test_single_instance_lock_mutual_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MONITOR_LOCK_FILE", str(tmp_path / "locks" / "monitor.lock"))
    first = monitor.acquire_single_instance_lock()
    assert first is not None
    # A second monitor (same host) must be refused while the first holds the lock.
    assert monitor.acquire_single_instance_lock() is None
    first.close()
    # Lock released → next start succeeds.
    third = monitor.acquire_single_instance_lock()
    assert third is not None
    third.close()


def test_monitor_lock_is_held_reflects_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MONITOR_LOCK_FILE", str(tmp_path / "locks" / "monitor.lock"))
    assert monitor.monitor_lock_is_held() is False
    holder = monitor.acquire_single_instance_lock()
    assert holder is not None
    assert monitor.monitor_lock_is_held() is True
    holder.close()
    assert monitor.monitor_lock_is_held() is False


def test_try_lock_file_exclusive_helpers(tmp_path):
    from src.state import try_lock_file_exclusive, unlock_file

    path = tmp_path / "some.lock"
    with open(path, "a+", encoding="utf-8") as first, open(path, "a+", encoding="utf-8") as second:
        assert try_lock_file_exclusive(first) is True
        assert try_lock_file_exclusive(second) is False
        unlock_file(first)
        assert try_lock_file_exclusive(second) is True
        unlock_file(second)


def test_doctor_refuses_live_persistent_profile(monkeypatch, capsys):
    from types import SimpleNamespace as _NS
    cfg = _config()
    cfg.browser_session_mode = "persistent_profile"
    monkeypatch.setattr(monitor, "load_config", lambda _path: cfg)
    monkeypatch.setattr(monitor, "monitor_lock_is_held", lambda: True)

    with pytest.raises(SystemExit) as exc:
        monitor.run_doctor("config.yaml")

    assert exc.value.code == 1
    assert "owns the Chrome profile" in capsys.readouterr().out


class _FakeSectionProbe:
    def __init__(self, sections_by_event: dict[str, list[str]]):
        self._sections = sections_by_event
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True

    def check_event(self, event_id, _url):
        return SimpleNamespace(
            raw_indicators={
                "venue_sections": self._sections.get(event_id, []),
                "listing_groups": [{"section": "FLOOR1", "row": "A", "price": 100.0, "count": 2}],
            }
        )


def test_detect_sections_merges_into_state(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # state.json lands in tmp
    config = _config()
    config.events[0].event_id = "ev1"
    probe = _FakeSectionProbe({"ev1": ["LOGE20", "GA2"]})
    monkeypatch.setattr(monitor, "load_config", lambda _p: config)
    monkeypatch.setattr(monitor, "monitor_lock_is_held", lambda: False)
    monkeypatch.setattr(
        monitor.BrowserProbe, "from_config", staticmethod(lambda _c, **_k: probe)
    )

    monitor.run_detect_sections("config.yaml")

    from src.state import MonitorState

    known = MonitorState(state_file=str(tmp_path / "state.json")).get_known_sections("ev1")
    assert set(known) == {"LOGE20", "GA2", "FLOOR1"}
    assert probe.closed is True
    assert "LOGE20" in capsys.readouterr().out


def test_detect_sections_refuses_while_monitor_running(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "load_config", lambda _p: _config())
    monkeypatch.setattr(monitor, "monitor_lock_is_held", lambda: True)
    with pytest.raises(SystemExit) as exc:
        monitor.run_detect_sections("config.yaml")
    assert exc.value.code == 2
    assert "collected automatically" in capsys.readouterr().out
