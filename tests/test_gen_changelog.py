"""Tests for scripts/gen_changelog.py's changelog/README body generation.

Focus: a git commit cannot embed its own final hash (the post-commit hook's
--amend rewrites it after this script runs), so the newest entry must show a
"(pending)" placeholder instead of a hash that would immediately go stale.
"""

from __future__ import annotations

from scripts.gen_changelog import build_changelog_body, build_readme_snippet

_COMMITS = [
    {"hash": "b" * 40, "short": "bbbbbbb", "subject": "Second commit", "date": "2026-07-08 12:00", "tag": None},
    {"hash": "a" * 40, "short": "aaaaaaa", "subject": "First commit", "date": "2026-07-07 12:00", "tag": None},
]


def test_changelog_body_shows_pending_for_newest_only():
    body = build_changelog_body(_COMMITS, pending_latest=True)
    assert "`(pending)`" in body
    assert "Second commit" in body.split("`(pending)`")[1][:40]
    assert "`aaaaaaa`" in body
    assert "`bbbbbbb`" not in body


def test_changelog_body_shows_real_hash_without_flag():
    body = build_changelog_body(_COMMITS, pending_latest=False)
    assert "`bbbbbbb`" in body
    assert "`aaaaaaa`" in body
    assert "(pending)" not in body


def test_readme_snippet_shows_pending_for_newest_only():
    snippet = build_readme_snippet(_COMMITS, pending_latest=True)
    lines = snippet.strip().splitlines()
    assert lines[0].startswith("- `(pending)`")
    assert "Second commit" in lines[0]
    assert lines[1].startswith("- `aaaaaaa`")


def test_readme_snippet_shows_real_hash_without_flag():
    snippet = build_readme_snippet(_COMMITS, pending_latest=False)
    lines = snippet.strip().splitlines()
    assert lines[0].startswith("- `bbbbbbb`")


def test_empty_commit_list_is_safe_with_pending_flag():
    assert build_changelog_body([], pending_latest=True) == "No commits yet.\n"
    assert build_readme_snippet([], pending_latest=True) == "No commits yet.\n"


def test_pending_placeholder_resolves_on_next_generation():
    """Simulates two consecutive commits: the newest is always pending, but the
    previously-pending entry resolves to its real hash once it's no longer index 0
    — exactly what happens the next time a real commit runs the hook."""
    first_pass = build_changelog_body(_COMMITS[1:], pending_latest=True)
    assert "`(pending)`" in first_pass
    assert "First commit" in first_pass

    second_pass = build_changelog_body(_COMMITS, pending_latest=True)
    assert "`aaaaaaa`" in second_pass  # now resolved, no longer pending
    assert "First commit" in second_pass.split("`aaaaaaa`")[1][:40]
    assert "`(pending)`" in second_pass  # the new newest entry is pending instead
