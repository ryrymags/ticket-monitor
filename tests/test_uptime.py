"""Tests for the uptime/downtime ledger and its read-side summaries."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from src.uptime import (
    UptimeLedger,
    current_status,
    load_uptime_segments,
    recent_outages,
    summarize_uptime,
    timeline,
)

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---- UptimeLedger.heartbeat ----


def test_same_state_extends_single_segment(tmp_path):
    ledger = UptimeLedger(path=str(tmp_path / "u.json"))
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=30), "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=60), "healthy")
    assert len(ledger.segments) == 1
    seg = ledger.segments[0]
    assert seg["state"] == "healthy"
    assert seg["start"] == _iso(T0)
    assert seg["end"] == _iso(T0 + timedelta(seconds=60))


def test_state_change_opens_new_segment_without_gap(tmp_path):
    ledger = UptimeLedger(path=str(tmp_path / "u.json"))
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=30), "impaired", reason="blocked")
    assert len(ledger.segments) == 2
    assert ledger.segments[0]["state"] == "healthy"
    assert ledger.segments[1]["state"] == "impaired"
    assert ledger.segments[1]["reason"] == "blocked"
    # New segment starts where the previous one ended (no gap → no down segment).
    assert ledger.segments[1]["start"] == ledger.segments[0]["end"]


def test_gap_backfills_down_segment(tmp_path):
    ledger = UptimeLedger(path=str(tmp_path / "u.json"), min_down_gap_seconds=90)
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=30), "healthy")
    # Monitor "goes away" for 2 hours (laptop closed), then comes back.
    resume = T0 + timedelta(hours=2)
    ledger.heartbeat(resume, "healthy")
    states = [s["state"] for s in ledger.segments]
    assert states == ["healthy", "down", "healthy"]
    down = ledger.segments[1]
    assert down["reason"] == "offline"
    # Downtime spans last heartbeat → resume, exactly as the user described.
    assert down["start"] == _iso(T0 + timedelta(seconds=30))
    assert down["end"] == _iso(resume)


def test_small_jitter_gap_is_not_downtime(tmp_path):
    ledger = UptimeLedger(path=str(tmp_path / "u.json"), min_down_gap_seconds=90)
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=60), "healthy")  # under the 90s base threshold
    assert [s["state"] for s in ledger.segments] == ["healthy"]


def test_planned_backoff_is_not_downtime(tmp_path):
    # While blocked, the monitor deliberately sleeps up to ~5 min between cycles.
    # A gap that fits the intended sleep is impairment, not a down segment.
    ledger = UptimeLedger(path=str(tmp_path / "u.json"), min_down_gap_seconds=90)
    ledger.heartbeat(T0, "impaired", reason="blocked")
    ledger.heartbeat(
        T0 + timedelta(seconds=250), "impaired", reason="blocked", expected_gap_seconds=300
    )
    # One continuous impaired stretch — no spurious "down" in the middle.
    assert [s["state"] for s in ledger.segments] == ["impaired"]


def test_unplanned_silence_beyond_intended_sleep_is_down(tmp_path):
    # Same 250s gap, but the monitor only intended to wait 3s → it vanished.
    ledger = UptimeLedger(path=str(tmp_path / "u.json"), min_down_gap_seconds=90)
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(
        T0 + timedelta(seconds=250), "healthy", expected_gap_seconds=3
    )
    assert [s["state"] for s in ledger.segments] == ["healthy", "down", "healthy"]


def test_heartbeat_persists_and_reloads(tmp_path):
    path = str(tmp_path / "u.json")
    ledger = UptimeLedger(path=path)
    ledger.heartbeat(T0, "healthy")
    ledger.heartbeat(T0 + timedelta(seconds=30), "impaired", reason="stale")
    ledger.flush(T0 + timedelta(seconds=30))
    assert os.path.exists(path)
    reloaded = load_uptime_segments(path)
    assert [s["state"] for s in reloaded] == ["healthy", "impaired"]
    # A brand-new ledger over the same file continues the timeline.
    ledger2 = UptimeLedger(path=path)
    assert len(ledger2.segments) == 2


def test_mark_online_closes_downtime_at_startup(tmp_path):
    # Monitor was healthy, then closed for 2h; on the next startup mark_online()
    # records the down gap ending exactly at startup — before any check runs.
    path = str(tmp_path / "u.json")
    seed = {
        "segments": [
            {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(minutes=10)), "reason": None},
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)

    ledger = UptimeLedger(path=path)
    startup = T0 + timedelta(hours=2)
    ledger.mark_online(startup)
    down = ledger.segments[-1]
    assert down["state"] == "down"
    assert down["start"] == _iso(T0 + timedelta(minutes=10))  # last check
    assert down["end"] == _iso(startup)  # monitor startup, NOT first pull

    # The first check a few seconds later opens the new segment AT startup, so the
    # boundary is last-check → startup (downtime) then startup → now (monitoring).
    first_check = startup + timedelta(seconds=8)
    ledger.heartbeat(first_check, "healthy")
    healthy = ledger.segments[-1]
    assert healthy["state"] == "healthy"
    assert healthy["start"] == _iso(startup)


def test_mark_online_ignores_brief_restart(tmp_path):
    path = str(tmp_path / "u.json")
    seed = {
        "segments": [
            {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(minutes=10)), "reason": None},
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    ledger = UptimeLedger(path=path, min_down_gap_seconds=90)
    ledger.mark_online(T0 + timedelta(minutes=10, seconds=30))  # 30s later
    assert [s["state"] for s in ledger.segments] == ["healthy"]  # no down for a blip


def test_retention_prunes_old_segments(tmp_path):
    path = str(tmp_path / "u.json")
    old_start = T0 - timedelta(days=40)
    seed = {
        "segments": [
            {"state": "healthy", "start": _iso(old_start), "end": _iso(old_start + timedelta(hours=1)), "reason": None},
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    ledger = UptimeLedger(path=path)
    ledger.heartbeat(T0, "healthy")  # triggers a flush → prune
    starts = [s["start"] for s in load_uptime_segments(path)]
    assert _iso(old_start) not in starts


# ---- summarize_uptime ----


def test_summarize_sums_windowed_seconds():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=3)), "reason": None},
        {"state": "impaired", "start": _iso(T0 + timedelta(hours=3)), "end": _iso(T0 + timedelta(hours=4)), "reason": "blocked"},
        {"state": "healthy", "start": _iso(T0 + timedelta(hours=4)), "end": _iso(T0 + timedelta(hours=5)), "reason": None},
    ]
    now = T0 + timedelta(hours=5)
    summ = summarize_uptime(segs, hours=24, now=now)
    assert summ["healthy_s"] == 4 * 3600
    assert summ["impaired_s"] == 1 * 3600
    assert summ["down_s"] == 0
    assert summ["healthy_pct"] == 80.0


def test_summarize_counts_trailing_gap_as_down():
    # Last heartbeat 6h ago and nothing since → that tail is downtime.
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
    ]
    now = T0 + timedelta(hours=7)
    summ = summarize_uptime(segs, hours=24, now=now)
    assert summ["healthy_s"] == 3600
    assert summ["down_s"] == 6 * 3600


def test_summarize_clips_to_window():
    # A 10h-old healthy segment, but the window is only 2h.
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=10)), "reason": None},
    ]
    now = T0 + timedelta(hours=10)
    summ = summarize_uptime(segs, hours=2, now=now)
    assert summ["healthy_s"] == 2 * 3600


# ---- recent_outages / current_status ----


def test_recent_outages_filters_and_orders():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
        {"state": "down", "start": _iso(T0 + timedelta(hours=1)), "end": _iso(T0 + timedelta(hours=3)), "reason": "offline"},
        {"state": "impaired", "start": _iso(T0 + timedelta(hours=3)), "end": _iso(T0 + timedelta(hours=3, seconds=10)), "reason": "blocked"},  # too short
        {"state": "impaired", "start": _iso(T0 + timedelta(hours=4)), "end": _iso(T0 + timedelta(hours=5)), "reason": "stale"},
    ]
    now = T0 + timedelta(hours=5)
    outages = recent_outages(segs, hours=24, min_seconds=60, now=now)
    # Only the down (2h) and the 1h impaired qualify; newest first.
    assert [o["state"] for o in outages] == ["impaired", "down"]
    assert outages[1]["duration_s"] == 2 * 3600


def test_current_status_reports_stale_tail_as_down():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
    ]
    # Long after the last heartbeat.
    st = current_status(segs, now=T0 + timedelta(hours=2))
    assert st["state"] == "down"


def test_current_status_fresh_returns_last_state():
    segs = [
        {"state": "impaired", "start": _iso(T0), "end": _iso(T0 + timedelta(seconds=5)), "reason": "blocked"},
    ]
    st = current_status(segs, now=T0 + timedelta(seconds=10))
    assert st["state"] == "impaired"
    assert st["reason"] == "blocked"


def test_current_status_down_immediately_when_not_running():
    # Even a fresh last heartbeat reads as down when the process isn't running
    # (e.g. the user just pressed Stop).
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(seconds=5)), "reason": None},
    ]
    st = current_status(segs, now=T0 + timedelta(seconds=10), monitor_running=False)
    assert st["state"] == "down"


def test_summarize_counts_stopped_tail_as_down_immediately():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
    ]
    now = T0 + timedelta(hours=1, seconds=30)  # only 30s since last heartbeat
    summ = summarize_uptime(segs, hours=24, now=now, monitor_running=False)
    assert summ["down_s"] == 30  # counted despite being under the freshness slack


# ---- timeline ----


def test_timeline_includes_healthy_and_flags_ongoing():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
        {"state": "impaired", "start": _iso(T0 + timedelta(hours=1)), "end": _iso(T0 + timedelta(hours=2)), "reason": "blocked"},
        {"state": "healthy", "start": _iso(T0 + timedelta(hours=2)), "end": _iso(T0 + timedelta(hours=3)), "reason": None},
    ]
    now = T0 + timedelta(hours=3)  # last heartbeat is fresh → last segment is ongoing
    rows = timeline(segs, hours=24, now=now)
    # Newest first, healthy rows present (not just outages).
    assert [r["state"] for r in rows] == ["healthy", "impaired", "healthy"]
    # The active (newest) segment is ongoing; the rest are finalized.
    assert rows[0]["ongoing"] is True
    assert rows[1]["ongoing"] is False
    assert rows[2]["ongoing"] is False


def test_timeline_appends_synthetic_ongoing_down_when_stale():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
    ]
    now = T0 + timedelta(hours=4)  # monitor stopped 3h ago
    rows = timeline(segs, hours=24, now=now)
    assert rows[0]["state"] == "down"
    assert rows[0]["ongoing"] is True
    assert rows[0]["reason"] == "offline"
    # The prior healthy stretch is finalized with its real 1h duration.
    healthy = [r for r in rows if r["state"] == "healthy"][0]
    assert healthy["ongoing"] is False
    assert healthy["duration_s"] == 3600


def test_timeline_drops_short_closed_segments_but_keeps_ongoing():
    segs = [
        {"state": "healthy", "start": _iso(T0), "end": _iso(T0 + timedelta(hours=1)), "reason": None},
        {"state": "impaired", "start": _iso(T0 + timedelta(hours=1)), "end": _iso(T0 + timedelta(hours=1, seconds=10)), "reason": "blocked"},  # 10s blip
        {"state": "healthy", "start": _iso(T0 + timedelta(hours=1, seconds=10)), "end": _iso(T0 + timedelta(hours=2)), "reason": None},
    ]
    now = T0 + timedelta(hours=2)
    rows = timeline(segs, hours=24, min_seconds=60, now=now)
    # The 10s impaired blip is dropped; two healthy stretches remain.
    assert [r["state"] for r in rows] == ["healthy", "healthy"]
