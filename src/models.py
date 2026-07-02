"""Data models for the ticket monitor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProbeSignalType(Enum):
    NONE = "none"
    DOM = "dom"
    NETWORK = "network"
    DOM_AND_NETWORK = "dom+network"


@dataclass
class ProbeResult:
    event_id: str
    event_url: str
    available: bool
    blocked: bool
    challenge_detected: bool
    signal_type: ProbeSignalType
    signal_confidence: float
    price_summary: str | None
    section_summary: str | None
    raw_indicators: dict[str, Any]
    listing_summary: str | None = None
    # Akamai _abck trust readout at check time: trusted once sensor.js validates the
    # session ("~0~"), flagged while unvalidated/suspicious ("~-1~"). Often flips to
    # flagged before the visible "activity paused" screen appears.
    abck_trusted: bool = False
    abck_flagged: bool = False


@dataclass
class DetectionDecision:
    should_alert: bool
    signature: str
    reason: str
