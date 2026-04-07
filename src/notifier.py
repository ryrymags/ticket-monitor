"""Discord webhook notification sender."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from dateutil import tz

import requests

HISTORY_FILE = "ticket_history.json"
MAX_HISTORY_ENTRIES = 500

logger = logging.getLogger(__name__)

# Discord embed color codes
COLOR_GREEN = 0x00FF00    # Test notification / success
COLOR_BLUE = 0x3498DB      # Status change / informational
COLOR_RED = 0xE74C3C       # Error or back to sold out
COLOR_ORANGE = 0xF39C12    # Available but not a target match

MAX_CONTENT_LEN = 1900
MAX_TITLE_LEN = 256
MAX_DESC_LEN = 4000
MAX_FIELD_NAME_LEN = 256
MAX_FIELD_VALUE_LEN = 1000
MAX_FOOTER_LEN = 200

class DiscordNotifier:
    """Sends formatted notifications to a Discord webhook."""

    def __init__(self, webhook_url: str, username: str = "Ticket Monitor", ping_user_id: str = ""):
        self.webhook_url = webhook_url
        self.username = username
        self.ping_user_id = f"<@{ping_user_id}>" if ping_user_id else ""
        self.session = requests.Session()

    def send_status_change(self, event_name: str, event_date: str, event_url: str,
                           old_status: str, new_status: str) -> bool:
        """Notify when an event's status changes. Mentions user only for onsale."""
        embed = {
            "title": f"Status Change: {event_name}",
            "url": event_url,
            "color": COLOR_BLUE,
            "description": f"Event status changed from **{old_status}** to **{new_status}**.",
            "fields": [
                {"name": "Date", "value": event_date, "inline": True},
                {
                    "name": "Action",
                    "value": f"[Check Ticketmaster]({event_url})",
                    "inline": False,
                },
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Only ping for onsale — that's when tickets are available
        mention = self.ping_user_id if new_status == "onsale" else ""
        return self._send(embeds=[embed], content=mention, retries=2)

    def send_price_range_appeared(self, event_name: str, event_date: str, event_url: str,
                                   price_min: float, price_max: float) -> bool:
        """Notify when price ranges appear on a previously sold-out event (status unchanged)."""
        embed = {
            "title": f"Price Range Appeared: {event_name}",
            "url": event_url,
            "color": COLOR_BLUE,
            "description": (
                f"Price data appeared in the API for this event — tickets may be available.\n"
                f"Price range: **${price_min:.0f} – ${price_max:.0f}**"
            ),
            "fields": [
                {"name": "Date", "value": event_date, "inline": True},
                {
                    "name": "Action",
                    "value": f"[Check Ticketmaster]({event_url})",
                    "inline": False,
                },
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._send(embeds=[embed], content=self.ping_user_id, retries=2)

    def send_page_resale_detected(self, event_name: str, event_date: str, event_url: str,
                                   sections: list[str], price_info: str | None) -> bool:
        """Notify when the page checker detects a resale/FVE listing on the event page."""
        detail = f"Sections: {', '.join(sections)}" if sections else "Check the event page for details."
        if price_info:
            detail += f" | Price: **{price_info}**"
        embed = {
            "title": f"Resale Detected on Page: {event_name}",
            "url": event_url,
            "color": COLOR_BLUE,
            "description": (
                "The page checker found a resale or Face Value Exchange listing on "
                f"the Ticketmaster event page.\n{detail}"
            ),
            "fields": [
                {"name": "Date", "value": event_date, "inline": True},
                {"name": "Action", "value": f"[Check Ticketmaster]({event_url})", "inline": False},
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._send(embeds=[embed], content=self.ping_user_id, retries=2)

    def send_ticket_available(
        self,
        event_name: str,
        event_date: str,
        event_url: str,
        signal_type: str,
        signal_confidence: float,
        price_summary: str | None,
        section_summary: str | None,
        reason: str,
        listing_summary: str | None = None,
        listing_groups: list[dict[str, Any]] | None = None,
        mention: bool = True,
        preferences=None,
    ) -> bool:
        """Notify when browser probe detects available inventory."""
        match = self._ticket_match_status(listing_groups, preferences=preferences)
        groups = self._normalized_listing_groups(listing_groups)
        trigger_label = self._trigger_label(reason)

        # ── Build description: lead with status, then every listing ─────────
        detail_lines: list[str] = []
        is_bingo = match.get("preview_status") == "BINGO"

        if is_bingo:
            detail_lines.append(f"🟢 **{match['label']}**")
        else:
            detail_lines.append(f"🟡 **{match['label']}**")

        if match.get("unknown_row"):
            detail_lines.append(
                "_Row data missing in Ticketmaster payload — "
                "seat adjacency confidence is lower._"
            )

        detail_lines.append("")  # blank line before listings

        # Show every detected listing group, not just the best match.
        if groups:
            detail_lines.append("**All listings detected:**")
            # Sort: preferred-section matches first, then by price ascending.
            for g in sorted(groups, key=lambda x: (x.get("price", 0), x.get("section", ""))):
                sect = g.get("section", "?")
                row = g.get("row", "?")
                price = g.get("price", 0.0)
                count = g.get("count", 0)
                row_str = f" · Row {row}" if row and row != "?" else ""
                detail_lines.append(
                    f"• **{sect}**{row_str} — "
                    f"{count} ticket{'s' if count != 1 else ''} @ "
                    f"**${price:,.2f}** each"
                )
        else:
            # No structured listing data — fall back to summary strings.
            if price_summary:
                detail_lines.append(f"Price range: **{price_summary}**")
            if section_summary:
                detail_lines.append(f"Sections: {section_summary}")
            if listing_summary:
                detail_lines.append(f"Listing: {listing_summary}")

        detail_lines.append("")  # blank line before meta
        detail_lines.append(f"Alert trigger: {trigger_label}")

        embed = {
            "title": f"{'🟢 BINGO' if is_bingo else '🟡 Tickets Available'}: {event_name}",
            "url": event_url,
            "color": int(match["color"]),
            "description": "\n".join(detail_lines),
            "fields": [
                {"name": "Date", "value": self._format_event_date(event_date), "inline": True},
                {
                    "name": "Action",
                    "value": f"[**Open Ticketmaster Now**]({event_url})",
                    "inline": False,
                },
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        content = self._ticket_preview_content(
            mention=mention,
            event_name=event_name,
            preview_status=str(match["preview_status"]),
        )
        sent = self._send(embeds=[embed], content=content, retries=2)

        # Write to local history file for real detections only — skip synthetic test alerts.
        if signal_type != "synthetic":
            try:
                self._write_history_entry(
                    event_name=event_name,
                    event_date=event_date,
                    event_url=event_url,
                    all_groups=groups,
                    is_bingo=bool(is_bingo),
                    label=str(match.get("label", "")),
                )
            except Exception as _hist_exc:
                logger.debug("History write skipped: %s", _hist_exc)

        return sent

    def send_monitor_blocked(
        self,
        event_name: str,
        message: str,
        *,
        context: dict | None = None,
        auto_fix_planned: str | None = None,
        manual_required: bool = False,
    ) -> bool:
        """Notify when the monitor is in a blind/blocked outage state."""
        monitor_doing = self._auto_fix_plan_label(auto_fix_planned) or (
            "The monitor will keep retrying checks and self-healing in the background."
        )
        user_action = (
            "No action needed right now unless this repeats for several minutes."
            if not manual_required
            else "Manual action is required now."
        )
        description = self._build_guided_description(
            what_happened=message,
            monitor_doing=monitor_doing,
            user_action=user_action,
            include_technical=True,
            alert_code="monitor_outage",
            action=None,
            reason=message,
            context=context,
        )
        embed = {
            "title": f"Monitor Outage: {event_name}",
            "color": COLOR_RED,
            "description": description,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        content = self.ping_user_id if self._should_ping(manual_required) else ""
        return self._send(embeds=[embed], content=content, retries=1)

    def send_monitor_recovered(self, event_name: str, message: str) -> bool:
        """Notify when monitor recovers from outage state."""
        embed = {
            "title": f"Monitor Recovered: {event_name}",
            "color": COLOR_BLUE,
            "description": message,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._send(embeds=[embed], retries=1)

    def send_auto_fix_action(
        self,
        action: str,
        reason: str,
        *,
        context: dict | None = None,
        auto_fix_planned: str | None = None,
        manual_required: bool = False,
    ) -> bool:
        """Notify when an automatic remediation action has been executed."""
        what_happened, default_monitor_doing, default_user_action = self._auto_fix_action_guidance(action)
        monitor_doing = self._auto_fix_plan_label(auto_fix_planned) or default_monitor_doing
        description = self._build_guided_description(
            what_happened=what_happened,
            monitor_doing=monitor_doing,
            user_action=default_user_action,
            include_technical=True,
            alert_code="auto_fix_action",
            action=action,
            reason=reason,
            context=context,
        )
        embed = {
            "title": "Auto-fix Action Taken",
            "color": COLOR_BLUE,
            "description": description,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        content = self.ping_user_id if self._should_ping(manual_required) else ""
        return self._send(embeds=[embed], content=content, retries=1)

    def send_critical_attention(
        self,
        message: str,
        *,
        context: dict | None = None,
        manual_required: bool = True,
        next_steps: list[str] | None = None,
    ) -> bool:
        """Notify when automation cannot recover and manual intervention is needed."""
        description = self._build_guided_description(
            what_happened=message,
            monitor_doing="Automatic recovery is paused or no longer enough.",
            user_action=self._manual_action_text(next_steps),
            include_technical=True,
            alert_code="critical_attention",
            action=None,
            reason=message,
            context=context,
        )
        embed = {
            "title": "Critical Attention Needed",
            "color": COLOR_RED,
            "description": description,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        content = self.ping_user_id if self._should_ping(manual_required) else ""
        return self._send(embeds=[embed], content=content, retries=1)

    def send_sold_out_again(self, event_name: str, event_date: str, event_url: str) -> bool:
        """Notify when an event goes back to sold out / offsale."""
        embed = {
            "title": f"Back to Sold Out: {event_name}",
            "url": event_url,
            "color": COLOR_RED,
            "description": "Tickets are no longer available. The monitor will keep checking.",
            "fields": [
                {"name": "Date", "value": event_date, "inline": True},
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._send(embeds=[embed])

    def send_heartbeat(self, uptime_hours: float,
                       last_check: datetime | None,
                       event_statuses: list[dict] | None = None) -> bool:
        """Send a periodic heartbeat to confirm the monitor is alive."""
        last_check_str = last_check.astimezone(tz.gettz("US/Eastern")).strftime("%I:%M %p ET") if last_check else "Never"

        fields = []
        for es in (event_statuses or []):
            raw_check = es.get("last_check")
            check_str = (
                raw_check.astimezone(tz.gettz("US/Eastern")).strftime("%I:%M %p ET")
                if raw_check else "Never"
            )
            fields.append({
                "name": es["name"],
                "value": f"{es['status']} | Last check: {check_str}",
                "inline": False,
            })

        embed = {
            "title": "Monitor Heartbeat",
            "color": COLOR_BLUE,
            "description": (
                f"Uptime: **{uptime_hours:.1f} hours**\n"
                f"Last successful check: **{last_check_str}**"
            ),
            "fields": fields,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._send(embeds=[embed])

    def send_test(self) -> bool:
        """Send a test notification to verify the webhook works."""
        embed = {
            "title": "Test Notification — Monitor Connected",
            "color": COLOR_GREEN,
            "description": (
                "Your Ticketmaster Face Value Exchange monitor is configured correctly.\n\n"
                "You will receive alerts here when any tickets are detected for your configured event nights."
            ),
            "fields": [
                {"name": "Webhook", "value": "Working", "inline": True},
                {"name": "Status", "value": "Ready", "inline": True},
            ],
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._send(embeds=[embed])

    def send_daily_recap(self, event_summaries: list[dict]) -> bool:
        """Send a daily 11PM recap summarizing the day's monitoring activity."""
        lines = []
        for summary in event_summaries:
            name = summary["name"]
            statuses = summary.get("statuses_seen", ["unknown"])
            current_status = statuses[-1] if statuses else "unknown"
            price_ranges_seen = summary.get("price_ranges_seen", False)

            if current_status == "offsale" and not price_ranges_seen:
                lines.append(f"**{name}**: Still offsale. No ticket activity today.")
            elif price_ranges_seen:
                lines.append(f"**{name}**: Price data appeared! Status: **{current_status}**.")
            else:
                lines.append(f"**{name}**: Status: **{current_status}**. No price data today.")

        description = "\n".join(lines)

        embed = {
            "title": "Daily Recap",
            "color": COLOR_BLUE,
            "description": description,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return self._send(embeds=[embed])

    def send_error(
        self,
        message: str,
        *,
        context: dict | None = None,
        manual_required: bool = False,
        next_steps: list[str] | None = None,
    ) -> bool:
        """Send an error notification."""
        user_action = (
            self._manual_action_text(next_steps)
            if manual_required
            else "No action needed right now unless this keeps repeating."
        )
        description = self._build_guided_description(
            what_happened=message,
            monitor_doing="The monitor will retry with backoff and self-healing.",
            user_action=user_action,
            include_technical=True,
            alert_code="monitor_error",
            action=None,
            reason=message,
            context=context,
        )
        embed = {
            "title": "Monitor Error",
            "color": COLOR_RED,
            "description": description,
            "footer": {"text": "Face Value Exchange Monitor"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        content = self.ping_user_id if self._should_ping(manual_required) else ""
        return self._send(embeds=[embed], content=content)

    # ---- Internal ----

    @staticmethod
    def _listings_fingerprint(groups: list[dict[str, Any]]) -> str:
        """Produce a short fingerprint of a set of listing groups for dedup."""
        import hashlib
        parts = []
        for g in sorted(groups, key=lambda x: (x.get("section", ""), x.get("row", ""), x.get("price", 0))):
            parts.append(f"{g.get('section','')}/{g.get('row','')}/{g.get('price',0)}/{g.get('count',0)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    @staticmethod
    def _write_history_entry(
        event_name: str,
        event_date: str,
        event_url: str,
        all_groups: list[dict[str, Any]],
        is_bingo: bool,
        label: str,
    ) -> None:
        """Append a ticket appearance entry to ticket_history.json.

        Saves ALL detected listing groups (not just the best match).
        Deduplicates: skips writing if the most recent entry for the same
        event has the same set of listings (same fingerprint).
        """
        m = re.search(r"/event/([A-Z0-9]+)", event_url, re.IGNORECASE)
        event_id = m.group(1) if m else ""

        # Build a fingerprint of the current listings for dedup.
        fingerprint = DiscordNotifier._listings_fingerprint(all_groups) if all_groups else ""

        # Serialize each listing group into the entry.
        listings: list[dict[str, Any]] = []
        for g in sorted(all_groups, key=lambda x: (x.get("price", 0), x.get("section", ""))):
            listings.append({
                "section": g.get("section", "?"),
                "row": g.get("row", "?"),
                "price": round(g.get("price", 0.0), 2),
                "count": g.get("count", 0),
            })

        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_name": event_name,
            "event_id": event_id,
            "event_date": event_date,
            "bingo": is_bingo,
            "label": label,
            "fingerprint": fingerprint,
            "listings": listings,
        }

        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history: list = json.load(f)
                if not isinstance(history, list):
                    history = []
            else:
                history = []

            # Dedup: skip if the last entry for this event has the same fingerprint.
            if fingerprint and history:
                for prev in reversed(history):
                    if prev.get("event_id") == event_id:
                        if prev.get("fingerprint") == fingerprint:
                            return  # same listings still up — don't spam
                        break  # different listings — proceed with new entry

            history.append(entry)
            if len(history) > MAX_HISTORY_ENTRIES:
                history = history[-MAX_HISTORY_ENTRIES:]

            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to write ticket history: %s", exc)

    def _send(self, embeds: list[dict], content: str = "", retries: int = 0) -> bool:
        """Send a webhook payload to Discord, with optional retries on transient failures."""
        sanitized_embeds = self._sanitize_embeds(embeds)
        payload = {
            "username": self.username,
            "embeds": sanitized_embeds,
        }
        if content:
            payload["content"] = self._truncate(content, MAX_CONTENT_LEN)

        for attempt in range(retries + 1):
            try:
                resp = self.session.post(self.webhook_url, json=payload, timeout=10)
                if resp.status_code == 204:
                    logger.debug("Discord notification sent successfully")
                    return True
                elif resp.status_code == 429:
                    logger.warning("Discord rate limited: %s", resp.text)
                    return False
                else:
                    logger.error("Discord webhook error %d: %s", resp.status_code, resp.text[:200])
                    return False
            except requests.RequestException as e:
                if attempt < retries:
                    delay = 2 ** attempt  # 1s, 2s
                    logger.warning("Discord webhook request failed (attempt %d/%d): %s — retrying in %ds",
                                   attempt + 1, retries + 1, e, delay)
                    time.sleep(delay)
                else:
                    logger.error("Discord webhook request failed: %s", e)
                    return False

        return False

    def _sanitize_embeds(self, embeds: list[dict]) -> list[dict]:
        sanitized = []
        for embed in embeds[:10]:
            if not isinstance(embed, dict):
                continue
            e = dict(embed)
            if "title" in e:
                e["title"] = self._truncate(str(e["title"]), MAX_TITLE_LEN)
            if "description" in e:
                e["description"] = self._truncate(str(e["description"]), MAX_DESC_LEN)

            fields = []
            for field in e.get("fields", [])[:25]:
                if not isinstance(field, dict):
                    continue
                name = self._truncate(str(field.get("name", "")), MAX_FIELD_NAME_LEN)
                value = self._truncate(str(field.get("value", "")), MAX_FIELD_VALUE_LEN)
                fields.append({
                    "name": name or "Detail",
                    "value": value or "-",
                    "inline": bool(field.get("inline", False)),
                })
            if fields:
                e["fields"] = fields
            elif "fields" in e:
                del e["fields"]

            footer = e.get("footer")
            if isinstance(footer, dict) and "text" in footer:
                e["footer"] = {"text": self._truncate(str(footer["text"]), MAX_FOOTER_LEN)}

            sanitized.append(e)
        return sanitized or [{
            "title": "Monitor Message",
            "description": "A notification was generated but had no valid embed payload.",
            "color": COLOR_RED,
        }]

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        if max_len <= 3:
            return value[:max_len]
        return value[: max_len - 3] + "..."

    @staticmethod
    def _format_event_date(event_date: str) -> str:
        # %#d on Windows, %-d on Unix — remove leading zero from day.
        _day_fmt = "%#d" if os.name == "nt" else "%-d"
        _full_fmt = f"%A, %B {_day_fmt}, %Y"
        try:
            dt = datetime.strptime(event_date, "%Y-%m-%d")
            return dt.strftime(_full_fmt)
        except ValueError:
            try:
                dt = datetime.fromisoformat(event_date)
                return dt.strftime(_full_fmt)
            except ValueError:
                return event_date

    @staticmethod
    def _signal_source_label(signal_type: str) -> str:
        mapping = {
            "dom": "Page UI signals",
            "network": "Ticket inventory network responses",
            "dom+network": "Both page UI and network inventory signals",
            "none": "No positive availability signal",
            "synthetic": "Synthetic test signal",
        }
        return mapping.get(signal_type, signal_type)

    @staticmethod
    def _confidence_label(value: float) -> str:
        if value >= 0.9:
            return "very high"
        if value >= 0.75:
            return "high"
        if value >= 0.5:
            return "medium"
        return "low"

    @staticmethod
    def _trigger_label(reason: str) -> str:
        mapping = {
            "signature_changed": "New inventory pattern detected",
            "cooldown_elapsed": "Reminder after cooldown window",
            "attention_burst": "Attention burst reminder",
            "manual_test": "Manual test alert",
            "not_available": "No availability (should not alert)",
            "deduped": "Duplicate signal suppressed",
        }
        return mapping.get(reason, reason)

    @staticmethod
    def _coerce_price(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace("$", "").replace(",", "")
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_count(value: Any) -> int:
        if isinstance(value, bool):
            return 1
        if isinstance(value, int):
            return max(1, value)
        if isinstance(value, float):
            return max(1, int(value))
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if not cleaned:
                return 1
            try:
                return max(1, int(float(cleaned)))
            except ValueError:
                return 1
        return 1

    @classmethod
    def _normalized_listing_groups(cls, listing_groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if not isinstance(listing_groups, list):
            return []

        normalized: list[dict[str, Any]] = []
        for raw in listing_groups:
            if not isinstance(raw, dict):
                continue
            price = cls._coerce_price(raw.get("price"))
            if price is None or price <= 0:
                continue

            section = str(raw.get("section", "")).strip().upper()
            row = str(raw.get("row", "")).strip() or "?"
            count = cls._coerce_count(raw.get("count"))
            normalized.append(
                {
                    "section": section,
                    "row": row,
                    "price": round(price, 2),
                    "count": count,
                }
            )
        return normalized

    @staticmethod
    def _best_match_group(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not groups:
            return None
        return sorted(
            groups,
            key=lambda item: (
                -int(item.get("count", 0)),
                float(item.get("price", 0.0)),
                str(item.get("section", "")),
                str(item.get("row", "")),
            ),
        )[0]

    @classmethod
    def _ticket_match_status(
        cls,
        listing_groups: list[dict[str, Any]] | None,
        preferences=None,
    ) -> dict[str, Any]:
        """Evaluate availability against user-configurable preferences.

        If no preferences object is supplied, any availability is treated as a
        generic alert (no bingo / no-match distinction).
        """
        groups = cls._normalized_listing_groups(listing_groups)

        # No preferences configured — just report availability generically.
        if preferences is None:
            best = cls._best_match_group(groups)
            if best:
                return {
                    "label": "Tickets available",
                    "preview_status": "Available",
                    "color": COLOR_GREEN,
                    "matched_group": best,
                    "unknown_row": isinstance(best, dict) and str(best.get("row", "")) == "?",
                }
            return {
                "label": "Tickets may be available",
                "preview_status": "Available",
                "color": COLOR_ORANGE,
                "matched_group": None,
                "unknown_row": False,
            }

        # Use configurable preferences.
        result = preferences.matches(groups)
        matched_group = result.get("bingo_group")
        return {
            "label": result["label"],
            "preview_status": result["preview"],
            "color": result["color"],
            "matched_group": matched_group,
            "unknown_row": isinstance(matched_group, dict) and str(matched_group.get("row", "")) == "?",
        }

    def _ticket_preview_content(self, *, mention: bool, event_name: str, preview_status: str) -> str:
        if not mention:
            return ""
        line = f"Tickets Available: {event_name} — {preview_status}"
        if self.ping_user_id:
            return f"{self.ping_user_id} {line}"
        return line

    def _build_guided_description(
        self,
        *,
        what_happened: str,
        monitor_doing: str,
        user_action: str,
        include_technical: bool,
        alert_code: str,
        action: str | None,
        reason: str | None,
        context: dict | None,
    ) -> str:
        sections = [
            f"**What happened**\n{what_happened}",
            f"**What monitor is doing**\n{monitor_doing}",
            f"**What you should do**\n{user_action}",
        ]
        if include_technical:
            technical = self._technical_block(
                alert_code=alert_code,
                action=action,
                reason=reason,
                context=context,
            )
            if technical:
                sections.append(f"**Technical**\n{technical}")
        return "\n\n".join(sections)

    @staticmethod
    def _should_ping(manual_required: bool) -> bool:
        return bool(manual_required)

    @staticmethod
    def _manual_action_text(next_steps: list[str] | None) -> str:
        if not next_steps:
            return "Manual action required. Check the monitor app for status and recent logs."
        commands = "\n".join(f"`{step}`" for step in next_steps)
        return f"Run these commands now:\n{commands}"

    @staticmethod
    def _auto_fix_plan_label(auto_fix_planned: str | None) -> str | None:
        if not auto_fix_planned:
            return None
        mapping = {
            "browser_recycle_now": (
                "Automatic fix in progress: browser recycle now; "
                "service-level watchdog may restart process if health stays stale."
            ),
            "probe_reload_after_reauth": "Automatic fix in progress: reloading browser probe with refreshed auth session.",
            "retry_auto_reauth": "Automatic fix in progress: monitor will retry auto re-login while attempt limits allow.",
            "launchd_restart_expected": "Automatic fix in progress: launchd should restart the monitor process automatically.",
            "health_recheck": "Automatic fix in progress: watchdog will re-check service health on its next cycle.",
            "event_poll_stale_recycle": "Automatic fix in progress: recycling browser context after stale event checks.",
        }
        return mapping.get(auto_fix_planned)

    @staticmethod
    def _auto_fix_action_guidance(action: str) -> tuple[str, str, str]:
        if action == "browser_recycled":
            return (
                "The browser context was recycled after repeated blind/error checks.",
                "The monitor will retry checks on the next cycle.",
                "No action needed right now.",
            )
        if action.startswith("kill_playwright_orphans("):
            return (
                "The watchdog restarted the monitor and cleaned orphaned browser processes.",
                "Service health should recover within about one check cycle.",
                "No action needed right now unless this repeats for several minutes.",
            )
        if action == "ticketmaster_reauth_success":
            return (
                "Auto re-login succeeded and session state was refreshed.",
                "The monitor is continuing with the refreshed session.",
                "No action needed right now.",
            )
        if action == "ticketmaster_reauth_failed":
            return (
                "Auto re-login attempt failed.",
                "The monitor may retry auto re-login while within attempt limits.",
                "No action needed right now unless failures continue.",
            )
        if action == "process_restart_requested":
            return (
                "The monitor requested a clean process restart after repeated browser errors.",
                "launchd should restart the process automatically.",
                "No action needed right now unless the service fails to come back.",
            )
        if action == "code_change_restart":
            return (
                "A local file change was detected and the monitor was restarted after preflight checks.",
                "Monitoring will continue with updated code/config.",
                "No action needed right now.",
            )
        return (
            "An automatic remediation action was executed.",
            "The monitor is attempting to recover automatically.",
            "No action needed right now unless this repeats.",
        )

    def _technical_block(
        self,
        *,
        alert_code: str,
        action: str | None,
        reason: str | None,
        context: dict | None,
    ) -> str:
        ctx = context if isinstance(context, dict) else {}
        lines = [f"alert_code={alert_code}"]
        lines.append(f"action={action or 'none'}")
        if reason:
            lines.append(f"reason={reason}")

        event_name = ctx.get("event_name")
        event_id = ctx.get("event_id")
        if event_name and event_id:
            lines.append(f"event={event_name} ({event_id})")
        elif event_name or event_id:
            lines.append(f"event={event_name or event_id}")

        signal = ctx.get("signal")
        if signal is not None:
            lines.append(f"signal={signal}")

        blocked = ctx.get("blocked")
        challenge = ctx.get("challenge")
        if blocked is not None or challenge is not None:
            lines.append(
                f"blocked={str(bool(blocked)).lower()}/challenge={str(bool(challenge)).lower()}"
            )

        consecutive = ctx.get("consecutive")
        if consecutive is not None:
            lines.append(f"consecutive={consecutive}")

        reason_code = ctx.get("reason_code")
        if reason_code:
            lines.append(f"reason_code={reason_code}")

        last_check_age = ctx.get("last_check_age_seconds")
        if last_check_age is not None:
            lines.append(f"last_check_age_seconds={last_check_age}")

        stale_threshold = ctx.get("stale_threshold_seconds")
        if stale_threshold is not None:
            lines.append(f"stale_threshold_seconds={stale_threshold}")

        return "\n".join(lines)
