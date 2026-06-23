#!/usr/bin/env python3
"""One-time cleanup: collapse repeat detections in ticket_history.json.

Re-detections of the same seats+price (a listing that lingered while no one
bought it) get merged into a single row with an accurate seen_count and a
first_seen → last_seen span — matching the monitor's going-forward behavior.

Safe to re-run. Backs up the original first. IMPORTANT: stop the monitor (or
restart it onto the latest code) before running, so it isn't writing the file
concurrently and re-adding duplicates.

Usage:  python scripts/dedupe_history.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history_stats import collapse_history  # noqa: E402

HISTORY_FILE = "ticket_history.json"


def main() -> int:
    if not os.path.exists(HISTORY_FILE):
        print(f"No {HISTORY_FILE} found — nothing to do.")
        return 0
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
    if not isinstance(history, list):
        print(f"{HISTORY_FILE} is not a list — aborting (no changes made).")
        return 1

    backup = f"ticket_history.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    shutil.copy2(HISTORY_FILE, backup)

    collapsed = collapse_history(history)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(collapsed, f, indent=2, ensure_ascii=False)

    print(f"Backed up {len(history)} entries  ->  {backup}")
    print(f"Collapsed to {len(collapsed)} unique listing-sets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
