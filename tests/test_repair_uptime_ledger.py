"""Tests for the one-time uptime-ledger repair script."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.repair_uptime_ledger import has_activity_within, repair_segments


def _iso(dt):
    return dt.isoformat()


BASE = datetime(2026, 7, 9, 5, 0, 0, tzinfo=timezone.utc)


def _seg(state, start_s, end_s, reason=None):
    return {
        "state": state,
        "start": _iso(BASE + timedelta(seconds=start_s)),
        "end": _iso(BASE + timedelta(seconds=end_s)),
        "reason": reason,
    }


class TestRepairSegments:
    def test_down_with_logged_activity_adopts_next_state(self):
        segments = [
            _seg("healthy", 0, 100),
            _seg("down", 100, 260, "offline"),
            _seg("healthy", 260, 400),
        ]
        stamps = [BASE + timedelta(seconds=180)]  # log line inside the "down" gap

        repaired, count, seconds = repair_segments(segments, stamps)

        assert count == 1
        assert seconds == 160.0
        # Coalesced into one continuous healthy stretch.
        assert [s["state"] for s in repaired] == ["healthy"]

    def test_down_without_activity_is_untouched(self):
        segments = [
            _seg("healthy", 0, 100),
            _seg("down", 100, 400, "offline"),
            _seg("healthy", 400, 500),
        ]
        stamps = [BASE + timedelta(seconds=50), BASE + timedelta(seconds=450)]

        repaired, count, _seconds = repair_segments(segments, stamps)

        assert count == 0
        assert [s["state"] for s in repaired] == ["healthy", "down", "healthy"]

    def test_down_before_impaired_adopts_impaired_reason(self):
        segments = [
            _seg("down", 0, 200, "offline"),
            _seg("impaired", 200, 300, "blocked"),
        ]
        stamps = [BASE + timedelta(seconds=100)]

        repaired, count, _ = repair_segments(segments, stamps)

        assert count == 1
        assert repaired[0]["state"] == "impaired"
        assert repaired[0]["reason"] == "blocked"

    def test_trailing_down_falls_back_to_impaired(self):
        segments = [_seg("down", 0, 200, "offline")]
        stamps = [BASE + timedelta(seconds=100)]

        repaired, count, _ = repair_segments(segments, stamps)

        assert count == 1
        assert repaired[0]["state"] == "impaired"


class TestActivityWindow:
    def test_boundary_timestamps_do_not_count(self):
        start, end = BASE, BASE + timedelta(seconds=120)
        assert has_activity_within([start], start, end) is False
        assert has_activity_within([end], start, end) is False
        assert has_activity_within([BASE + timedelta(seconds=60)], start, end) is True
