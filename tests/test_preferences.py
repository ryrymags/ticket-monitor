"""Tests for TicketPreferences.matches() — the core alert-classification logic.

This decides whether a set of listing groups fires a green BINGO alert, a 🟡
orange "available but not preferred" alert, or nothing. It's the whole point of
the monitor, so the section/price/quantity gating and the green-vs-orange tiering
are pinned down here.
"""

from __future__ import annotations

from src.notifier import COLOR_GREEN, COLOR_ORANGE
from src.preferences import TicketPreferences, configs_for_event


def _group(count: int, price: float, section: str = "FLOOR", row: str = "A") -> dict:
    return {"count": count, "price": price, "section": section, "row": row}


# ── BINGO (green) ────────────────────────────────────────────────────────────

def test_bingo_on_count_and_price_when_no_section_filter():
    prefs = TicketPreferences(min_tickets=2, max_price_per_ticket=200.0)
    result = prefs.matches([_group(count=2, price=150.0)])
    assert result["matched"] is True
    assert result["bingo"] is True
    assert result["color"] == COLOR_GREEN
    assert result["preview"] == "BINGO"


def test_bingo_requires_section_match_when_filter_set():
    prefs = TicketPreferences(
        min_tickets=2, max_price_per_ticket=300.0, preferred_sections=["LOGE"]
    )
    result = prefs.matches([_group(count=2, price=250.0, section="LOGE 12")])
    assert result["bingo"] is True
    assert result["in_preferred"] is True
    assert result["color"] == COLOR_GREEN


def test_section_match_is_case_insensitive_substring():
    prefs = TicketPreferences(preferred_sections=["floor"])
    result = prefs.matches([_group(count=1, price=50.0, section="FLOOR RIGHT")])
    assert result["bingo"] is True


def test_best_bingo_group_chosen_by_count_then_price():
    prefs = TicketPreferences(min_tickets=2, max_price_per_ticket=500.0)
    groups = [
        _group(count=2, price=100.0, section="A"),
        _group(count=4, price=400.0, section="B"),  # more tickets → preferred
        _group(count=4, price=300.0, section="C"),  # same count, cheaper → best
    ]
    result = prefs.matches(groups)
    assert result["bingo"] is True
    assert result["bingo_group"]["section"] == "C"


# ── Thresholds (no match) ────────────────────────────────────────────────────

def test_below_min_tickets_does_not_match():
    prefs = TicketPreferences(min_tickets=4, max_price_per_ticket=999.0)
    result = prefs.matches([_group(count=2, price=50.0)])
    assert result["matched"] is False
    assert result["bingo"] is False


def test_over_max_price_does_not_match():
    prefs = TicketPreferences(min_tickets=1, max_price_per_ticket=100.0)
    result = prefs.matches([_group(count=2, price=150.0)])
    assert result["matched"] is False


def test_price_at_exact_max_still_matches():
    prefs = TicketPreferences(min_tickets=1, max_price_per_ticket=100.0)
    result = prefs.matches([_group(count=1, price=100.0)])
    assert result["bingo"] is True


def test_empty_listing_groups_is_no_match():
    prefs = TicketPreferences()
    result = prefs.matches([])
    assert result["matched"] is False
    assert result["bingo_group"] is None


# ── Secondary (orange) tiering ───────────────────────────────────────────────

def test_orange_when_count_price_pass_but_section_wrong():
    prefs = TicketPreferences(
        min_tickets=2, max_price_per_ticket=300.0, preferred_sections=["LOGE"]
    )
    result = prefs.matches([_group(count=2, price=200.0, section="BALCONY")])
    assert result["matched"] is True
    assert result["bingo"] is False
    assert result["in_preferred"] is False
    assert result["color"] == COLOR_ORANGE
    assert result["preview"] == "Available"


def test_require_preferred_only_suppresses_orange():
    prefs = TicketPreferences(
        min_tickets=2,
        max_price_per_ticket=300.0,
        preferred_sections=["LOGE"],
        require_preferred_only=True,
    )
    result = prefs.matches([_group(count=2, price=200.0, section="BALCONY")])
    assert result["matched"] is False
    assert result["bingo"] is False


def test_alert_on_any_availability_false_suppresses_orange():
    prefs = TicketPreferences(
        min_tickets=2,
        max_price_per_ticket=300.0,
        preferred_sections=["LOGE"],
        alert_on_any_availability=False,
    )
    result = prefs.matches([_group(count=2, price=200.0, section="BALCONY")])
    assert result["matched"] is False


def test_bingo_wins_over_orange_when_both_present():
    prefs = TicketPreferences(
        min_tickets=2, max_price_per_ticket=300.0, preferred_sections=["LOGE"]
    )
    groups = [
        _group(count=2, price=200.0, section="BALCONY"),  # orange-eligible
        _group(count=2, price=250.0, section="LOGE 5"),   # true bingo
    ]
    result = prefs.matches(groups)
    assert result["bingo"] is True
    assert result["bingo_group"]["section"] == "LOGE 5"


# ── Event scoping ────────────────────────────────────────────────────────────


def test_empty_event_ids_applies_to_every_event():
    prefs = TicketPreferences()
    assert prefs.applies_to_event("event-1") is True
    assert prefs.applies_to_event("") is True


def test_scoped_config_applies_only_to_listed_events():
    prefs = TicketPreferences(event_ids=["event-2"])
    assert prefs.applies_to_event("event-2") is True
    assert prefs.applies_to_event("event-1") is False


def test_applies_to_event_is_case_insensitive():
    prefs = TicketPreferences(event_ids=["exampleevent0002"])
    assert prefs.applies_to_event("EXAMPLEEVENT0002") is True


def test_event_ids_round_trip_through_dict():
    prefs = TicketPreferences(name="Backup night", event_ids=["event-2", "event-3"])
    restored = TicketPreferences.from_dict(prefs.to_dict())
    assert restored.event_ids == ["event-2", "event-3"]


def test_from_dict_accepts_comma_separated_event_ids():
    prefs = TicketPreferences.from_dict({"event_ids": "event-1, event-2"})
    assert prefs.event_ids == ["event-1", "event-2"]


def test_from_dict_defaults_to_unscoped():
    prefs = TicketPreferences.from_dict({"name": "Legacy"})
    assert prefs.event_ids == []
    assert prefs.applies_to_event("anything") is True


def test_configs_for_event_filters_scoped_configs():
    global_cfg = TicketPreferences(name="Global")
    night2_cfg = TicketPreferences(name="Night 2 only", event_ids=["event-2"])
    configs = [global_cfg, night2_cfg]

    assert configs_for_event(configs, "event-1") == [global_cfg]
    assert configs_for_event(configs, "event-2") == [global_cfg, night2_cfg]


def test_configs_for_event_handles_single_object_and_none():
    assert configs_for_event(None, "event-1") is None
    single = TicketPreferences(name="Solo")
    assert configs_for_event(single, "event-1") == [single]
    scoped = TicketPreferences(name="Elsewhere", event_ids=["event-9"])
    assert configs_for_event(scoped, "event-1") == []
