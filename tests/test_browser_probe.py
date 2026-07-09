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
        title: str = "",
        status: int = 200,
        selector_counts=None,
        network_payloads=None,
        html_sequence: list[str] | None = None,
        body_text_sequence: list[str] | None = None,
        final_url: str | None = None,
    ):
        self._html = html
        self._body_text = body_text if body_text is not None else html
        self._html_sequence = html_sequence or [self._html]
        self._body_text_sequence = body_text_sequence or [self._body_text]
        self._stage = 0
        self._title = title
        self._status = status
        self._selector_counts = selector_counts or {}
        self._network_payloads = network_payloads or []
        self._listeners = []
        self._final_url = final_url
        self.url = "about:blank"
        self.reload_calls = 0
        self.goto_calls: list[str] = []
        self.timeout_calls: list[int] = []
        self.closed = False

    def on(self, event: str, callback):
        if event == "response":
            self._listeners.append(callback)

    def remove_listener(self, event: str, callback):
        if event == "response" and callback in self._listeners:
            self._listeners.remove(callback)

    def goto(self, *args, **_kwargs):
        if args:
            requested_url = str(args[0])
            self.goto_calls.append(requested_url)
            if self._final_url and "my-account" in requested_url:
                self.url = self._final_url
            else:
                self.url = requested_url
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

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_timeout(self, _ms: int):
        self.timeout_calls.append(_ms)
        return

    def wait_for_load_state(self, *_args, **_kwargs):
        max_stage = max(len(self._body_text_sequence), len(self._html_sequence)) - 1
        self._stage = min(self._stage + 1, max_stage)
        return

    def content(self) -> str:
        return self._html_sequence[min(self._stage, len(self._html_sequence) - 1)]

    def locator(self, selector: str):
        if selector == "body":
            text = self._body_text_sequence[min(self._stage, len(self._body_text_sequence) - 1)]
            return _FakeLocator(1, text)
        return _FakeLocator(self._selector_counts.get(selector, 0))

    def title(self) -> str:
        return self._title

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.pages = []
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        if self._page not in self.pages:
            self.pages.append(self._page)
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

    def test_browsing_activity_paused_screen_detected_as_block(self):
        # DataDome "soft block" interstitial — no captcha widget, just a pause page.
        page = _FakePage(
            html=(
                "<html><body>Your Browsing Activity Has Been Paused. "
                "We've detected unusual behavior on either your network or your browser.</body></html>"
            ),
            body_text=(
                "Your Browsing Activity Has Been Paused "
                "We've detected unusual behavior on either your network or your browser. "
                "Sign in to your account if you haven't already"
            ),
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


class TestBrowserProbeSessionHealth:
    def test_403_with_late_pause_text_is_challenge_not_logout(self):
        page = _FakePage(
            html="<html><body>Forbidden</body></html>",
            body_text="Forbidden",
            html_sequence=[
                "<html><body>Forbidden</body></html>",
                "<html><body>Your Browsing Activity Has Been Paused</body></html>",
            ],
            body_text_sequence=[
                "Forbidden",
                "Your Browsing Activity Has Been Paused. We've detected unusual behavior.",
            ],
            status=403,
        )
        probe = _probe_for_page(page)

        result = probe.check_session_health(warm_navigation=False)

        assert result["reason"] == "challenge_detected"
        assert result["challenge"] is True
        assert result["definitive_logged_out"] is False
        # The health-check tab is always closed afterward (whatever the outcome) so
        # it never lingers as a second visible tab alongside the event tab.
        assert page.is_closed() is True

    def test_bare_403_is_non_definitive_block(self):
        page = _FakePage(html="<html><body>Forbidden</body></html>", body_text="Forbidden", status=403)
        probe = _probe_for_page(page)

        result = probe.check_session_health(warm_navigation=False)

        assert result["reason"] == "http_403"
        assert result["challenge"] is False
        assert result["definitive_logged_out"] is False

    def test_login_redirect_is_definitive_logout(self):
        page = _FakePage(
            html="<html><body>Sign in</body></html>",
            status=200,
            final_url="https://auth.ticketmaster.com/signin",
        )
        probe = _probe_for_page(page)

        result = probe.check_session_health(warm_navigation=False)

        assert result["reason"] == "login_redirect"
        assert result["definitive_logged_out"] is True

    def test_login_title_is_definitive_logout(self):
        page = _FakePage(
            html="<html><body>Ticketmaster</body></html>",
            title="Ticketmaster Sign In",
            status=200,
        )
        probe = _probe_for_page(page)

        result = probe.check_session_health(warm_navigation=False)

        assert result["reason"] == "login_page_title"
        assert result["definitive_logged_out"] is True

    def test_rendered_password_form_is_definitive_logout(self):
        page = _FakePage(
            html="<html><body>Sign in to your account</body></html>",
            body_text="Sign in to your account",
            status=200,
            selector_counts={"input[type='password'], input[name='password'], input#password": 1},
        )
        probe = _probe_for_page(page)

        result = probe.check_session_health(warm_navigation=False)

        assert result["reason"] == "login_page_content"
        assert result["definitive_logged_out"] is True

    def test_session_health_closes_tab_after_each_check(self):
        """Each check opens its own tab and closes it afterward — never leaves a
        second tab sitting alongside the event tab between checks."""
        page = _FakePage(html="<html><body>Account</body></html>", body_text="Account", status=200)
        context = _FakeContext(page)
        probe = BrowserProbe(storage_state_path="unused.json", headless=True, navigation_timeout_seconds=20)
        probe._started = True
        probe._context = context

        first = probe.check_session_health(warm_navigation=False)
        assert page.is_closed() is True
        assert probe._health_page is None
        second = probe.check_session_health(warm_navigation=False)

        assert first["healthy"] is True
        assert second["healthy"] is True
        # A closed tab can't be reused, so each check opens a fresh one.
        assert context.new_page_calls == 2
        assert page.is_closed() is True
        # No lingering homepage navigation — the tab is gone, not parked.
        assert BrowserProbe.WARMUP_URL not in page.goto_calls


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

    def test_persistent_profile_falls_back_to_chromium_when_chrome_not_found(
        self, monkeypatch, tmp_path
    ):
        """If Chrome channel is not installed, start() should retry with bundled Chromium."""
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}

        class _RuntimeWithMissingChrome(_FakeRuntime):
            def _launch_persistent_context(self, user_data_dir: str, **kwargs):
                if kwargs.get("channel") == "chrome":
                    raise Exception(
                        "browsertype.launch_persistent_context: "
                        "Chromium distribution 'chrome' not found at "
                        r"C:\Users\User\AppData\Local\Google\Chrome\Application\chrome.exe"
                    )
                return super()._launch_persistent_context(user_data_dir, **kwargs)

        class _StarterWithMissingChrome:
            def start(self):
                record["started"] = True
                return _RuntimeWithMissingChrome(record)

        monkeypatch.setattr(playwright_sync, "sync_playwright", _StarterWithMissingChrome)
        profile_dir = tmp_path / "profile"

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="persistent_profile",
            user_data_dir=str(profile_dir),
            channel="chrome",
            navigation_timeout_seconds=20,
        )
        probe.start()  # should not raise

        assert "channel" not in record.get("launch_kwargs", {}), (
            "fallback launch should not pass a channel"
        )
        probe.close()

    def test_storage_mode_falls_back_to_chromium_when_chrome_not_found(
        self, monkeypatch, tmp_path
    ):
        """If Chrome channel is not installed, storage_state mode should also fall back."""
        playwright_sync = pytest.importorskip("playwright.sync_api")
        record: dict = {}

        class _RuntimeWithMissingChrome(_FakeRuntime):
            def _launch(self, **kwargs):
                if kwargs.get("channel") == "chrome":
                    raise Exception(
                        "Chromium distribution 'chrome' not found at chrome.exe"
                    )
                return super()._launch(**kwargs)

        class _StarterWithMissingChrome:
            def start(self):
                record["started"] = True
                return _RuntimeWithMissingChrome(record)

        monkeypatch.setattr(playwright_sync, "sync_playwright", _StarterWithMissingChrome)
        storage_state = tmp_path / "tm_storage_state.json"
        storage_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

        probe = BrowserProbe(
            storage_state_path=str(storage_state),
            session_mode="storage_state",
            channel="chrome",
            navigation_timeout_seconds=20,
        )
        probe.start()  # should not raise

        assert "channel" not in record.get("launch_kwargs", {}), (
            "fallback launch should not pass a channel"
        )
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

    def test_persistent_mode_reuses_tab_and_reloads(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[{"offers": [{"available": True, "quantity": 1, "price": 99.0}]}],
        )
        context = _FakePersistentContext()
        context.new_page = lambda: page  # one page, reused across checks

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="persistent_profile",
            reuse_event_tabs=True,
            navigation_timeout_seconds=20,
        )
        probe._started = True
        probe._context = context

        first = probe.check_event("event-1", "https://ticketmaster.com/event/1")
        second = probe.check_event("event-1", "https://ticketmaster.com/event/1")

        assert first.available is True
        assert second.available is True
        # Second check reused the same tab via reload() instead of a fresh navigation.
        assert page.reload_calls >= 1

    def test_single_event_page_navigates_same_page_across_event_ids(self):
        page = _FakePage(
            html="<html><body>Event</body></html>",
            status=200,
            network_payloads=[{"offers": [{"available": True, "quantity": 1, "price": 99.0}]}],
        )
        context = _FakePersistentContext()
        new_page_calls = 0

        def make_page():
            nonlocal new_page_calls
            new_page_calls += 1
            return page

        context.new_page = make_page

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="persistent_profile",
            reuse_event_tabs=True,
            single_event_page=True,
            navigation_timeout_seconds=20,
            event_dwell_min_seconds=3,
            event_dwell_max_seconds=8,
            homepage_warmup_interval_seconds=0,
        )
        probe._started = True
        probe._context = context

        first = probe.check_event("event-1", "https://ticketmaster.com/event/1")
        second = probe.check_event("event-2", "https://ticketmaster.com/event/2")

        assert first.available is True
        assert second.available is True
        assert new_page_calls == 1
        assert "https://ticketmaster.com/event/1" in page.goto_calls
        assert "https://ticketmaster.com/event/2" in page.goto_calls
        assert page.reload_calls == 0
        assert any(3000 <= ms <= 8000 for ms in page.timeout_calls)

    def test_persistent_mode_without_reuse_opens_fresh_pages(self):
        pages: list = []

        def make_page():
            p = _FakePage(html="<html><body>Event</body></html>", status=200)
            pages.append(p)
            return p

        context = _FakePersistentContext()
        context.new_page = make_page

        probe = BrowserProbe(
            storage_state_path="unused.json",
            session_mode="persistent_profile",
            reuse_event_tabs=False,
            navigation_timeout_seconds=20,
        )
        probe._started = True
        probe._context = context

        probe.check_event("event-1", "https://ticketmaster.com/event/1")
        probe.check_event("event-1", "https://ticketmaster.com/event/1")

        assert len(pages) == 2
        assert all(p.reload_calls == 0 for p in pages)

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


class TestStealthAndChannel:
    def _probe(self, **over):
        kwargs = dict(storage_state_path="secrets/x.json", session_mode="persistent_profile")
        kwargs.update(over)
        return BrowserProbe(**kwargs)

    def test_launch_kwargs_includes_channel_when_set(self):
        kw = BrowserProbe._launch_kwargs(headless=True, channel="chrome")
        assert kw["channel"] == "chrome"
        assert "--disable-blink-features=AutomationControlled" in kw["args"]

    def test_launch_kwargs_omits_channel_when_none(self):
        kw = BrowserProbe._launch_kwargs(headless=True, channel=None)
        assert "channel" not in kw

    def test_context_options_when_stealth_on(self):
        probe = self._probe(stealth_enabled=True, locale="en-US", timezone_id="America/New_York")
        opts = probe._context_options()
        assert opts["locale"] == "en-US"
        assert opts["timezone_id"] == "America/New_York"
        assert "viewport" in opts

    def test_context_options_empty_when_stealth_off(self):
        probe = self._probe(stealth_enabled=False)
        assert probe._context_options() == {}

    def test_context_options_skipped_for_cdp(self):
        probe = self._probe(session_mode="cdp_attach", stealth_enabled=True)
        assert probe._context_options() == {}


class _CookieContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


class TestReadAkamaiState:
    @staticmethod
    def _probe_with_cookies(cookies):
        probe = BrowserProbe.__new__(BrowserProbe)
        probe._context = _CookieContext(cookies)
        return probe

    def test_trusted_cookie(self):
        probe = self._probe_with_cookies([{"name": "_abck", "value": "hash~0~-1~-1~-1~abc"}])
        state = probe.read_akamai_state()
        assert state == {"abck_present": True, "abck_trusted": True, "abck_flagged": False}

    def test_flagged_cookie(self):
        probe = self._probe_with_cookies([{"name": "_abck", "value": "hash~-1~-1~-1~abc"}])
        state = probe.read_akamai_state()
        assert state["abck_present"] is True
        assert state["abck_flagged"] is True
        assert state["abck_trusted"] is False

    def test_missing_cookie(self):
        probe = self._probe_with_cookies([{"name": "datadome", "value": "x"}])
        assert probe.read_akamai_state() == {
            "abck_present": False,
            "abck_trusted": False,
            "abck_flagged": False,
        }

    def test_no_context_is_safe(self):
        probe = BrowserProbe.__new__(BrowserProbe)
        probe._context = None
        assert probe.read_akamai_state()["abck_present"] is False


def test_launch_kwargs_bounds_launch_time():
    kwargs = BrowserProbe._launch_kwargs(headless=False, channel="chrome")
    assert kwargs["timeout"] == 60_000


def test_trim_profile_caches_removes_cache_dirs_only(tmp_path):
    from src.browser_probe import trim_profile_caches

    profile = tmp_path / "profile"
    (profile / "Default" / "Cache").mkdir(parents=True)
    (profile / "Default" / "Cache" / "blob").write_bytes(b"x" * 2048)
    (profile / "Default" / "Code Cache").mkdir()
    (profile / "Default" / "Service Worker" / "CacheStorage").mkdir(parents=True)
    (profile / "GrShaderCache").mkdir()
    # Things that must survive: cookies/login/fingerprint state.
    (profile / "Default" / "Cookies").write_bytes(b"keep")
    (profile / "Default" / "Local Storage").mkdir()

    trim_profile_caches(str(profile))

    assert not (profile / "Default" / "Cache").exists()
    assert not (profile / "Default" / "Code Cache").exists()
    assert not (profile / "Default" / "Service Worker" / "CacheStorage").exists()
    assert not (profile / "GrShaderCache").exists()
    assert (profile / "Default" / "Cookies").exists()
    assert (profile / "Default" / "Local Storage").exists()


def test_trim_profile_caches_missing_dir_is_noop(tmp_path):
    from src.browser_probe import trim_profile_caches

    assert trim_profile_caches(str(tmp_path / "nope")) == 0


class TestChallengePatternTightening:
    """The bare 'datadome' HTML token appears in the vendor's ordinary JS tag on
    normal pages — it must only read as a challenge on a content-less shell."""

    def _probe(self):
        return BrowserProbe(storage_state_path="secrets/none.json")

    def test_normal_page_with_datadome_tag_is_not_challenge(self):
        probe = self._probe()
        body = ("Example Artist The Deadbeat Tour tickets. " * 10).lower()
        html = '<html><script src="https://js.datadome.co/tags.js"></script><body>x</body></html>'

        assert probe._detect_challenge_pattern(body, html.lower(), "example artist tickets") is None

    def test_datadome_with_minimal_body_is_challenge(self):
        probe = self._probe()
        html = '<html><script src="https://js.datadome.co/tags.js"></script><body></body></html>'

        assert (
            probe._detect_challenge_pattern("", html.lower(), "")
            == "html:datadome+minimal_body"
        )

    def test_captcha_delivery_iframe_is_always_challenge(self):
        probe = self._probe()
        body = ("Lots of visible page text here. " * 20).lower()
        html = '<html><iframe src="https://geo.captcha-delivery.com/captcha/"></iframe></html>'

        assert (
            probe._detect_challenge_pattern(body, html.lower(), "")
            == "html:captcha-delivery.com"
        )

    def test_body_pattern_reports_source(self):
        probe = self._probe()
        assert (
            probe._detect_challenge_pattern("please verify you are human", "<html></html>", "")
            == "body:verify you are human"
        )

    def test_challenge_pattern_lands_in_raw_indicators(self):
        page = _FakePage(
            html="<html><body>Please verify you are human</body></html>",
            status=200,
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.challenge_detected is True
        assert result.raw_indicators["challenge_pattern"] == "body:verify you are human"

    def test_clean_page_has_no_challenge_pattern(self):
        page = _FakePage(html="<html><body>Sold out</body></html>", status=200)
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        assert result.raw_indicators["challenge_pattern"] is None


class TestAvailabilitySourceDiagnostics:
    def test_network_availability_records_source_paths(self):
        page = _FakePage(
            html="<html><body>Event page</body></html>",
            status=200,
            network_payloads=[{"offers": [{"available": True, "quantity": 2, "price": 119.6}]}],
        )
        probe = _probe_for_page(page)
        result = probe.check_event("event-1", "http://event")

        sources = result.raw_indicators["availability_sources"]
        assert sources, "availability hits must name their JSON paths"
        assert any("available" in s or "quantity" in s for s in sources)

    def test_extract_snapshot_returns_sources(self):
        probe = BrowserProbe(storage_state_path="secrets/none.json")
        payload = {"offers": [{"available": True, "quantity": 3, "section": "LOGE"}]}

        count, signals, _prices, _sections, _groups, sources = probe._extract_network_snapshot(payload)

        assert count > 0
        assert "offers.available" in sources
        assert "offers.quantity" in sources
