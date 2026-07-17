#!/usr/bin/env python3
"""Fail when public Git content contains high-confidence private data."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PATH_RULES = (
    ("private configuration", re.compile(r"^config\.ya?ml$")),
    ("browser or authentication data", re.compile(r"^secrets/")),
    ("runtime logs", re.compile(r"^logs/")),
    ("runtime state", re.compile(r"^state(?:[ .].*)?\.json(?:\..*)?$")),
    ("ticket history", re.compile(r"^ticket_history(?:[ .].*)?\.json(?:\..*)?$")),
    ("uptime history", re.compile(r"^uptime_log(?:[ _].*)?\.json(?:\..*)?$")),
)

CONTENT_RULES = (
    ("personal Apple email", re.compile(r"\b[\w.+-]+@(?:icloud|me|mac)\.com\b", re.IGNORECASE)),
    ("local-only email identity", re.compile(r"\b[\w.+-]+@[\w.-]+\.(?:lan|local)\b", re.IGNORECASE)),
    ("absolute macOS home path", re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    ("absolute Windows home path", re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\\\s]+\\\\", re.IGNORECASE)),
    (
        "live-looking Discord webhook",
        re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d{15,}/[A-Za-z0-9._-]{20,}"),
    ),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    (
        "production-looking Ticketmaster event ID",
        re.compile(
            r"(?:ticketmaster\.com/[^\s\"']*event/|event_id[\"']?\s*[:=]\s*[\"']?)[0-9A-F]{16}\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def forbidden_path_rule(path: str) -> str | None:
    for rule, pattern in FORBIDDEN_PATH_RULES:
        if pattern.search(path):
            return rule
    return None


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in CONTENT_RULES:
            if pattern.search(line):
                findings.append(Finding(path, line_number, rule))
    return findings


def tracked_paths(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [path.decode() for path in result.stdout.split(b"\0") if path]


def audit(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path in tracked_paths(root):
        path_rule = forbidden_path_rule(relative_path)
        if path_rule:
            findings.append(Finding(relative_path, 0, path_rule))
            continue

        data = (root / relative_path).read_bytes()
        if b"\0" in data:
            continue
        findings.extend(scan_text(relative_path, data.decode("utf-8", errors="replace")))
    return findings


def audit_history(root: Path = ROOT) -> list[Finding]:
    commit_result = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    commits = [commit for commit in commit_result.stdout.splitlines() if commit]
    findings: list[Finding] = []
    blobs: dict[str, str] = {}
    checked_paths: set[str] = set()

    for commit in commits:
        metadata = subprocess.run(
            ["git", "show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        findings.extend(scan_text(f"{commit}:commit-metadata", metadata))

        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", commit],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            details, raw_path = entry.split(b"\t", 1)
            _mode, object_type, object_id = details.decode().split()
            if object_type != "blob":
                continue
            path = raw_path.decode()
            blobs.setdefault(object_id, path)
            if path not in checked_paths:
                checked_paths.add(path)
                path_rule = forbidden_path_rule(path)
                if path_rule:
                    findings.append(Finding(path, 0, path_rule))

    for object_id, path in blobs.items():
        data = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        if b"\0" in data:
            continue
        findings.extend(scan_text(f"{path}@{object_id[:12]}", data.decode("utf-8", errors="replace")))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="scan every commit and historical blob")
    args = parser.parse_args()

    findings = audit_history() if args.history else audit()
    if not findings:
        scope = "history" if args.history else "current tree"
        print(f"Public-safety audit passed ({scope}).")
        return 0

    print("Public-safety audit failed; matched values are intentionally redacted:")
    for finding in findings:
        location = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {location}: {finding.rule}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
