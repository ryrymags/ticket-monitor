"""Tests for Detector signature and cooldown logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.detector import Detector
from src.models import ProbeResult, ProbeSignalType
from src.state import MonitorState


def _result(
    *,
    available: bool = True,
    signal_type: ProbeSignalType = ProbeSignalType.DOM_AND_NETWORK,
    price_summary: str | None = "$99.00 - $129.00",
    section_summary: str | None = "Section 101",
    dom_signals: list[str] | None = None,
    network_signals: list[str] | None = None,
    availability_count: int = 2,
) -> ProbeResult:
    return ProbeResult(
        event_id="event-1",
        event_url="http://event",
        available=available,
        blocked=False,
        challenge_detected=False,
        signal_type=signal_type,
        signal_confidence=0.95,
        price_summary=price_summary,
        section_summary=section_summary,
        raw_indicators={
            "dom_signals": dom_signals or ["buy_ui"],
            "network_signals": network_signals or ["quantity"],
            "availability_count": availability_count,
        },
    )


class TestDetector:
    def test_first_available_alerts(self, tmp_path):
        detector = Detector(cooldown_seconds=180)
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        decision = detector.evaluate("event-1", _result(), state)
        assert decision.should_alert is True
        assert decision.reason == "signature_changed"

    def test_same_signature_within_cooldown_dedupes(self, tmp_path):
        detector = Detector(cooldown_seconds=180)
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        now = datetime.now(timezone.utc)
        result = _result()
        signature = detector.build_signature(result)
        state.set_last_availability_signature("event-1", signature)
        state.set_last_alert_at("event-1", now)

        decision = detector.evaluate("event-1", result, state, now=now + timedelta(seconds=30))
        assert decision.should_alert is False
        assert decision.reason == "deduped"

    def test_same_signature_after_cooldown_alerts(self, tmp_path):
        detector = Detector(cooldown_seconds=180)
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        now = datetime.now(timezone.utc)
        result = _result()
        signature = detector.build_signature(result)
        state.set_last_availability_signature("event-1", signature)
        state.set_last_alert_at("event-1", now)

        decision = detector.evaluate("event-1", result, state, now=now + timedelta(seconds=181))
        assert decision.should_alert is True
        assert decision.reason == "cooldown_elapsed"

    def test_signature_change_alerts_even_within_cooldown(self, tmp_path):
        detector = Detector(cooldown_seconds=180)
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        now = datetime.now(timezone.utc)
        first = _result(section_summary="Section 101")
        second = _result(section_summary="Section 203")
        state.set_last_availability_signature("event-1", detector.build_signature(first))
        state.set_last_alert_at("event-1", now)

        decision = detector.evaluate("event-1", second, state, now=now + timedelta(seconds=10))
        assert decision.should_alert is True
        assert decision.reason == "signature_changed"

    def test_signature_ignores_availability_count_jitter(self):
        detector = Detector(cooldown_seconds=180)
        first = _result(availability_count=1)
        second = _result(availability_count=3)
        assert detector.build_signature(first) == detector.build_signature(second)

    def test_signature_ignores_price_jitter(self):
        detector = Detector(cooldown_seconds=180)
        first = _result(price_summary="$99.00 - $129.00")
        second = _result(price_summary="$100.00 - $130.00")
        assert detector.build_signature(first) == detector.build_signature(second)

    def test_not_available_never_alerts(self, tmp_path):
        detector = Detector(cooldown_seconds=180)
        state = MonitorState(state_file=str(tmp_path / "state.json"))
        decision = detector.evaluate("event-1", _result(available=False), state)
        assert decision.should_alert is False
        assert decision.reason == "not_available"
