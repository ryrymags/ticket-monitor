"""Configurable ticket preference matching.

Replaces the hardcoded bingo rules with user-defined preferences so anyone can
tune the monitor to their own seating and budget requirements.

BINGO definition:
  - The right number of adjacent tickets are available  (count >= min_tickets)
  - The price is within budget                          (price <= max_price_per_ticket)
  - AND: if preferred_sections is non-empty, the listing must be in one of those
    sections.  (If preferred_sections is empty, section is not a BINGO criterion.)

Secondary (orange) alerts:
  - A listing passes count + price but isn't in a preferred section.
  - These fire only when require_preferred_only=False (the default) and
    alert_on_any_availability=True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TicketPreferences:
    """What the user actually wants — loaded from config.yaml → preferences."""

    # Minimum number of adjacent tickets available together
    # (same section, row, and price = physically adjacent seats).
    min_tickets: int = 1

    # Maximum price per ticket (face value, USD).
    max_price_per_ticket: float = 9999.0

    # Section keywords (case-insensitive).  e.g. ["LOGE", "FLOOR", "PIT"]
    # Leave empty to accept any section.
    preferred_sections: list[str] = field(default_factory=list)

    # If True, suppress Discord alerts for listings that are NOT in a preferred
    # section (you'll only hear about preferred-section hits).
    # If False (default), you'll also receive 🟡 orange alerts for other sections.
    require_preferred_only: bool = False

    # If True (default), send a 🟡 orange Discord alert even when tickets don't
    # fully match your preferences — so you know something is out there.
    alert_on_any_availability: bool = True

    # ── Back-compat alias ────────────────────────────────────────────────────
    # config.yaml may still have the old key name; from_dict() handles both.
    # We store the canonical value in require_preferred_only.

    # ---- Convenience helpers ----

    @property
    def has_section_filter(self) -> bool:
        return bool(self.preferred_sections)

    def section_keywords(self) -> list[str]:
        return [s.strip().upper() for s in self.preferred_sections if s.strip()]

    def matches(self, listing_groups: list[dict[str, Any]]) -> dict[str, Any]:
        """Evaluate listing groups against user preferences.

        BINGO  (green):  count ≥ min AND price ≤ max AND section matches (if specified)
        Secondary (orange): count ≥ min AND price ≤ max but section doesn't match

        Returns a dict with:
          matched       bool  — True for BINGO; True for secondary if alert_on_any_availability
          bingo         bool  — True only for a real BINGO hit
          bingo_group   dict | None — the best matching group
          in_preferred  bool  — the match is in a preferred section
          label         str   — human-readable match status
          preview       str   — short status for Discord message subject
          color         int   — Discord embed color (green / orange)
        """
        from src.notifier import COLOR_GREEN, COLOR_ORANGE  # avoid circular at top level

        keywords = self.section_keywords()
        min_tix = max(1, self.min_tickets)
        max_price = float(self.max_price_per_ticket)

        bingo_groups: list[dict[str, Any]] = []     # meets ALL criteria
        secondary_groups: list[dict[str, Any]] = []  # meets count+price but not section

        for group in listing_groups:
            count = int(group.get("count", 0))
            price = float(group.get("price", 0.0))
            section = str(group.get("section", "")).upper()

            if count < min_tix or price > max_price:
                continue

            in_pref = bool(keywords and any(kw in section for kw in keywords))
            if not keywords or in_pref:
                # No section filter → BINGO on count+price alone
                # OR section filter AND this listing is in a preferred section
                bingo_groups.append(group)
            else:
                # Meets count+price but NOT in a preferred section
                secondary_groups.append(group)

        # ── BINGO ────────────────────────────────────────────────────────────
        if bingo_groups:
            bingo_groups.sort(key=lambda g: (-int(g.get("count", 0)), float(g.get("price", 9999.0))))
            best = bingo_groups[0]
            label = self._match_label(best, min_tix, max_price, True, keywords)
            return {
                "matched": True,
                "bingo": True,
                "bingo_group": best,
                "in_preferred": bool(keywords),
                "label": label,
                "preview": "BINGO",
                "color": COLOR_GREEN,
            }

        # ── Secondary / orange alert ──────────────────────────────────────────
        if (
            secondary_groups
            and not self.require_preferred_only
            and self.alert_on_any_availability
        ):
            secondary_groups.sort(key=lambda g: (-int(g.get("count", 0)), float(g.get("price", 9999.0))))
            best = secondary_groups[0]
            count = int(best.get("count", 0))
            price = float(best.get("price", 0.0))
            section = str(best.get("section", "?"))
            row = str(best.get("row", "?"))
            kw_str = "/".join(keywords) if keywords else "preferred section"
            label = (
                f"Available (not preferred section): "
                f"{count}x {section} Row {row} @ ${price:.2f} "
                f"— not in {kw_str}"
            )
            return {
                "matched": True,
                "bingo": False,
                "bingo_group": best,
                "in_preferred": False,
                "label": label,
                "preview": "Available",
                "color": COLOR_ORANGE,
            }

        # ── No alert ─────────────────────────────────────────────────────────
        return {
            "matched": False,
            "bingo": False,
            "bingo_group": None,
            "in_preferred": False,
            "label": self._no_match_label(min_tix, max_price, keywords),
            "preview": "Not a match",
            "color": COLOR_ORANGE,
        }

    # ---- Internal label builders ----

    @staticmethod
    def _no_match_label(min_tix: int, max_price: float, keywords: list[str]) -> str:
        parts = [f"{min_tix}+ tickets together"]
        if max_price < 9999:
            parts.append(f"≤ ${max_price:.0f}/ticket")
        if keywords:
            parts.append("in " + "/".join(keywords))
        return "No match for: " + ", ".join(parts)

    @staticmethod
    def _match_label(group: dict[str, Any], min_tix: int, max_price: float,
                     in_preferred: bool, keywords: list[str]) -> str:
        count = int(group.get("count", 0))
        price = float(group.get("price", 0.0))
        section = str(group.get("section", "?"))
        row = str(group.get("row", "?"))
        base = f"BINGO! {count}x {section} Row {row} @ ${price:.2f}"
        if keywords and in_preferred:
            base += " ✓ preferred section"
        return base

    # ---- Serialization ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_tickets": self.min_tickets,
            "max_price_per_ticket": self.max_price_per_ticket,
            "preferred_sections": list(self.preferred_sections),
            "require_preferred_only": self.require_preferred_only,
            "alert_on_any_availability": self.alert_on_any_availability,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TicketPreferences":
        sections_raw = data.get("preferred_sections", [])
        if isinstance(sections_raw, str):
            sections_raw = [s.strip() for s in sections_raw.split(",") if s.strip()]

        # Support old config key name (require_section_match) for back-compat
        require = data.get("require_preferred_only", data.get("require_section_match", False))

        return cls(
            min_tickets=max(1, int(data.get("min_tickets", 1))),
            max_price_per_ticket=float(data.get("max_price_per_ticket", 9999.0)),
            preferred_sections=[str(s).strip() for s in sections_raw if str(s).strip()],
            require_preferred_only=bool(require),
            alert_on_any_availability=bool(data.get("alert_on_any_availability", True)),
        )
