"""Tests for Discord notifier — embed colors and notification methods."""

from unittest.mock import MagicMock, patch

from src.notifier import (
    DiscordNotifier,
    COLOR_GREEN,
    COLOR_BLUE,
    COLOR_RED,
    COLOR_ORANGE,
)
from src.preferences import TicketPreferences


class TestNotificationColors:
    """Verify that each notification type uses the correct embed color."""

    def test_status_change_uses_blue(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_status_change("Test", "2026-07-28", "http://test", "offsale", "onsale")
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_BLUE

    def test_price_range_appeared_uses_blue(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_price_range_appeared("Test", "2026-07-28", "http://test", 50.0, 150.0)
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_BLUE

    def test_sold_out_again_uses_red(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_sold_out_again("Test", "2026-07-28", "http://test")
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_RED

    def test_heartbeat_uses_blue(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_heartbeat(uptime_hours=24.0, last_check=None)
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_BLUE

    def test_test_notification_uses_green(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_test()
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_GREEN

    def test_error_uses_red(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_error("Something broke")
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_RED

    def test_ticket_available_non_bingo_uses_orange(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom",
                signal_confidence=0.9,
                price_summary="$99 - $129",
                section_summary="Section 101",
                reason="signature_changed",
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_ORANGE

    def test_ticket_available_type_1_bingo_uses_green(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        prefs = TicketPreferences(min_tickets=4, max_price_per_ticket=220.0, preferred_sections=["LOGE"])
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$199.50 - $199.50",
                section_summary="LOGE20",
                reason="signature_changed",
                listing_groups=[{"section": "LOGE20", "row": "14", "price": 199.5, "count": 4}],
                preferences=prefs,
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_GREEN
            assert "BINGO" in embed["description"]

    def test_ticket_available_type_2_bingo_uses_green(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        prefs = TicketPreferences(min_tickets=3, max_price_per_ticket=125.0)
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="network",
                signal_confidence=0.85,
                price_summary="$120.00 - $120.00",
                section_summary="BALCONY301",
                reason="signature_changed",
                listing_groups=[{"section": "BALCONY301", "row": "6", "price": 120.0, "count": 3}],
                preferences=prefs,
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_GREEN
            assert "BINGO" in embed["description"]

    def test_monitor_blocked_uses_red(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_monitor_blocked("Night 1", "blocked")
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_RED


class TestStatusChangeMention:
    """Verify that onsale status changes include a user mention."""

    def test_onsale_includes_mention(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_status_change("Test", "2026-07-28", "http://test", "offsale", "onsale")
            content = mock_send.call_args[1].get("content", "")
            assert "<@" in content

    def test_offsale_no_mention(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_status_change("Test", "2026-07-28", "http://test", "onsale", "offsale")
            content = mock_send.call_args[1].get("content", "")
            assert content == ""


class TestTicketAvailableMention:
    def test_ticket_available_mention_can_be_disabled(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$200.10 - $200.10",
                section_summary="LOGE20",
                reason="attention_burst",
                mention=False,
            )
            assert mock_send.call_args[1].get("content", "") == ""

    def test_ticket_available_mention_has_preview_title_on_same_line(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        prefs = TicketPreferences(min_tickets=4, max_price_per_ticket=220.0, preferred_sections=["LOGE"])
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test Event",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$199.50 - $199.50",
                section_summary="LOGE20",
                reason="signature_changed",
                listing_groups=[{"section": "LOGE20", "row": "14", "price": 199.5, "count": 4}],
                mention=True,
                preferences=prefs,
            )
            content = mock_send.call_args[1].get("content", "")
            assert content.startswith("<@123456789> Tickets Available: Test Event")
            assert "BINGO" in content

    def test_ticket_available_includes_listing_summary(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$200.10 - $200.10",
                section_summary="LOGE20",
                reason="signature_changed",
                listing_summary="LOGE20 / Row 14 / $200.10 x3",
            )
            embed = mock_send.call_args[1]["embeds"][0]
            # No structured listing_groups → falls back to summary strings.
            assert "Listing: LOGE20 / Row 14 / $200.10 x3" in embed["description"]

    def test_ticket_available_unknown_row_includes_warning(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$199.50 - $199.50",
                section_summary="LOGE20",
                reason="signature_changed",
                listing_groups=[{"section": "LOGE20", "row": "?", "price": 199.5, "count": 4}],
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert "Row data missing in Ticketmaster payload" in embed["description"]


class TestGuidedNotifications:
    def test_monitor_blocked_includes_action_matrix_and_autofix_plan(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_monitor_blocked(
                "Night 1",
                "Event checks are blind (5 consecutive).",
                context={
                    "event_name": "Night 1",
                    "event_id": "event-1",
                    "blocked": True,
                    "challenge": False,
                    "signal": "none",
                    "consecutive": 5,
                },
                auto_fix_planned="browser_recycle_now",
            )
            payload = mock_send.call_args[1]
            embed = payload["embeds"][0]
            assert "**What happened**" in embed["description"]
            assert "**What monitor is doing**" in embed["description"]
            assert "**What you should do**" in embed["description"]
            assert "Automatic fix in progress: browser recycle now" in embed["description"]
            assert "**Technical**" in embed["description"]
            assert "alert_code=monitor_outage" in embed["description"]
            assert payload.get("content", "") == ""

    def test_auto_fix_action_maps_to_plain_english_and_includes_technical(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_auto_fix_action(
                action="browser_recycled",
                reason="blind/outage threshold reached",
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert "browser context was recycled" in embed["description"].lower()
            assert "monitor will retry checks" in embed["description"].lower()
            assert "alert_code=auto_fix_action" in embed["description"]
            assert "action=browser_recycled" in embed["description"]

    def test_critical_attention_includes_commands_and_ping(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_critical_attention(
                "Auto re-login failed repeatedly and is now paused.",
                next_steps=[
                    "scripts/monitorctl.sh reauth",
                    "python3 monitor.py --bootstrap-session --config config.yaml",
                ],
            )
            payload = mock_send.call_args[1]
            embed = payload["embeds"][0]
            assert "Run these commands now" in embed["description"]
            assert "`scripts/monitorctl.sh reauth`" in embed["description"]
            assert "`python3 monitor.py --bootstrap-session --config config.yaml`" in embed["description"]
            assert payload.get("content", "").startswith("<@")

    def test_non_manual_error_does_not_ping(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_error("Transient network reset while probing event page.")
            payload = mock_send.call_args[1]
            embed = payload["embeds"][0]
            assert "No action needed right now" in embed["description"]
            assert "alert_code=monitor_error" in embed["description"]
            assert payload.get("content", "") == ""

    def test_ticket_available_stays_english_only(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2026-07-28",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$200.10 - $200.10",
                section_summary="LOGE20",
                reason="signature_changed",
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert "**Technical**" not in embed["description"]
