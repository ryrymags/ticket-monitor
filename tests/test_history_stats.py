"""Tests for re-scoring detection history against the current BINGO configs."""

from __future__ import annotations

from src.history_stats import count_bingo_in_history
from src.preferences import TicketPreferences


def _cfg_a() -> TicketPreferences:
    return TicketPreferences(
        min_tickets=2, max_price_per_ticket=200.0, preferred_sections=["LOGE"], name="A"
    )


def _cfg_b() -> TicketPreferences:
    return TicketPreferences(
        min_tickets=4, max_price_per_ticket=300.0, preferred_sections=["FLOOR"], name="B"
    )


def test_total_and_per_config_with_multiple_configs():
    history = [
        # A bingo (LOGE, 2 @ 150)
        {"listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}]},
        # B bingo (FLOOR, 4 @ 250)
        {"listings": [{"section": "FLOOR 1", "row": "1", "price": 250.0, "count": 4}]},
        # matches BOTH configs
        {
            "listings": [
                {"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2},
                {"section": "FLOOR 1", "row": "1", "price": 250.0, "count": 4},
            ]
        },
        # too pricey for A, wrong section for B -> no match
        {"listings": [{"section": "LOGE 5", "row": "1", "price": 500.0, "count": 2}]},
        # old single-field format -> A bingo
        {"section": "LOGE 9", "row": "3", "price": 150.0, "count": 2},
    ]

    res = count_bingo_in_history(history, [_cfg_a(), _cfg_b()])

    assert res["total"] == 4  # entries 0, 1, 2, 4 (entry 3 matches nothing)
    assert res["per_config"]["A"] == 3  # entries 0, 2, 4
    assert res["per_config"]["B"] == 2  # entries 1, 2


def test_every_config_name_present_even_with_zero_matches():
    history = [{"listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}]}]
    res = count_bingo_in_history(history, [_cfg_a(), _cfg_b()])
    assert res["per_config"] == {"A": 1, "B": 0}


def test_no_configs_returns_zero():
    history = [{"listings": [{"section": "LOGE", "row": "1", "price": 50.0, "count": 2}]}]
    res = count_bingo_in_history(history, [])
    assert res == {"total": 0, "per_config": {}}


def test_empty_history():
    assert count_bingo_in_history([], [_cfg_a()]) == {"total": 0, "per_config": {"A": 0}}


def test_repeat_listings_with_fingerprint_counted_once():
    cfg = _cfg_a()
    entry = {
        "event_id": "E1",
        "fingerprint": "fp-loge",
        "listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}],
    }
    history = [dict(entry), dict(entry), dict(entry)]  # 3 repeat detections
    res = count_bingo_in_history(history, [cfg])
    assert res["total"] == 1
    assert res["per_config"]["A"] == 1


def test_repeat_listings_without_fingerprint_counted_once():
    cfg = _cfg_a()
    entry = {
        "event_id": "E1",
        "listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}],
    }
    history = [dict(entry), dict(entry)]  # derived key from listings dedups them
    res = count_bingo_in_history(history, [cfg])
    assert res["total"] == 1


def test_collapse_history_merges_repeats():
    from src.history_stats import collapse_history

    history = [
        {"event_id": "E1", "fingerprint": "fpA", "seen_count": 1,
         "first_seen": "2026-06-23T10:00:00+00:00", "last_seen": "2026-06-23T10:00:00+00:00",
         "bingo": True, "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}]},
        {"event_id": "E1", "fingerprint": "fpB", "seen_count": 1,
         "first_seen": "2026-06-23T10:01:00+00:00", "last_seen": "2026-06-23T10:01:00+00:00",
         "bingo": False, "listings": [{"section": "FLOOR", "row": "A", "price": 175.0, "count": 2}]},
        {"event_id": "E1", "fingerprint": "fpA", "seen_count": 3,
         "first_seen": "2026-06-23T10:02:00+00:00", "last_seen": "2026-06-23T10:05:00+00:00",
         "bingo": True, "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}]},
    ]
    rows = collapse_history(history)
    assert len(rows) == 2  # fpA collapsed, fpB separate
    a = next(r for r in rows if r["fingerprint"] == "fpA")
    assert a["seen_count"] == 4  # 1 + 3
    assert a["first_seen"] == "2026-06-23T10:00:00+00:00"
    assert a["last_seen"] == "2026-06-23T10:05:00+00:00"


def test_collapse_history_distinct_events_kept_separate():
    from src.history_stats import collapse_history

    history = [
        {"event_id": "E1", "fingerprint": "fp", "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}]},
        {"event_id": "E2", "fingerprint": "fp", "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}]},
    ]
    assert len(collapse_history(history)) == 2  # same fingerprint, different events


def test_count_recent_appearances_window_and_bingo():
    from datetime import datetime, timedelta, timezone
    from src.history_stats import count_recent_appearances

    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

    def ent(event_id, fp, hours_ago, bingo):
        ts = (now - timedelta(hours=hours_ago)).isoformat()
        return {
            "event_id": event_id, "fingerprint": fp, "bingo": bingo,
            "first_seen": ts, "last_seen": ts, "seen_count": 1,
            "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}],
        }

    history = [
        ent("E1", "a", 2, True),     # in 48h + 7d, bingo
        ent("E2", "b", 30, False),   # in 48h + 7d, not bingo
        ent("E3", "c", 100, True),   # in 7d only, bingo
        ent("E4", "d", 24 * 10, True),  # older than 7d
    ]

    d2 = count_recent_appearances(history, now, hours=48)
    assert d2 == {"total": 2, "bingo": 1}

    d7 = count_recent_appearances(history, now, hours=24 * 7)
    assert d7 == {"total": 3, "bingo": 2}


def test_count_recent_appearances_collapses_repeats():
    from datetime import datetime, timedelta, timezone
    from src.history_stats import count_recent_appearances

    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat()
    entry = {
        "event_id": "E1", "fingerprint": "same", "bingo": True,
        "first_seen": ts, "last_seen": ts, "seen_count": 1,
        "listings": [{"section": "LOGE", "row": "1", "price": 150.0, "count": 2}],
    }
    history = [dict(entry), dict(entry), dict(entry)]  # 3 repeat detections → 1
    assert count_recent_appearances(history, now, hours=48) == {"total": 1, "bingo": 1}
