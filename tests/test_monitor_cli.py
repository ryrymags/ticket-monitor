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
        events=[
            SimpleNamespace(
                name="Night 1",
                date="2026-07-28",
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
