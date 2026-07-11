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

import re
from dataclasses import dataclass, field
from typing import Any

# Ticketmaster names the same section differently across payloads (the seat-map
# geometry says "BALCONY 325", the listing feed says "BAL325"). Canonicalize the
# leading word so either convention matches the other.
_SECTION_PREFIX_ALIASES = {
    "BALCONY": "BAL",
    "BALC": "BAL",
    "MEZZANINE": "MEZZ",
    "MEZ": "MEZZ",
    "ORCHESTRA": "ORCH",
    "ORC": "ORCH",
    "FLOOR": "FLR",
    "SECTION": "SEC",
    "SECT": "SEC",
    "GENERALADMISSION": "GA",
    "LOWER": "LWR",
    "UPPER": "UPR",
}


def canonical_section_key(name: str) -> str:
    """Normalize a section name so naming variants compare equal.

    Uppercases, strips spaces/punctuation, and maps known long-form prefixes to
    their listing-feed abbreviations: "BALCONY 325" and "BAL325" → "BAL325".
    """
    s = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    m = re.match(r"([A-Z]+)(.*)", s)
    if m:
        prefix, rest = m.groups()
        alias = _SECTION_PREFIX_ALIASES.get(prefix)
        if alias is None and len(prefix) >= 3:
            # Progressive typing: "BALCO" is unambiguously partway to "BALCONY",
            # so resolve it to the same abbreviation the full word would get.
            hits = {
                short
                for long, short in _SECTION_PREFIX_ALIASES.items()
                if long.startswith(prefix)
            }
            if len(hits) == 1:
                alias = hits.pop()
        s = (alias or prefix) + rest
    return s


def dedupe_section_names(names: list[str]) -> list[str]:
    """Collapse naming variants of the same section into one display name.

    Groups by canonical key and keeps the most descriptive (longest) variant,
    so ["BAL325", "BALCONY 325"] → ["BALCONY 325"]. Sorted by canonical key.
    """
    groups: dict[str, str] = {}
    for name in names or []:
        name = str(name).strip()
        key = canonical_section_key(name)
        if not key:
            continue
        current = groups.get(key)
        if current is None or (len(name), name) > (len(current), current):
            groups[key] = name
    return [groups[k] for k in sorted(groups)]


def section_families(names: list[str]) -> list[str]:
    """Derive section-type prefixes from concrete section names.

    Thirty BALCONY3xx sections clearly mean "BALCONY" is a seating type, so a
    user can add the whole family instead of picking sections one by one (the
    matcher treats a bare prefix as a match-all for that family). A family is
    the leading letters of names that end in digits, alias-grouped (BAL304 and
    BALCONY 325 both count toward "BALCONY"), and must cover at least two
    distinct sections — a single LAWN1 doesn't make LAWN a "type".
    """
    groups: dict[str, dict[str, Any]] = {}
    for name in names or []:
        m = re.match(r"([A-Za-z][A-Za-z ]*?)[\s\-]*\d", str(name).strip())
        if not m:
            continue
        display = m.group(1).strip().upper()
        if len(display) < 2:
            continue
        key = canonical_section_key(display)
        group = groups.setdefault(key, {"display": display, "members": set()})
        # Prefer the most descriptive spelling (BALCONY over BAL) for display.
        if (len(display), display) > (len(group["display"]), group["display"]):
            group["display"] = display
        group["members"].add(canonical_section_key(name))
    return sorted(g["display"] for g in groups.values() if len(g["members"]) >= 2)


def keyword_matches_section(keyword_upper: str, section_upper: str) -> bool:
    """Substring match, tried in both raw and canonical form.

    Raw substring preserves the historical behavior ("LOGE" matches "LOGE 5");
    the canonical pass makes cross-convention keywords work ("BAL325" matches
    "BALCONY 325" and vice versa).
    """
    if keyword_upper in section_upper:
        return True
    key = canonical_section_key(keyword_upper)
    return bool(key) and key in canonical_section_key(section_upper)


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

    # Human-friendly label for this BINGO category.
    name: str = "BINGO"

    # Event IDs (from the events list) this config applies to.
    # Empty (the default) = applies to every event, including ones added later.
    event_ids: list[str] = field(default_factory=list)

    # ── Back-compat alias ────────────────────────────────────────────────────
    # config.yaml may still have the old key name; from_dict() handles both.
    # We store the canonical value in require_preferred_only.

    # ---- Convenience helpers ----

    @property
    def has_section_filter(self) -> bool:
        return bool(self.preferred_sections)

    def section_keywords(self) -> list[str]:
        return [s.strip().upper() for s in self.preferred_sections if s.strip()]

    def applies_to_event(self, event_id: str) -> bool:
        """True if this config should be evaluated for the given event.

        An empty event_ids list means the config is global (all events).
        Comparison is case-insensitive so hand-edited YAML can't silently
        mismatch on event-ID casing.
        """
        scoped = [str(e).strip().upper() for e in self.event_ids if str(e).strip()]
        return not scoped or str(event_id or "").strip().upper() in scoped

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

            in_pref = bool(keywords and any(keyword_matches_section(kw, section) for kw in keywords))
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
            "name": self.name,
            "min_tickets": self.min_tickets,
            "max_price_per_ticket": self.max_price_per_ticket,
            "preferred_sections": list(self.preferred_sections),
            "require_preferred_only": self.require_preferred_only,
            "alert_on_any_availability": self.alert_on_any_availability,
            "event_ids": list(self.event_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TicketPreferences":
        data = data or {}
        sections_raw = data.get("preferred_sections", [])
        if isinstance(sections_raw, str):
            sections_raw = [s.strip() for s in sections_raw.split(",") if s.strip()]

        # Support old config key name (require_section_match) for back-compat
        require = data.get("require_preferred_only", data.get("require_section_match", False))

        event_ids_raw = data.get("event_ids", [])
        if isinstance(event_ids_raw, str):
            event_ids_raw = [e.strip() for e in event_ids_raw.split(",") if e.strip()]
        elif not isinstance(event_ids_raw, list):
            event_ids_raw = []

        return cls(
            name=str(data.get("name", "BINGO")).strip() or "BINGO",
            min_tickets=max(1, int(data.get("min_tickets", 1))),
            max_price_per_ticket=float(data.get("max_price_per_ticket", 9999.0)),
            preferred_sections=[str(s).strip() for s in sections_raw if str(s).strip()],
            require_preferred_only=bool(require),
            alert_on_any_availability=bool(data.get("alert_on_any_availability", True)),
            event_ids=[str(e).strip() for e in event_ids_raw if str(e).strip()],
        )


def configs_for_event(preferences, event_id: str):
    """Filter preference config(s) down to those that apply to ``event_id``.

    Accepts a single TicketPreferences, a list/tuple of them, or None (returned
    as-is). Configs without an ``event_ids`` attribute — or with an empty one —
    are treated as global, so pre-scoping configs behave exactly as before.
    """
    if preferences is None:
        return None
    configs = preferences if isinstance(preferences, (list, tuple)) else [preferences]
    selected = []
    for pref in configs:
        if pref is None:
            continue
        scoped = [str(e).strip().upper() for e in (getattr(pref, "event_ids", None) or []) if str(e).strip()]
        if not scoped or str(event_id or "").strip().upper() in scoped:
            selected.append(pref)
    return selected
