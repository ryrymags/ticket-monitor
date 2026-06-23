"""Tests for re-scoring detection history against the current BINGO configs."""

from __future__ import annotations

from src.history_stats import count_bingo_in_history
from src.preferences import TicketPreferences


def _cfg_a() -> TicketPreferences:
    return TicketPreferences(
        min_tickets=2, max_price_per_ticket=200.0, preferred_sections=["LOGE"], name="A"
    )


def _cfg_b() -> TicketPreferences:
    return TicketPreferences(
        min_tickets=4, max_price_per_ticket=300.0, preferred_sections=["FLOOR"], name="B"
    )


def test_total_and_per_config_with_multiple_configs():
    history = [
        # A bingo (LOGE, 2 @ 150)
        {"listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}]},
        # B bingo (FLOOR, 4 @ 250)
        {"listings": [{"section": "FLOOR 1", "row": "1", "price": 250.0, "count": 4}]},
        # matches BOTH configs
        {
            "listings": [
                {"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2},
                {"section": "FLOOR 1", "row": "1", "price": 250.0, "count": 4},
            ]
        },
        # too pricey for A, wrong section for B -> no match
        {"listings": [{"section": "LOGE 5", "row": "1", "price": 500.0, "count": 2}]},
        # old single-field format -> A bingo
        {"section": "LOGE 9", "row": "3", "price": 150.0, "count": 2},
    ]

    res = count_bingo_in_history(history, [_cfg_a(), _cfg_b()])

    assert res["total"] == 4  # entries 0, 1, 2, 4 (entry 3 matches nothing)
    assert res["per_config"]["A"] == 3  # entries 0, 2, 4
    assert res["per_config"]["B"] == 2  # entries 1, 2


def test_every_config_name_present_even_with_zero_matches():
    history = [{"listings": [{"section": "LOGE 5", "row": "1", "price": 150.0, "count": 2}]}]
    res = count_bingo_in_history(history, [_cfg_a(), _cfg_b()])
    assert res["per_config"] == {"A": 1, "B": 0}


def test_no_configs_returns_zero():
    history = [{"listings": [{"section": "LOGE", "row": "1", "price": 50.0, "count": 2}]}]
    res = count_bingo_in_history(history, [])
    assert res == {"total": 0, "per_config": {}}


def test_empty_history():
    assert count_bingo_in_history([], [_cfg_a()]) == {"total": 0, "per_config": {"A": 0}}
