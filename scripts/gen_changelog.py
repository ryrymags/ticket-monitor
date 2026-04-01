#!/usr/bin/env python3
"""
gen_changelog.py — Regenerates CHANGELOG.md and the README 'Recent Changes'
section from git log.

Run manually:  python3 scripts/gen_changelog.py
Auto-runs via: scripts/hooks/post-commit  (install with scripts/install_hooks.sh)
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
README_PATH = REPO_ROOT / "README.md"

CHANGELOG_START = "<!-- CHANGELOG_START -->"
CHANGELOG_END = "<!-- CHANGELOG_END -->"


def git_log() -> list[dict]:
    """Return commits as a list of dicts, newest first."""
    sep = "|||"
    fmt = f"%H{sep}%h{sep}%s{sep}%ad{sep}%D"
    try:
        out = subprocess.check_output(
            ["git", "log", f"--pretty=format:{fmt}", "--date=format:%Y-%m-%d %H:%M", "--no-walk=unsorted"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return []

    # Re-run without --no-walk to get all commits
    try:
        out = subprocess.check_output(
            ["git", "log", f"--pretty=format:{fmt}", "--date=format:%Y-%m-%d %H:%M"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return []

    commits = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(sep)
        if len(parts) < 4:
            continue
        full_hash, short_hash, subject, date = parts[0], parts[1], parts[2], parts[3]
        refs = parts[4] if len(parts) > 4 else ""
        # Extract version tag if present
        tag = None
        for ref in refs.split(","):
            ref = ref.strip()
            m = re.match(r"tag:\s*(v?[\d.]+.*)", ref)
            if m:
                tag = m.group(1)
                break
        commits.append({
            "hash": full_hash,
            "short": short_hash,
            "subject": subject,
            "date": date,
            "tag": tag,
        })
    return commits


def build_changelog_body(commits: list[dict]) -> str:
    """Build the content that goes between the CHANGELOG sentinels."""
    if not commits:
        return "No commits yet.\n"

    lines: list[str] = []
    current_version = "Unreleased"
    current_date = datetime.now().strftime("%Y-%m-%d")
    section: list[str] = []

    def flush_section():
        nonlocal lines, section
        if not section:
            return
        lines.append(f"## [{current_version}] — {current_date}\n")
        lines.append("### Changes\n")
        for entry in section:
            lines.append(entry)
        lines.append("")
        section = []

    for commit in commits:
        if commit["tag"]:
            flush_section()
            current_version = commit["tag"]
            current_date = commit["date"].split(" ")[0]
        section.append(f"- `{commit['short']}`  {commit['date']}  {commit['subject']}\n")

    flush_section()
    return "\n".join(lines)


def update_sentinel_block(path: Path, start_marker: str, end_marker: str, new_body: str) -> bool:
    """Replace content between start/end markers in a file. Returns True if changed."""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    replacement = f"{start_marker}\n{new_body}{end_marker}"
    new_text, count = pattern.subn(replacement, text)
    if count == 0 or new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def build_readme_snippet(commits: list[dict], max_entries: int = 10) -> str:
    """Build a short 'Recent Changes' snippet for the README."""
    if not commits:
        return "No commits yet.\n"
    lines = []
    for c in commits[:max_entries]:
        date = c["date"].split(" ")[0]
        lines.append(f"- `{c['short']}`  {date}  {c['subject']}\n")
    lines.append(f"\nFull history: [CHANGELOG.md](CHANGELOG.md)\n")
    return "".join(lines)


def main() -> int:
    commits = git_log()

    # Update CHANGELOG.md
    if CHANGELOG_PATH.exists():
        body = build_changelog_body(commits)
        update_sentinel_block(CHANGELOG_PATH, CHANGELOG_START, CHANGELOG_END, body)
    else:
        print(f"Warning: {CHANGELOG_PATH} not found — skipping", file=sys.stderr)

    # Update README.md Recent Changes section
    if README_PATH.exists():
        snippet = build_readme_snippet(commits)
        changed = update_sentinel_block(README_PATH, CHANGELOG_START, CHANGELOG_END, snippet)
        if not changed:
            # Markers not present — nothing to update
            pass
    else:
        print(f"Warning: {README_PATH} not found — skipping", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
