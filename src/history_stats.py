"""Re-score the detection history against the CURRENT BINGO configs.

The history file stores each appearance's raw listing groups (section/row/price/
count), so we can recompute whether each past listing would be a BINGO under the
user's configs as they are *now* — independent of whatever config was active when
the entry was first written.
"""

from __future__ import annotations

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
                "section": section,
                "row": entry.get("row", "?"),
                "price": entry.get("price", 0),
                "count": entry.get("count", 0),
            }
        ]
    return []


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
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
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
