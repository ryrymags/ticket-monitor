#!/usr/bin/env python3
"""One-time repair: reclassify historical ``down`` uptime segments that overlap
logged monitor activity.

Before the 2026-07-09 down-gap fix (heartbeats now budget planned sleep PLUS
measured cycle work), any cycle slower than the ledger's 90s base margin — slow
page loads, session-health navigations, BINGO alert sends — was back-filled as a
spurious ``down offline`` segment even though monitor.log shows the process
checking events and sending alerts the whole time. The fix stops NEW phantom
segments; this script corrects the ones already recorded.

Method: a monitor.log line timestamped strictly inside a ``down`` segment proves
the process was alive then, so the segment is reclassified to the state of the
segment that FOLLOWS it (the heartbeat that closed the gap describes the very
cycle that was running during it), falling back to ``impaired``. Segments with
no log coverage (rotated away, or genuinely down) are left untouched.

Run only while the monitor is STOPPED — the live monitor holds the ledger in
memory and its next flush would overwrite the repair. Originals are backed up
beside each file as ``<name>.pre-fix-backup-<timestamp>.json``.

Usage:
    python scripts/repair_uptime_ledger.py [--dry-run]
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.state import write_json_atomic
from src.uptime import _coalesce_segments

LOG_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
# A log line must fall strictly inside the segment (with this slack) so the
# boundary heartbeats that delimit a genuine outage don't count as activity.
EDGE_SLACK_SECONDS = 1.0

DEFAULT_LOGS = ("logs/monitor.log", "logs/monitor.log.1", "logs/monitor.log.2", "logs/monitor.log.3")


def load_log_timestamps(log_paths) -> list[datetime]:
    """UTC timestamps of every monitor.log line (log lines are local time)."""
    local_tz = datetime.now().astimezone().tzinfo
    stamps: list[datetime] = []
    for raw_path in log_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                m = LOG_LINE_TS.match(line)
                if m:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)
                    stamps.append(dt.astimezone(timezone.utc))
    stamps.sort()
    return stamps


def _iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def has_activity_within(stamps: list[datetime], start: datetime, end: datetime) -> bool:
    lo = start + timedelta(seconds=EDGE_SLACK_SECONDS)
    hi = end - timedelta(seconds=EDGE_SLACK_SECONDS)
    if hi <= lo:
        return False
    i = bisect.bisect_left(stamps, lo)
    return i < len(stamps) and stamps[i] < hi


def repair_segments(segments: list[dict], stamps: list[datetime]) -> tuple[list[dict], int, float]:
    """Reclassify down segments proven alive; returns (segments, count, seconds)."""
    repaired: list[dict] = []
    count = 0
    seconds = 0.0
    for idx, seg in enumerate(segments):
        if isinstance(seg, dict) and seg.get("state") == "down":
            start, end = _iso(seg.get("start")), _iso(seg.get("end"))
            if start is not None and end is not None and has_activity_within(stamps, start, end):
                nxt = segments[idx + 1] if idx + 1 < len(segments) else None
                if isinstance(nxt, dict) and nxt.get("state") in ("healthy", "impaired"):
                    new_state, new_reason = nxt.get("state"), nxt.get("reason")
                else:
                    new_state, new_reason = "impaired", "blocked"
                seg = {**seg, "state": new_state, "reason": new_reason}
                count += 1
                seconds += (end - start).total_seconds()
        repaired.append(seg)
    return _coalesce_segments(repaired), count, seconds


def repair_file(path: str, stamps: list[datetime], dry_run: bool) -> tuple[int, float]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segments, list):
        print(f"{path}: no segments — skipped")
        return 0, 0.0

    repaired, count, seconds = repair_segments(segments, stamps)
    if count == 0:
        print(f"{path}: nothing to repair")
        return 0, 0.0
    if dry_run:
        print(f"{path}: WOULD reclassify {count} down segment(s) ({seconds / 60:.1f} min) [dry run]")
        return count, seconds

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    p = Path(path)
    backup = p.with_name(f"{p.stem}.pre-fix-backup-{stamp}{p.suffix}")
    shutil.copy2(path, backup)
    write_json_atomic(path, {"segments": repaired}, indent=None)
    print(f"{path}: reclassified {count} down segment(s) ({seconds / 60:.1f} min); backup at {backup.name}")
    return count, seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair spurious 'down' uptime segments using monitor.log evidence")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--logs", nargs="*", default=list(DEFAULT_LOGS), help="Monitor log files to scan")
    parser.add_argument("ledgers", nargs="*", help="Ledger files (default: uptime_log*.json, excluding backups)")
    args = parser.parse_args()

    from monitor import monitor_lock_is_held

    if not args.dry_run and monitor_lock_is_held():
        print("The monitor is running — stop it first (scripts/monitorctl.sh stop); its next")
        print("ledger flush would overwrite the repair. Or use --dry-run to preview.")
        return 1

    ledgers = args.ledgers or [
        f for f in sorted(glob.glob("uptime_log*.json")) if "backup" not in f
    ]
    if not ledgers:
        print("No uptime ledgers found.")
        return 0

    stamps = load_log_timestamps(args.logs)
    if not stamps:
        print("No monitor.log timestamps found — nothing can be proven; aborting.")
        return 1
    print(f"Loaded {len(stamps)} log timestamps ({stamps[0]:%Y-%m-%d %H:%M} → {stamps[-1]:%Y-%m-%d %H:%M} UTC)")

    total_count = 0
    total_seconds = 0.0
    for ledger in ledgers:
        count, seconds = repair_file(ledger, stamps, args.dry_run)
        total_count += count
        total_seconds += seconds
    print(f"Total: {total_count} segment(s), {total_seconds / 3600:.2f} h reclassified{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
