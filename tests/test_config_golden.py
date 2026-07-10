"""Golden characterization tests for load_config.

These lock down load_config's EXACT output — every default, type coercion, and
field — so the Phase 5 loader refactor cannot silently change behavior. The
snapshots in tests/fixtures/config_golden_*.json are generated once from the
current code and committed; any later change to a default or coercion fails
these loudly.

Regenerating a snapshot is a DELIBERATE act (only when a config change is
intended): delete the JSON and re-run. On the refactor commits the snapshots
already exist, so the tests compare rather than regenerate — that is the safety
property.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


def _normalized_snapshot(config) -> dict:
    """MonitorConfig as a JSON-normalized dict (nested EventConfig /
    TicketPreferences dataclasses recurse via asdict)."""
    raw = dataclasses.asdict(config)
    return json.loads(json.dumps(raw, sort_keys=True, default=str))


@pytest.mark.parametrize("name", ["minimal", "maximal"])
def test_config_matches_golden(name):
    cfg_path = FIXTURES / f"config_{name}.yaml"
    golden_path = FIXTURES / f"config_golden_{name}.json"

    actual = _normalized_snapshot(load_config(str(cfg_path)))

    if not golden_path.exists():
        golden_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"Generated golden snapshot {golden_path.name}; re-run to compare")

    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"load_config output for {name} diverged from the committed golden snapshot. "
        "If this change is intentional, delete the JSON and regenerate."
    )
