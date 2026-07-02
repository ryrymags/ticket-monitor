"""Tests for the block-scope variation probe (src/variation_probe.py)."""

from __future__ import annotations

import pytest

from src.variation_probe import VariationOutcome, interpret_matrix


def _outcome(variation: str, blocked: bool, ran: bool = True) -> VariationOutcome:
    return VariationOutcome(variation=variation, ran=ran, blocked=blocked, challenge=blocked)


def _matrix(
    signed_in_regular: bool,
    signed_out_regular: bool,
    signed_in_private: bool,
    signed_out_private: bool,
) -> list[VariationOutcome]:
    return [
        _outcome("signed_in_regular", signed_in_regular),
        _outcome("signed_out_regular", signed_out_regular),
        _outcome("signed_in_private", signed_in_private),
        _outcome("signed_out_private", signed_out_private),
    ]


class TestInterpretMatrix:
    def test_nothing_blocked_is_none(self):
        assert interpret_matrix(_matrix(False, False, False, False)) == "none"

    def test_clean_private_blocked_is_ip_device(self):
        # Even a fresh, cookie-less private window is blocked → nothing profile- or
        # account-scoped explains it, regardless of the other variations.
        assert interpret_matrix(_matrix(True, True, True, True)) == "ip_device"
        assert interpret_matrix(_matrix(False, False, False, True)) == "ip_device"
        assert interpret_matrix(_matrix(True, False, True, True)) == "ip_device"

    def test_profile_scope(self):
        # Both runs on the real profile's cookies blocked, private windows fine.
        assert interpret_matrix(_matrix(True, True, False, False)) == "profile"

    def test_account_scope(self):
        # Signed-in contexts blocked wherever they run; signed-out fine everywhere.
        assert interpret_matrix(_matrix(True, False, True, False)) == "account"

    def test_partial_profile_block_maps_to_profile(self):
        # Only one of the profile-based runs blocked — profile is still the
        # actionable common denominator.
        assert interpret_matrix(_matrix(True, False, False, False)) == "profile"
        assert interpret_matrix(_matrix(False, True, False, False)) == "profile"

    def test_private_only_block_is_unknown(self):
        # signed_in_private blocked but the real profile and the clean slate are fine —
        # contradictory; don't act on it.
        assert interpret_matrix(_matrix(False, False, True, False)) == "unknown"

    def test_too_few_results_is_unknown(self):
        assert interpret_matrix([]) == "unknown"
        assert interpret_matrix([_outcome("signed_in_regular", True)]) == "unknown"
        # Failed variations (ran=False) don't count toward the minimum.
        assert (
            interpret_matrix(
                [
                    _outcome("signed_in_regular", True),
                    _outcome("signed_out_regular", False, ran=False),
                    _outcome("signed_in_private", False, ran=False),
                    _outcome("signed_out_private", False, ran=False),
                ]
            )
            == "unknown"
        )

    @pytest.mark.parametrize("si_reg", [True, False])
    @pytest.mark.parametrize("so_reg", [True, False])
    @pytest.mark.parametrize("si_priv", [True, False])
    @pytest.mark.parametrize("so_priv", [True, False])
    def test_every_combination_yields_a_valid_scope(self, si_reg, so_reg, si_priv, so_priv):
        scope = interpret_matrix(_matrix(si_reg, so_reg, si_priv, so_priv))
        assert scope in {"none", "profile", "account", "ip_device", "unknown"}
        # Invariants: a blocked clean private window always means ip_device; a fully
        # clean matrix always means none.
        if so_priv:
            assert scope == "ip_device"
        if not any([si_reg, so_reg, si_priv, so_priv]):
            assert scope == "none"


class TestVariationOutcomeShape:
    def test_report_round_trips_to_dict(self):
        from src.variation_probe import VariationReport

        report = VariationReport(
            at="2026-07-02T00:00:00+00:00",
            event_url="https://www.ticketmaster.com/event/X",
            scope="profile",
            outcomes=_matrix(True, True, False, False),
        )
        data = report.to_dict()
        assert data["scope"] == "profile"
        assert len(data["outcomes"]) == 4
        assert data["outcomes"][0]["variation"] == "signed_in_regular"
        assert data["outcomes"][0]["blocked"] is True
