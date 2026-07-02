"""Durable uptime/downtime ledger.

Records a timeline of *when monitoring was actually working* so the GUI can show
"20h healthy / 2h impaired / 2h down in the last 24h" and a history of past
outages ("Jun 28 10pm → Jun 29 4am · DOWN 6h").

Three states are tracked:
  - ``healthy``  — the monitor loop ran a cycle and checks succeeded.
  - ``impaired`` — the loop ran but checks were blocked/stale/logged-out/errored.
  - ``down``     — no heartbeat at all: laptop asleep/off, app closed, wifi killed
                   the loop, or a crash. Down segments are *inferred* from gaps in
                   the heartbeat stream, so they need no clean-shutdown hook.

Only the monitor subprocess writes ``uptime_log.json`` (via :class:`UptimeLedger`).
The GUI reads it lock-free through the pure functions at the bottom of this module.
Writes are atomic (temp file + ``os.replace``) so a reader never sees a half file.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from .state import write_json_atomic

logger = logging.getLogger(__name__)

STATES = ("healthy", "impaired", "down")

# Base gap (seconds): the smallest silence between heartbeats that counts as "the
# monitor was down in between" when we have no better expectation. Kept tight so
# even short outages (a couple minutes of sleep/wifi-loss/pause) are caught. The
# monitor deliberately sleeps far longer than this while backing off from a block
# (adaptive cadence up to ~5 min), so the *dynamic* threshold below adds the
# intended sleep on top — a planned 5-min backoff is NOT downtime, but a 5-min
# unplanned silence is.
DEFAULT_MIN_DOWN_GAP_SECONDS = 90

# How often to persist while sitting in the same state (state changes flush
# immediately regardless).
DEFAULT_FLUSH_INTERVAL_SECONDS = 30

# Segments older than this are pruned on save.
RETENTION_DAYS = 30

# Read-side fallback: when a caller can't tell us whether the monitor process is
# actually running, treat the tail after the last heartbeat as "down" once it
# exceeds this slack (a live monitor heartbeats every few seconds).
FRESHNESS_SLACK_SECONDS = DEFAULT_MIN_DOWN_GAP_SECONDS


def _dt_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class UptimeLedger:
    """Append-only ledger of monitoring state segments, persisted to JSON.

    Constructed and driven by the monitor subprocess. Call :meth:`heartbeat`
    once per cycle; call :meth:`flush` on clean shutdown.
    """

    def __init__(
        self,
        path: str = "uptime_log.json",
        min_down_gap_seconds: int = DEFAULT_MIN_DOWN_GAP_SECONDS,
        flush_interval_seconds: int = DEFAULT_FLUSH_INTERVAL_SECONDS,
    ):
        self.path = path
        self.min_down_gap_seconds = min_down_gap_seconds
        self.flush_interval_seconds = flush_interval_seconds
        self._segments: list[dict] = self._load()
        self._last_flush_at: datetime | None = None

    # ---- persistence ----

    def _load(self) -> list[dict]:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                segments = data.get("segments") if isinstance(data, dict) else None
                if isinstance(segments, list):
                    return [s for s in segments if isinstance(s, dict)]
        except Exception as exc:  # pragma: no cover - corrupt file is non-fatal
            logger.warning("Could not load uptime ledger %s: %s", self.path, exc)
        return []

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=RETENTION_DAYS)
        kept: list[dict] = []
        for seg in self._segments:
            end = _iso_to_dt(seg.get("end"))
            if end is None or end >= cutoff:
                kept.append(seg)
        self._segments = kept

    def flush(self, now: datetime | None = None) -> None:
        """Atomically write the ledger to disk (temp file + os.replace)."""
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        payload = {"segments": self._segments}
        try:
            write_json_atomic(self.path, payload, indent=None)
        except Exception as exc:  # pragma: no cover - disk errors are non-fatal
            logger.warning("Could not write uptime ledger %s: %s", self.path, exc)
            return
        self._last_flush_at = now

    # ---- recording ----

    def heartbeat(
        self,
        now: datetime | None = None,
        state: str = "healthy",
        reason: str | None = None,
        expected_gap_seconds: float | None = None,
    ) -> None:
        """Record that the monitor is currently in ``state`` at time ``now``.

        ``expected_gap_seconds`` is how long the scheduler *intended* to wait since
        its previous heartbeat (its adaptive sleep). A silence counts as downtime
        only when it exceeds that intended wait plus a base margin — so a planned
        5-minute backoff during blocking is impairment (one continuous segment),
        while an *unplanned* 5-minute silence (sleep, wifi loss, app closed) is a
        ``down`` segment spanning last-heartbeat → resume. When it's ``None`` (a
        fresh process's first heartbeat, so the prior gap is genuinely off-time)
        the base margin alone applies.

        Handles three cases: a down gap (back-fills a ``down`` segment), the same
        state (extends it), or a state change (opens a new segment). Persists
        immediately on a state change or gap, else at most once per flush interval.
        """
        now = now or datetime.now(timezone.utc)
        if state not in STATES:
            state = "impaired"

        changed = False  # structural change worth an immediate flush
        last = self._segments[-1] if self._segments else None

        if last is None:
            self._segments.append(self._new_segment(state, now, now, reason))
            changed = True
        else:
            last_end = _iso_to_dt(last.get("end")) or now
            gap = (now - last_end).total_seconds()
            if expected_gap_seconds is None:
                threshold = self.min_down_gap_seconds
            else:
                threshold = max(
                    self.min_down_gap_seconds,
                    expected_gap_seconds + self.min_down_gap_seconds,
                )
            if gap > threshold:
                # Silence longer than the monitor planned for → it was down.
                self._segments.append(
                    self._new_segment("down", last_end, now, "offline")
                )
                self._segments.append(self._new_segment(state, now, now, reason))
                changed = True
            elif last.get("state") == state:
                last["end"] = _dt_to_iso(now)
                last["reason"] = reason
            else:
                start = last_end if last_end <= now else now
                self._segments.append(self._new_segment(state, start, now, reason))
                changed = True

        due = (
            self._last_flush_at is None
            or (now - self._last_flush_at).total_seconds() >= self.flush_interval_seconds
        )
        if changed or due:
            self.flush(now)

    def mark_online(self, now: datetime | None = None) -> None:
        """Close a downtime gap at process startup.

        Call this the moment the monitor starts, before the first check. If the last
        heartbeat is older than the base gap, the intervening time (the monitor was
        closed / the machine was off / no internet) is recorded as ``down`` ending
        exactly here — at startup. The first cycle's heartbeat then opens the new
        healthy/impaired segment from this same instant, so downtime spans
        last-check → startup, never last-check → first *successful* pull.
        """
        now = now or datetime.now(timezone.utc)
        if not self._segments:
            return  # first run ever — the first heartbeat opens the timeline
        last = self._segments[-1]
        last_end = _iso_to_dt(last.get("end")) or now
        if (now - last_end).total_seconds() > self.min_down_gap_seconds:
            self._segments.append(self._new_segment("down", last_end, now, "offline"))
            self.flush(now)

    @staticmethod
    def _new_segment(
        state: str, start: datetime, end: datetime, reason: str | None
    ) -> dict:
        return {
            "state": state,
            "start": _dt_to_iso(start),
            "end": _dt_to_iso(end),
            "reason": reason,
        }

    @property
    def segments(self) -> list[dict]:
        return self._segments


# ---- pure read-side helpers (safe for the GUI; no locking) ----


def load_uptime_segments(path: str = "uptime_log.json") -> list[dict]:
    """Load segments from disk for read-only display. Returns [] on any problem."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            segments = data.get("segments") if isinstance(data, dict) else None
            if isinstance(segments, list):
                return [s for s in segments if isinstance(s, dict)]
    except Exception:
        pass
    return []


def _clip_seconds(seg_start: datetime, seg_end: datetime, win_start: datetime, win_end: datetime) -> float:
    start = max(seg_start, win_start)
    end = min(seg_end, win_end)
    return max(0.0, (end - start).total_seconds())


def _tail_is_down(last_end: datetime, now: datetime, monitor_running: bool | None) -> bool:
    """Decide whether the time since the last heartbeat counts as downtime.

    If we know the monitor process isn't running, it's down immediately; if it is
    running, the tail belongs to the current (live) state; otherwise fall back to a
    freshness slack.
    """
    if monitor_running is False:
        return True
    if monitor_running is True:
        return False
    return (now - last_end).total_seconds() > FRESHNESS_SLACK_SECONDS


def summarize_uptime(
    segments: list[dict],
    hours: int,
    now: datetime | None = None,
    monitor_running: bool | None = None,
) -> dict:
    """Sum seconds spent in each state over the last ``hours``.

    Segments are clipped to the ``[now-hours, now]`` window. The interval between
    the final segment's ``end`` and ``now`` is counted as ``down`` when the monitor
    isn't running (or, if unknown, once it's staler than a small slack) — so a
    stopped monitor correctly accrues downtime even though nothing is writing the
    ledger anymore.

    Returns ``{"healthy_s","impaired_s","down_s","total_s","healthy_pct"}``.
    """
    now = now or datetime.now(timezone.utc)
    win_start = now - timedelta(hours=max(1, hours))
    totals = {"healthy": 0.0, "impaired": 0.0, "down": 0.0}

    last_end: datetime | None = None
    for seg in segments or []:
        start = _iso_to_dt(seg.get("start"))
        end = _iso_to_dt(seg.get("end"))
        state = seg.get("state")
        if start is None or end is None or state not in totals:
            continue
        if end < start:
            continue
        totals[state] += _clip_seconds(start, end, win_start, now)
        if last_end is None or end > last_end:
            last_end = end

    # Trailing downtime: from the last known heartbeat to now.
    if last_end is not None and _tail_is_down(last_end, now, monitor_running):
        totals["down"] += _clip_seconds(last_end, now, win_start, now)

    total = totals["healthy"] + totals["impaired"] + totals["down"]
    healthy_pct = round(100.0 * totals["healthy"] / total, 1) if total else 0.0
    return {
        "healthy_s": int(totals["healthy"]),
        "impaired_s": int(totals["impaired"]),
        "down_s": int(totals["down"]),
        "total_s": int(total),
        "healthy_pct": healthy_pct,
    }


def timeline(
    segments: list[dict],
    hours: int,
    min_seconds: int = 60,
    now: datetime | None = None,
    monitor_running: bool | None = None,
) -> list[dict]:
    """All state segments (healthy, impaired, down) in the window, newest first.

    Each entry: ``{"state","reason","start","end","duration_s","ongoing"}``.

    The currently-active segment is flagged ``ongoing`` and its duration is left
    open — callers should render "Ongoing" rather than a creeping number, and only
    treat the duration as final once the state switches. If the last heartbeat is
    stale (monitor stopped), a synthetic ongoing ``down`` segment is appended from
    that heartbeat to ``now``. Closed segments shorter than ``min_seconds`` are
    dropped to keep the list readable; the ongoing segment is always kept.
    """
    now = now or datetime.now(timezone.utc)
    win_start = now - timedelta(hours=max(1, hours))

    effective: list[dict] = []
    for seg in segments or []:
        start = _iso_to_dt(seg.get("start"))
        end = _iso_to_dt(seg.get("end"))
        state = seg.get("state")
        if start is None or end is None or state not in STATES or end < start:
            continue
        effective.append(
            {
                "state": state,
                "reason": seg.get("reason"),
                "start": start,
                "end": end,
                "ongoing": False,
            }
        )

    # Mark the live tail: if the monitor is still running the last segment is the
    # ongoing state; otherwise (stopped / stale) the tail is ongoing downtime.
    if effective:
        last = effective[-1]
        if _tail_is_down(last["end"], now, monitor_running):
            effective.append(
                {
                    "state": "down",
                    "reason": "offline",
                    "start": last["end"],
                    "end": now,
                    "ongoing": True,
                }
            )
        else:
            last["ongoing"] = True
            last["end"] = now

    rows: list[dict] = []
    for e in effective:
        if e["end"] < win_start:
            continue
        duration = (e["end"] - e["start"]).total_seconds()
        if not e["ongoing"] and duration < min_seconds:
            continue
        rows.append(
            {
                "state": e["state"],
                "reason": e["reason"],
                "start": _dt_to_iso(e["start"]),
                "end": _dt_to_iso(e["end"]),
                "duration_s": int(duration),
                "ongoing": e["ongoing"],
            }
        )
    rows.sort(key=lambda s: s.get("start") or "", reverse=True)
    return rows


def recent_outages(
    segments: list[dict], hours: int, min_seconds: int = 60, now: datetime | None = None
) -> list[dict]:
    """Non-healthy rows (down + impaired) from :func:`timeline`, newest first."""
    return [
        r
        for r in timeline(segments, hours, min_seconds=min_seconds, now=now)
        if r["state"] in ("down", "impaired")
    ]


def current_status(
    segments: list[dict], now: datetime | None = None, monitor_running: bool | None = None
) -> dict:
    """Describe the monitor's current state from the tail of the ledger.

    Returns ``{"state","reason","since"}``. If the monitor isn't running (or, when
    unknown, the last heartbeat is staler than the freshness slack) the state is
    reported as ``down`` since that last heartbeat.
    """
    now = now or datetime.now(timezone.utc)
    if not segments:
        return {"state": "down", "reason": "offline", "since": None}
    last = segments[-1]
    last_end = _iso_to_dt(last.get("end"))
    if last_end is not None and _tail_is_down(last_end, now, monitor_running):
        return {"state": "down", "reason": "offline", "since": last.get("end")}
    return {
        "state": last.get("state", "down"),
        "reason": last.get("reason"),
        "since": last.get("start"),
    }
