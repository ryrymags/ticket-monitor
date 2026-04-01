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


@dataclass
class DetectionDecision:
    should_alert: bool
    signature: str
    reason: str
