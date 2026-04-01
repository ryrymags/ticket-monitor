"""Detection decision engine for ticket availability alerts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .models import DetectionDecision, ProbeResult
from .state import MonitorState


class Detector:
    """Computes alert decisions with signature dedupe + cooldown."""

    def __init__(self, cooldown_seconds: int):
        self.cooldown_seconds = cooldown_seconds

    def build_signature(self, result: ProbeResult) -> str:
        raw_count = result.raw_indicators.get("availability_count")
        has_availability_count = isinstance(raw_count, (int, float)) and raw_count > 0
        payload = {
            "event_id": result.event_id,
            "signal_type": result.signal_type.value,
            "section_summary": (result.section_summary or "").strip().lower(),
            "dom_signals": self._normalize_value(result.raw_indicators.get("dom_signals", [])),
            "network_signals": self._normalize_value(result.raw_indicators.get("network_signals", [])),
            # Keep signature stable across minor counter jitter (1 vs 2 responses in a cycle).
            "has_availability_count": has_availability_count,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def evaluate(
        self,
        event_id: str,
        result: ProbeResult,
        state: MonitorState,
        now: datetime | None = None,
    ) -> DetectionDecision:
        now = now or datetime.now(timezone.utc)
        signature = self.build_signature(result)

        if not result.available:
            return DetectionDecision(should_alert=False, signature=signature, reason="not_available")

        last_signature = state.get_last_availability_signature(event_id) or ""
        last_alert_at = state.get_last_alert_at(event_id)

        signature_changed = signature != last_signature
        cooldown_elapsed = (
            last_alert_at is None
            or (now - last_alert_at).total_seconds() >= self.cooldown_seconds
        )

        if signature_changed:
            return DetectionDecision(should_alert=True, signature=signature, reason="signature_changed")

        if cooldown_elapsed:
            return DetectionDecision(should_alert=True, signature=signature, reason="cooldown_elapsed")

        return DetectionDecision(should_alert=False, signature=signature, reason="deduped")

    @staticmethod
    def _normalize_value(value):
        if isinstance(value, dict):
            return {str(k): Detector._normalize_value(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple, set)):
            return sorted([Detector._normalize_value(v) for v in value], key=lambda item: str(item))
        return value
