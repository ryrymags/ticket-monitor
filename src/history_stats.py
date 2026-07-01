"""Re-score the detection history against the CURRENT BINGO configs.

The history file stores each appearance's raw listing groups (section/row/price/
count), so we can recompute whether each past listing would be a BINGO under the
user's configs as they are *now* — independent of whatever config was active when
the entry was first written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _entry_listings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return an entry's listing groups, falling back to the old single-field format."""
    listings = entry.get("listings") or []
    if listings:
        return listings
    # Backward-compat: very old entries stored a single listing inline.
    section = entry.get("section")
    if section and section != "?":
        return [
            {
                "section": entry.get("section", "?"),
                "row": entry.get("row", "?"),
                "price": entry.get("price", 0),
                "count": entry.get("count", 0),
            }
        ]
    return []


def _entry_key(entry: dict[str, Any]) -> tuple:
    """A dedup key identifying a unique listing-set for an event.

    The same listing re-detected over minutes (no one bought it yet) can be written
    as several rows because the full-set fingerprint flips as surrounding inventory
    churns. Collapsing on (event_id, listing-set) ensures each distinct set counts
    once. Prefers the stored fingerprint; derives one from the listings otherwise.
    """
    event_id = entry.get("event_id", "")
    fingerprint = entry.get("fingerprint")
    if fingerprint:
        return (event_id, fingerprint)
    sig = tuple(
        sorted(
            (
                str(g.get("section", "")),
                str(g.get("row", "")),
                float(g.get("price", 0) or 0),
                int(g.get("count", 0) or 0),
            )
            for g in _entry_listings(entry)
        )
    )
    return (event_id, sig)


def collapse_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeat detections of the same (event, listing-set) into one row.

    Re-detections of identical seats+price (no one bought it yet) are merged:
    seen_count is summed, first_seen is the earliest and last_seen the latest.
    The earliest entry's descriptive fields are kept; bingo is OR-ed. Returns rows
    sorted by first_seen. Mirrors the going-forward write-path dedup so the stored
    history and the BINGO counter agree.
    """
    merged: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        key = _entry_key(entry)
        first = entry.get("first_seen") or entry.get("timestamp") or ""
        last = entry.get("last_seen") or entry.get("timestamp") or first
        seen = int(entry.get("seen_count", 1) or 1)
        if key not in merged:
            row = dict(entry)
            row["seen_count"] = seen
            row["first_seen"] = first
            row["last_seen"] = last
            merged[key] = row
            order.append(key)
        else:
            row = merged[key]
            row["seen_count"] = int(row.get("seen_count", 1) or 1) + seen
            if first and (not row.get("first_seen") or first < row["first_seen"]):
                row["first_seen"] = first
            if last and (not row.get("last_seen") or last > row["last_seen"]):
                row["last_seen"] = last
            row["bingo"] = bool(row.get("bingo")) or bool(entry.get("bingo"))
    rows = [merged[k] for k in order]
    rows.sort(key=lambda r: r.get("first_seen") or "")
    return rows


def count_bingo_in_history(history: list[dict[str, Any]], configs: list) -> dict[str, Any]:
    """Count history entries that are a BINGO under the current configs.

    Returns ``{"total": int, "per_config": {name: int}}`` where:
      - ``total`` counts each entry once if it is a BINGO under ANY config (so
        ``total`` <= sum of per-config counts when an entry matches multiple).
      - ``per_config`` counts matches per config name; every current config name is
        present (0 if it never matched). Configs sharing a name are merged.
    """
    per_config: dict[str, int] = {}
    for cfg in configs:
        name = (str(getattr(cfg, "name", "") or "").strip()) or "BINGO"
        per_config.setdefault(name, 0)

    total = 0
    seen_keys: set = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        # Count each distinct listing-set once, ignoring repeat detections.
        key = _entry_key(entry)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        listings = _entry_listings(entry)
        if not listings:
            continue
        matched_any = False
        for cfg in configs:
            name = (str(getattr(cfg, "name", "") or "").strip()) or "BINGO"
            try:
                result = cfg.matches(listings)
            except Exception:
                continue
            if result.get("bingo"):
                per_config[name] = per_config.get(name, 0) + 1
                matched_any = True
        if matched_any:
            total += 1

    return {"total": total, "per_config": per_config}


def count_recent_appearances(
    history: list[dict[str, Any]], now: datetime | None = None, hours: int = 48
) -> dict[str, int]:
    """Count distinct ticket appearances first seen within the last ``hours``.

    Collapses repeat detections of the same (event, listing-set) so a listing that
    lingered across many checks counts once — mirroring the History tab. An entry is
    in-window if its ``first_seen`` (falling back to ``timestamp``) is at or after
    ``now - hours``. Returns ``{"total": int, "bingo": int}`` where ``bingo`` counts
    the in-window rows flagged as a BINGO.

    Used for the peace-of-mind "tickets seen in 48h / week" stat: a 0 there when no
    alerts have fired is a hint to go check the monitor.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(1, hours))
    total = 0
    bingo = 0
    for entry in collapse_history(history):
        raw = entry.get("first_seen") or entry.get("timestamp")
        seen = _iso_to_dt(raw)
        if seen is None or seen < cutoff:
            continue
        total += 1
        if entry.get("bingo"):
            bingo += 1
    return {"total": total, "bingo": bingo}


def _iso_to_dt(value: Any) -> datetime | None:
    """Parse an ISO8601 string to an aware datetime; None on failure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
