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
            notifier.send_status_change("Test", "2030-01-01", "http://test", "offsale", "onsale")
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_BLUE

    def test_price_range_appeared_uses_blue(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_price_range_appeared("Test", "2030-01-01", "http://test", 50.0, 150.0)
            embed = mock_send.call_args[1]["embeds"][0]
            assert embed["color"] == COLOR_BLUE

    def test_sold_out_again_uses_red(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_sold_out_again("Test", "2030-01-01", "http://test")
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
                event_date="2030-01-01",
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
                event_date="2030-01-01",
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
                event_date="2030-01-01",
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
            notifier.send_status_change("Test", "2030-01-01", "http://test", "offsale", "onsale")
            content = mock_send.call_args[1].get("content", "")
            assert "<@" in content

    def test_offsale_no_mention(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_status_change("Test", "2030-01-01", "http://test", "onsale", "offsale")
            content = mock_send.call_args[1].get("content", "")
            assert content == ""


class TestTicketAvailableMention:
    def test_ticket_available_mention_can_be_disabled(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2030-01-01",
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
                event_date="2030-01-01",
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
            assert content.startswith("🟢 BINGO")
            assert "BINGO" in content
            assert content.endswith("<@123456789>")

    def test_ticket_available_multiple_bingo_configs_names_match(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="123456789")
        prefs = [
            TicketPreferences(name="LOGE pairs", min_tickets=2, max_price_per_ticket=200.0, preferred_sections=["LOGE"]),
            TicketPreferences(name="Budget triples", min_tickets=3, max_price_per_ticket=125.0),
        ]
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test Event",
                event_date="2030-01-01",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$120.00 - $120.00",
                section_summary="BALCONY301",
                reason="signature_changed",
                listing_groups=[{"section": "BALCONY301", "row": "6", "price": 120.0, "count": 3}],
                mention=True,
                preferences=prefs,
            )
            content = mock_send.call_args[1].get("content", "")
            embed = mock_send.call_args[1]["embeds"][0]
            assert content.startswith("🟢 BINGO — Budget triples")
            assert "Budget triples" in embed["title"]
            assert "**Best match:**" in embed["description"]

    def test_ticket_available_includes_listing_summary(self):
        notifier = DiscordNotifier(webhook_url="https://test")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_ticket_available(
                event_name="Test",
                event_date="2030-01-01",
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
                event_date="2030-01-01",
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
                event_date="2030-01-01",
                event_url="http://test",
                signal_type="dom+network",
                signal_confidence=0.95,
                price_summary="$200.10 - $200.10",
                section_summary="LOGE20",
                reason="signature_changed",
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert "**Technical**" not in embed["description"]


class TestOperationalLogOnly:
    def test_operational_messages_are_log_only_when_disabled(self):
        notifier = DiscordNotifier(webhook_url="https://test", operational_to_discord=False)
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            assert notifier.send_monitor_blocked("Night 1", "blind") is True
            assert notifier.send_monitor_recovered("Night 1", "back") is True
            assert notifier.send_auto_fix_action(action="browser_recycled", reason="x") is True
            assert notifier.send_error("transient blip") is True
            mock_send.assert_not_called()

    def test_manual_required_error_still_posts_when_operational_disabled(self):
        notifier = DiscordNotifier(webhook_url="https://test", operational_to_discord=False)
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_error("probe reload failed", manual_required=True, next_steps=["x"])
            mock_send.assert_called_once()

    def test_critical_attention_posts_even_when_operational_disabled(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="1", operational_to_discord=False)
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_critical_attention("manual login needed", next_steps=["scripts/monitorctl.sh reauth"])
            mock_send.assert_called_once()

    def test_critical_attention_copy_is_plain_no_technical(self):
        notifier = DiscordNotifier(webhook_url="https://test", ping_user_id="1")
        with patch.object(notifier, "_send", return_value=True) as mock_send:
            notifier.send_critical_attention(
                "Auto re-login keeps failing.",
                context={"degraded_for": "16 min"},
                next_steps=["scripts/monitorctl.sh reauth"],
            )
            embed = mock_send.call_args[1]["embeds"][0]
            assert "Action needed" in embed["title"]
            assert "**Technical**" not in embed["description"]
            assert "alert_code" not in embed["description"]
            assert "16 min" in embed["description"]


class TestHistorySeenCount:
    def test_repeat_listing_updates_seen_count(self, tmp_path, monkeypatch):
        import json as _json
        import src.notifier as notifier_mod
        hist = tmp_path / "history.json"
        monkeypatch.setattr(notifier_mod, "HISTORY_FILE", str(hist))

        groups = [{"section": "LOGE20", "row": "5", "price": 150.0, "count": 2}]
        for _ in range(3):
            DiscordNotifier._write_history_entry(
                event_name="Test", event_date="2030-01-01",
                event_url="https://www.ticketmaster.com/event/ABC123",
                all_groups=groups, is_bingo=True, label="BINGO!",
            )
        data = _json.loads(hist.read_text())
        assert len(data) == 1
        assert data[0]["seen_count"] == 3
        assert data[0]["first_seen"] and data[0]["last_seen"]

    def test_new_listing_creates_new_row(self, tmp_path, monkeypatch):
        import json as _json
        import src.notifier as notifier_mod
        hist = tmp_path / "history.json"
        monkeypatch.setattr(notifier_mod, "HISTORY_FILE", str(hist))

        DiscordNotifier._write_history_entry(
            event_name="Test", event_date="2030-01-01",
            event_url="https://www.ticketmaster.com/event/ABC123",
            all_groups=[{"section": "LOGE20", "row": "5", "price": 150.0, "count": 2}],
            is_bingo=True, label="BINGO!",
        )
        DiscordNotifier._write_history_entry(
            event_name="Test", event_date="2030-01-01",
            event_url="https://www.ticketmaster.com/event/ABC123",
            all_groups=[{"section": "FLOOR1", "row": "A", "price": 175.0, "count": 2}],
            is_bingo=True, label="BINGO!",
        )
        data = _json.loads(hist.read_text())
        assert len(data) == 2
        assert all(e["seen_count"] == 1 for e in data)

    def test_reappearing_listing_collapses_not_duplicates(self, tmp_path, monkeypatch):
        import json as _json
        import src.notifier as notifier_mod
        hist = tmp_path / "history.json"
        monkeypatch.setattr(notifier_mod, "HISTORY_FILE", str(hist))

        a = [{"section": "LOGE20", "row": "5", "price": 150.0, "count": 2}]
        b = [{"section": "FLOOR1", "row": "A", "price": 175.0, "count": 2}]
        # A, then B, then A again — the SECOND A must collapse into the first row,
        # not create a third (it isn't the immediately-previous entry).
        for groups in (a, b, a):
            DiscordNotifier._write_history_entry(
                event_name="Test", event_date="2030-01-01",
                event_url="https://www.ticketmaster.com/event/ABC123",
                all_groups=groups, is_bingo=True, label="BINGO!",
            )
        data = _json.loads(hist.read_text())
        assert len(data) == 2  # one row for A, one for B
        a_row = next(e for e in data if e["listings"][0]["section"] == "LOGE20")
        assert a_row["seen_count"] == 2


class TestNtfyNotifier:
    """Verify the ntfy.sh push channel and its fan-out from ticket alerts."""

    def test_send_ticket_posts_json_to_base_url_with_action(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["secret-topic"], server="https://ntfy.sh")
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ok = ntfy.send_ticket(
                title="BINGO: Example Artist",
                message="LOGE Row A - 4 @ $200.00 ea",
                event_url="https://www.ticketmaster.com/event/ABC",
                is_bingo=True,
            )
        assert ok is True
        args, kwargs = mock_post.call_args
        assert args[0] == "https://ntfy.sh"  # base URL, topic in JSON body
        body = kwargs["json"]
        assert body["topic"] == "secret-topic"
        assert body["priority"] == 5  # bingo escalates to urgent/max
        assert body["click"] == "https://www.ticketmaster.com/event/ABC"
        assert body["actions"][0] == {
            "action": "view",
            "label": "🌐 Open in Safari",
            "url": "https://www.ticketmaster.com/event/ABC",
            "clear": False,
        }

    def test_app_deep_link_drives_body_tap_click(self):
        from src.notifier import NtfyNotifier
        onelink = ("https://ticketmaster.onelink.me/7u25/edpUS"
                   "?deep_link_value={url_encoded}&af_force_deeplink=true")
        ntfy = NtfyNotifier(topics=["t"], app_deep_link=onelink)
        event_url = "https://www.ticketmaster.com/foo/event/ABC123"
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ntfy.send_ticket(title="BINGO", message="m",
                             event_url=event_url, is_bingo=True)
        body = mock_post.call_args[1]["json"]
        # Body tap (click) carries the OneLink with the percent-encoded event URL.
        assert body["click"].startswith("https://ticketmaster.onelink.me/7u25/edpUS")
        assert "https%3A%2F%2Fwww.ticketmaster.com%2Ffoo%2Fevent%2FABC123" in body["click"]
        # The single button remains the plain event URL (reliable fallback).
        assert len(body["actions"]) == 1
        assert body["actions"][0]["label"] == "🌐 Open in Safari"
        assert body["actions"][0]["url"] == event_url

    def test_no_deep_link_click_is_plain_event_url(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])  # no app_deep_link
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ntfy.send_ticket(title="t", message="m",
                             event_url="https://x/event/ABC", is_bingo=True)
        body = mock_post.call_args[1]["json"]
        assert body["click"] == "https://x/event/ABC"
        assert len(body["actions"]) == 1
        assert body["actions"][0]["label"] == "🌐 Open in Safari"

    def test_non_bingo_uses_configured_priority(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"], priority="high")
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ntfy.send_ticket(title="t", message="m", event_url="u", is_bingo=False)
        assert mock_post.call_args[1]["json"]["priority"] == 4  # high

    def test_send_test_has_no_click_or_actions(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ntfy.send_test()
        body = mock_post.call_args[1]["json"]
        assert "actions" not in body
        assert "click" not in body

    def test_disabled_when_no_topics_is_noop(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=[])
        with patch.object(ntfy.session, "post") as mock_post:
            assert ntfy.send_ticket(title="t", message="m", event_url="u", is_bingo=True) is True
            assert ntfy.send_test() is True
            mock_post.assert_not_called()

    def test_explicitly_disabled_is_noop(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"], enabled=False)
        with patch.object(ntfy.session, "post") as mock_post:
            assert ntfy.send_ticket(title="t", message="m", event_url="u", is_bingo=True) is True
            mock_post.assert_not_called()

    def test_posts_to_every_topic(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["a", "b"])
        with patch.object(ntfy.session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ntfy.send_test()
        assert mock_post.call_count == 2

    def test_ticket_alert_fans_out_to_ntfy(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])
        notifier = DiscordNotifier(webhook_url="https://test", ntfy=ntfy)
        with patch.object(notifier, "_send", return_value=True), \
             patch.object(ntfy, "send_ticket", return_value=True) as mock_ntfy:
            notifier.send_ticket_available(
                event_name="Example Artist",
                event_date="2030-01-01",
                event_url="https://www.ticketmaster.com/event/ABC",
                signal_type="synthetic",
                signal_confidence=1.0,
                price_summary=None,
                section_summary=None,
                reason="manual_test",
                listing_groups=[{"section": "LOGE", "row": "A", "price": 200.0, "count": 4}],
            )
        mock_ntfy.assert_called_once()
        assert mock_ntfy.call_args[1]["event_url"] == "https://www.ticketmaster.com/event/ABC"

    def test_ntfy_failure_does_not_break_discord(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])
        notifier = DiscordNotifier(webhook_url="https://test", ntfy=ntfy)
        with patch.object(notifier, "_send", return_value=True), \
             patch.object(ntfy, "send_ticket", side_effect=RuntimeError("boom")):
            sent = notifier.send_ticket_available(
                event_name="E", event_date="2030-01-01",
                event_url="http://x", signal_type="synthetic",
                signal_confidence=1.0, price_summary=None,
                section_summary=None, reason="manual_test",
                listing_groups=[{"section": "A", "row": "1", "price": 10.0, "count": 1}],
            )
        assert sent is True  # discord path unaffected

    def test_no_ntfy_push_when_mention_false(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])
        notifier = DiscordNotifier(webhook_url="https://test", ntfy=ntfy)
        with patch.object(notifier, "_send", return_value=True) as mock_send, \
             patch.object(ntfy, "send_ticket", return_value=True) as mock_ntfy:
            notifier.send_ticket_available(
                event_name="E", event_date="2030-01-01",
                event_url="http://x", signal_type="dom",
                signal_confidence=1.0, price_summary=None,
                section_summary=None, reason="cooldown_elapsed",
                listing_groups=[{"section": "A", "row": "1", "price": 10.0, "count": 1}],
                mention=False,
            )
        mock_ntfy.assert_not_called()      # friends are NOT pushed off-burst
        mock_send.assert_called_once()     # Discord message still posts

    def test_ntfy_push_when_mention_true(self):
        from src.notifier import NtfyNotifier
        ntfy = NtfyNotifier(topics=["t"])
        notifier = DiscordNotifier(webhook_url="https://test", ntfy=ntfy)
        with patch.object(notifier, "_send", return_value=True), \
             patch.object(ntfy, "send_ticket", return_value=True) as mock_ntfy:
            notifier.send_ticket_available(
                event_name="E", event_date="2030-01-01",
                event_url="http://x", signal_type="dom",
                signal_confidence=1.0, price_summary=None,
                section_summary=None, reason="signature_changed",
                listing_groups=[{"section": "A", "row": "1", "price": 10.0, "count": 1}],
                mention=True,
            )
        mock_ntfy.assert_called_once()
