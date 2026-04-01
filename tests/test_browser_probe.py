"""Tests for BrowserProbe availability and blocking logic."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.browser_probe import BrowserProbe
from src.models import ProbeSignalType


class _FakeLocator:
    def __init__(self, count: int, text: str = ""):
        self._count = count
        self._text = text

    def count(self) -> int:
        return self._count

    def inner_text(self, timeout: int = 0) -> str:
        del timeout
        return self._text


class _FakeGotoResponse:
    def __init__(self, status: int):
        self.status = status


class _FakeNetworkResponse:
    def __init__(self, url: str, payload: dict):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._payload = payload

    def json(self):
        return self._payload


class _FakePage:
    def __init__(
        self,
        *,
        html: str,
        body_text: str | None = None,
        status: int = 200,
        selector_counts=None,
        network_payloads=None,
    ):
        self._html = html
        self._body_text = body_text if body_text is not None else html
        self._status = status
        self._selector_counts = selector_counts or {}
        self._network_payloads = network_payloads or []
        self._listeners = []
        self.url = "about:blank"
        self.reload_calls = 0

    def on(self, event: str, callback):
        if event == "response":
            self._listeners.append(callback)

    def remove_listener(self, event: str, callback):
        if event == "response" and callback in self._listeners:
            self._listeners.remove(callback)

    def goto(self, *args, **_kwargs):
        if args:
            self.url = str(args[0])
        for payload in self._network_payloads:
            response = _FakeNetworkResponse(
                "https://www.ticketmaster.com/api/inventory",
                payload,
            )
            for callback in self._listeners:
                callback(response)
        return _FakeGotoResponse(self._status)

    def reload(self, *_args, **_kwargs):
        self.reload_calls += 1
        return self.goto(self.url)

    def wait_for_timeout(self, _ms: int):
        return

    def content(self) -> str:
        return self._html

    def locator(self, selector: str):
        if selector == "body":
            return _FakeLocator(1, self._body_text)
        return _FakeLocator(self._selector_counts.get(selector, 0))

    def close(self):
        return


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _FakePersistentPage:
    def goto(self, *_args, **_kwargs):
        return SimpleNamespace(status=200)


class _FakePersistentContext:
    def __init__(self):
        self.default_timeout = None
        self.closed = False
        self.pages = []

    def set_default_timeout(self, timeout: int):
        self.default_timeout = timeout

    def new_page(self):
        page = _FakePersistentPage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeEphemeralContext(_FakePersistentContext):
    def __init__(self):
        super().__init__()
        self.storage_state_path = None


class _FakeBrowser:
    def __init__(self):
        self.closed = False
        self.context = _FakeEphemeralContext()

    def new_context(self, storage_state: str):
        self.context.storage_state_path = storage_state
        return self.context

    def close(self):
        self.closed = True


class _FakeRuntime:
    def __init__(self, record: dict):
        self.record = record
        self.stopped = False
        self.chromium = SimpleNamespace(
            launch=self._launch,
            launch_persistent_context=self._launch_persistent_context,
            connect_over_cdp=self._connect_over_cdp,
        )
        self.browser = _FakeBrowser()
        self.persistent_context = _FakePersistentContext()
        self.cdp_context = _FakePersistentContext()
        self.cdp_browser = SimpleNamespace(
            contexts=[self.cdp_context],
            new_context=lambda: self.cdp_context,
        )

    def _launch(self, **kwargs):
        self.record["launch_kwargs"] = kwargs
        return self.browser

    def _launch_persistent_context(self, user_data_dir: str, **kwargs):
        self.record["user_data_dir"] = user_data_dir
        self.record["launch_kwargs"] = kwargs
        return self.persistent_context

    def _connect_over_cdp(self, endpoint_url: str, timeout: int):
        self.record["cdp_endpoint_url"] = endpoint_url
        self.record["cdp_timeout"] = timeout
        return self.cdp_browser

    def stop(self):
        self.stopped = True


class _FakeSyncPlaywrightStarter:
    def __init__(self, record: dict):
        self.record = record
        self.runtime = _FakeRuntime(record)

    def start(self):
        self.record["started"] = True
        return self.runtime


class _FakeSyncPlaywrightContextManager:
    def __init__(self, record: dict):
        self.record = record
        self.runtime = _FakeRuntime(record)

    def __enter__(self):
        return self.runtime

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False


def _probe_for_page(page: _FakePage) -> BrowserProbe:
    probe = BrowserProbe(storage_state_path="unused.json", headless=True, navigation_timeout_seconds=20)
    probe._started = True
    probe._context = _FakeContext(page)
    return probe


class TestBrowserProbe:
    def test_dom_buy_button_alone_is_not_available(self):
        page = _FakePage(
            html="<html><button>Find Tickets</button></html>",
            status=200,
            selector_counts={"button:has-text('Find Tickets')": 1},
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is False
        assert result.blocked is False
        assert result.signal_type == ProbeSignalType.DOM

    def test_offer_card_signal_marks_available(self):
        page = _FakePage(
            html="<html><body><div data-testid='offer-card'>Offer</div></body></html>",
            status=200,
            selector_counts={"[data-testid='offer-card']": 1},
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.blocked is False
        assert result.signal_type == ProbeSignalType.DOM

    def test_network_inventory_signal_marks_available(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[{"offers": [{"available": True, "quantity": 2, "price": 119.6}]}],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.signal_type in {ProbeSignalType.NETWORK, ProbeSignalType.DOM_AND_NETWORK}
        assert result.price_summary is not None

    def test_blocked_status_detected(self):
        page = _FakePage(html="<html><body>Unauthorized</body></html>", status=401)
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.blocked is True
        assert result.available is False

    def test_challenge_page_detected(self):
        page = _FakePage(
            html="<html><body>Please verify you are human</body></html>",
            status=200,
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.challenge_detected is True
        assert result.blocked is True
        assert result.available is False

    def test_sold_out_text_is_not_available(self):
        page = _FakePage(
            html="<html><body>Sold out</body></html>",
            body_text="Sold out",
            status=200,
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is False
        assert result.blocked is False
        assert "sold_out_text" in result.raw_indicators["dom_signals"]

    def test_hidden_html_strings_do_not_count_as_visible_dom_signal(self):
        page = _FakePage(
            html="<html><script>var labels = {'tickets_available': 'tickets available'};</script></html>",
            body_text="No tickets currently available",
            status=200,
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is False
        assert "tickets_available_text" not in result.raw_indicators["dom_signals"]

    def test_hidden_html_sold_out_string_does_not_block_offer_card_signal(self):
        page = _FakePage(
            html=(
                "<html><script>var labels = {'sold_out': 'sold out'};</script>"
                "<body><div data-testid='offer-card'>Offer</div></body></html>"
            ),
            body_text="Tickets available now",
            status=200,
            selector_counts={"[data-testid='offer-card']": 1},
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert "sold_out_text" not in result.raw_indicators["dom_signals"]

    def test_price_summary_ignores_html_currency_noise(self):
        page = _FakePage(
            html="<html><body>$0.00 $25.00 random text</body></html>",
            status=200,
            network_payloads=[
                {"offers": [{"available": True, "section": "LOGE20", "listPrice": 200.10}]}
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.price_summary == "$200.10 - $200.10"

    def test_price_summary_ignores_non_listing_price_fields(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[
                {
                    "facets": [{"price": 25.00, "section": "META"}],
                    "_embedded": {"offer": [{"available": True, "section": "LOGE20", "listPrice": 200.10}]},
                }
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.price_summary == "$200.10 - $200.10"
        assert result.listing_summary == "LOGE20 / Row ? / $200.10 x1"

    def test_price_summary_none_when_no_offer_linked_prices(self):
        page = _FakePage(
            html="<html><body>$25.00</body></html>",
            status=200,
            network_payloads=[
                {
                    "offers": [{"available": True, "section": "LOGE20"}],
                    "facets": [{"price": 25.00}],
                }
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.price_summary is None
        assert result.listing_summary is None

    def test_listing_summary_groups_and_counts_quantities(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[
                {
                    "offers": [
                        {"available": True, "section": "loge20", "row": "14", "listPrice": 200.10, "quantity": 2},
                        {"available": True, "sectionName": "LOGE20", "rowName": "Row 14", "listPrice": 200.10, "quantity": 1},
                    ]
                },
                {
                    "offers": [
                        {"available": True, "section": "LOGE20", "row": "14", "listPrice": 200.10, "quantity": 1},
                    ]
                },
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.price_summary == "$200.10 - $200.10"
        assert result.listing_summary == "LOGE20 / Row 14 / $200.10 x3"
        assert result.raw_indicators["listing_groups"] == [
            {"section": "LOGE20", "row": "14", "price": 200.1, "count": 3}
        ]

    def test_listing_summary_prefers_quantity_fields(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[
                {
                    "quickpicks": [
                        {"available": True, "section": "LOGE20", "row": "14", "listPrice": 200.10, "availableTickets": 4},
                        {"available": True, "section": "LOGE20", "row": "15", "listPrice": 210.00, "totalAvailable": 2},
                    ]
                }
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.listing_summary == (
            "LOGE20 / Row 14 / $200.10 x4; LOGE20 / Row 15 / $210.00 x2"
        )

    def test_listing_summary_uses_sellable_quantities_for_resale_offer(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[
                {
                    "quickpicks": {"total": 1},
                    "_embedded": {
                        "offer": [
                            {
                                "available": True,
                                "section": "LOGE11",
                                "row": "4",
                                "listPrice": 458.15,
                                "sellableQuantities": [1, 3],
                                "seatFrom": "2",
                                "seatTo": "4",
                            }
                        ]
                    },
                }
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.price_summary == "$458.15 - $458.15"
        assert result.listing_summary == "LOGE11 / Row 4 / $458.15 x3"


class TestBrowserProbeSessionModes:
    def test_start_uses_persistent_profile_mode(self, monkeypatch, tmp_path):
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}
        monkeypatch.setattr(
            playwright_sync,
            "sync_playwright",
            lambda: _FakeSyncPlaywrightStarter(record),
        )
        profile_dir = tmp_path / "profile"

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="persistent_profile",
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            navigation_timeout_seconds=20,
        )
        probe.start()

        assert record.get("started") is True
        assert record.get("user_data_dir") == str(profile_dir)
        assert record.get("launch_kwargs", {}).get("headless") is False
        assert record.get("launch_kwargs", {}).get("channel") == "chrome"
        assert probe._context.default_timeout == 20000
        probe.close()

    def test_start_storage_mode_uses_storage_state_file(self, monkeypatch, tmp_path):
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}
        monkeypatch.setattr(
            playwright_sync,
            "sync_playwright",
            lambda: _FakeSyncPlaywrightStarter(record),
        )
        storage_state = tmp_path / "tm_storage_state.json"
        storage_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

        probe = BrowserProbe(
            storage_state_path=str(storage_state),
            session_mode="storage_state",
            headless=True,
            navigation_timeout_seconds=20,
        )
        probe.start()

        assert record.get("launch_kwargs", {}).get("headless") is True
        assert probe._browser.context.storage_state_path == str(storage_state)
        probe.close()

    def test_persistent_profile_bootstrap_uses_profile_dir(self, monkeypatch, tmp_path):
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}
        monkeypatch.setattr(
            playwright_sync,
            "sync_playwright",
            lambda: _FakeSyncPlaywrightContextManager(record),
        )
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        profile_dir = tmp_path / "profile"

        BrowserProbe.save_persistent_profile_interactive(
            event_url="https://www.ticketmaster.com/event/abc",
            user_data_dir=str(profile_dir),
            navigation_timeout_seconds=20,
            channel="chrome",
        )

        assert record.get("user_data_dir") == str(profile_dir)
        assert record.get("launch_kwargs", {}).get("headless") is False
        assert record.get("launch_kwargs", {}).get("channel") == "chrome"

    def test_cdp_bootstrap_reuses_existing_page(self, monkeypatch):
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}
        context_manager = _FakeSyncPlaywrightContextManager(record)

        class _TrackedPage:
            def __init__(self):
                self.url = "about:blank"
                self.goto_calls = 0
                self.last_url = ""

            def bring_to_front(self):
                return

            def goto(self, url, *args, **kwargs):
                del args, kwargs
                self.goto_calls += 1
                self.last_url = str(url)
                self.url = self.last_url
                return SimpleNamespace(status=200)

        existing_page = _TrackedPage()
        context_manager.runtime.cdp_context.pages = [existing_page]

        monkeypatch.setattr(playwright_sync, "sync_playwright", lambda: context_manager)
        monkeypatch.setattr("builtins.input", lambda _prompt: "")

        BrowserProbe.save_cdp_attach_interactive(
            event_url="https://www.ticketmaster.com/event/abc",
            cdp_endpoint_url="http://127.0.0.1:9222",
            navigation_timeout_seconds=20,
        )

        assert existing_page.goto_calls == 1
        assert existing_page.last_url == "https://www.ticketmaster.com/event/abc"
        assert len(context_manager.runtime.cdp_context.pages) == 1

    def test_start_uses_cdp_attach_mode(self, monkeypatch):
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}
        monkeypatch.setattr(
            playwright_sync,
            "sync_playwright",
            lambda: _FakeSyncPlaywrightStarter(record),
        )

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="cdp_attach",
            cdp_endpoint_url="http://127.0.0.1:9223",
            cdp_connect_timeout_seconds=7,
            navigation_timeout_seconds=20,
        )
        probe.start()

        assert record.get("cdp_endpoint_url") == "http://127.0.0.1:9223"
        assert record.get("cdp_timeout") == 7000
        assert probe.cdp_connected is True
        probe.close()

    def test_cdp_mode_reuses_same_tab_and_uses_reload(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[{"offers": [{"available": True, "quantity": 1, "price": 99.0}]}],
        )
        context = _FakePersistentContext()
        context.pages = [page]

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="cdp_attach",
            reuse_event_tabs=True,
            navigation_timeout_seconds=20,
        )
        probe._started = True
        probe._context = context

        first = probe.check_event("event-1", "https://ticketmaster.com/event/1")
        second = probe.check_event("event-1", "https://ticketmaster.com/event/1")

        assert first.available is True
        assert second.available is True
        assert page.reload_calls >= 1

    def test_listing_summary_reports_when_rows_are_omitted(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[
                {
                    "offers": [
                        {"available": True, "section": "LOGE01", "row": "1", "listPrice": 101.0},
                        {"available": True, "section": "LOGE02", "row": "1", "listPrice": 102.0},
                        {"available": True, "section": "LOGE03", "row": "1", "listPrice": 103.0},
                        {"available": True, "section": "LOGE04", "row": "1", "listPrice": 104.0},
                        {"available": True, "section": "LOGE05", "row": "1", "listPrice": 105.0},
                        {"available": True, "section": "LOGE06", "row": "1", "listPrice": 106.0},
                    ]
                }
            ],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.available is True
        assert result.listing_summary is not None
        assert result.listing_summary.endswith("(+1 more)")
