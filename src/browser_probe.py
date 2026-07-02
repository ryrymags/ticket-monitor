"""Playwright-based Ticketmaster event page probe."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import ProbeResult, ProbeSignalType

logger = logging.getLogger(__name__)

PLAYWRIGHT_IMPORT_ERROR = (
    "Playwright is not installed. Install dependencies and run: "
    "python -m playwright install chromium"
)

# Light stealth: hide the most obvious headless/automation tells. Runs before any
# page script on every navigation. Kept minimal on purpose — aggressive spoofing is
# itself detectable and not guaranteed to defeat Ticketmaster's bot wall.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""


class BrowserProbeError(Exception):
    """Raised when the browser probe cannot complete a check."""


def _is_channel_not_found_error(exc: BaseException) -> bool:
    """Return True if the exception indicates a Playwright browser channel (e.g. 'chrome')
    is not installed on this machine, so a Chromium fallback is appropriate."""
    msg = str(exc).lower()
    return "not found" in msg and ("distribution" in msg or "channel" in msg or "chrome" in msg)


@dataclass
class _NetworkSnapshot:
    availability_count: int
    signals: set[str]
    prices: list[float]
    sections: set[str]
    listing_groups: dict[tuple[str, str, str], dict[str, Any]]


class BrowserProbe:
    """Probe that uses a persisted browser session to inspect event availability."""

    # Human-like navigation: occasionally land on the TM homepage before the event page
    # so traffic looks like browsing (not a bot hitting a resale endpoint cold), which
    # also keeps the DataDome cookie warm.
    WARMUP_URL = "https://www.ticketmaster.com/"
    WARMUP_INTERVAL_SECONDS = 1800
    SINGLE_EVENT_PAGE_KEY = "__active_event_page__"

    CTA_SELECTORS = [
        "button:has-text('Find Tickets')",
        "button:has-text('Buy Tickets')",
        "a:has-text('Find Tickets')",
        "a:has-text('Buy Tickets')",
    ]
    OFFER_CARD_SELECTORS = [
        "[data-bdd='offer-card']",
        "[data-testid='offer-card']",
    ]
    RESALE_SELECTORS = [
        "text=Face Value Exchange",
        "text=Verified Resale",
        "text=Resale",
    ]
    CHALLENGE_PATTERNS = [
        "verify you are human",
        "are you human",
        "captcha",
        "attention required",
        "press & hold",
        "datadome",
        "cf-challenge",
        # DataDome "soft block" interstitial (no captcha, just a pause page):
        #   "Your Browsing Activity Has Been Paused —
        #    We've detected unusual behavior on either your network or your browser."
        "browsing activity has been paused",
        "detected unusual behavior",
        "unusual behavior on either your network",
        "unusual traffic",
    ]
    SOLD_OUT_PATTERNS = [
        "sold out",
        "no tickets available",
        "currently unavailable",
    ]
    NETWORK_KEYWORDS = (
        "inventory",
        "offers",
        "quickpicks",
        "availability",
        "facets",
        "resale",
    )

    def __init__(
        self,
        storage_state_path: str,
        session_mode: str = "storage_state",
        user_data_dir: str = "secrets/tm_profile",
        channel: str = "",
        cdp_endpoint_url: str = "http://127.0.0.1:9222",
        cdp_connect_timeout_seconds: int = 10,
        reuse_event_tabs: bool = True,
        single_event_page: bool = True,
        headless: bool = True,
        navigation_timeout_seconds: int = 20,
        stealth_enabled: bool = False,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        event_dwell_min_seconds: int = 3,
        event_dwell_max_seconds: int = 8,
        homepage_warmup_interval_seconds: int = WARMUP_INTERVAL_SECONDS,
    ):
        self.storage_state_path = storage_state_path
        self.session_mode = session_mode
        self.user_data_dir = user_data_dir
        self.channel = channel
        self.cdp_endpoint_url = cdp_endpoint_url
        self.cdp_connect_timeout_seconds = cdp_connect_timeout_seconds
        self.reuse_event_tabs = reuse_event_tabs
        self.single_event_page = single_event_page
        self.headless = headless
        self.navigation_timeout_seconds = navigation_timeout_seconds
        self.stealth_enabled = stealth_enabled
        self.locale = locale
        self.timezone_id = timezone_id
        self.event_dwell_min_seconds = event_dwell_min_seconds
        self.event_dwell_max_seconds = event_dwell_max_seconds
        self.homepage_warmup_interval_seconds = homepage_warmup_interval_seconds
        self._playwright = None
        self._browser = None
        self._context = None
        self._uses_persistent_context = False
        self._event_pages: dict[str, Any] = {}
        self._single_event_page_event_id: str | None = None
        self._health_page: Any | None = None
        self._last_warmup_at: dict[str, float] = {}
        self._cdp_connected = False
        self._started = False

    def start(self):
        """Start Playwright browser and context."""
        if self._started:
            return

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import guard
            raise BrowserProbeError(PLAYWRIGHT_IMPORT_ERROR) from exc

        try:
            self._playwright = sync_playwright().start()
            self._uses_persistent_context = self.session_mode == "persistent_profile"
            if self.session_mode == "cdp_attach":
                connect_timeout_ms = max(1000, int(self.cdp_connect_timeout_seconds * 1000))
                self._browser = self._playwright.chromium.connect_over_cdp(
                    self.cdp_endpoint_url,
                    timeout=connect_timeout_ms,
                )
                contexts = list(getattr(self._browser, "contexts", []))
                if contexts:
                    self._context = contexts[0]
                else:
                    # Fallback for runtimes that do not expose the default context.
                    self._context = self._browser.new_context()
                self._cdp_connected = True
            elif self._uses_persistent_context:
                launch_kwargs = self._launch_kwargs(headless=self.headless, channel=self.channel)
                launch_kwargs.update(self._context_options())
                if not self.user_data_dir:
                    raise BrowserProbeError(
                        "browser.user_data_dir is required when browser.session_mode is persistent_profile"
                    )
                os.makedirs(self.user_data_dir, exist_ok=True)
                try:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        self.user_data_dir,
                        **launch_kwargs,
                    )
                except Exception as launch_exc:
                    if self.channel and _is_channel_not_found_error(launch_exc):
                        logger.warning(
                            "Chrome channel %r not found — falling back to bundled Chromium. "
                            "Install Google Chrome for best Ticketmaster compatibility.",
                            self.channel,
                        )
                        fallback_kwargs = self._launch_kwargs(headless=self.headless, channel=None)
                        fallback_kwargs.update(self._context_options())
                        self._context = self._playwright.chromium.launch_persistent_context(
                            self.user_data_dir,
                            **fallback_kwargs,
                        )
                    else:
                        raise
                self._browser = None
            else:
                launch_kwargs = self._launch_kwargs(headless=self.headless, channel=self.channel)
                if not os.path.exists(self.storage_state_path):
                    raise BrowserProbeError(
                        f"Storage state file not found: {self.storage_state_path}. Run --bootstrap-session first."
                    )
                try:
                    self._browser = self._playwright.chromium.launch(**launch_kwargs)
                except Exception as launch_exc:
                    if self.channel and _is_channel_not_found_error(launch_exc):
                        logger.warning(
                            "Chrome channel %r not found — falling back to bundled Chromium. "
                            "Install Google Chrome for best Ticketmaster compatibility.",
                            self.channel,
                        )
                        fallback_kwargs = self._launch_kwargs(headless=self.headless, channel=None)
                        self._browser = self._playwright.chromium.launch(**fallback_kwargs)
                    else:
                        raise
                self._context = self._browser.new_context(
                    storage_state=self.storage_state_path,
                    **self._context_options(),
                )

            self._apply_stealth()
            self._context.set_default_timeout(self.navigation_timeout_seconds * 1000)
            self._close_stray_tabs()
            self._started = True
        except Exception as exc:
            self.close()
            raise BrowserProbeError(f"Failed to start browser probe: {exc}") from exc

    def _close_stray_tabs(self):
        """Persistent profiles can restore old tabs on launch; keep one (reused for an
        event) and close the rest so blank tabs don't accumulate across restarts. Never
        touches an external (cdp_attach) browser — those are the user's own tabs."""
        if self.session_mode == "cdp_attach" or self._context is None:
            return
        try:
            pages = list(getattr(self._context, "pages", []))
        except Exception:
            return
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                pass

    def close(self):
        """Close browser resources."""
        if self.session_mode == "cdp_attach":
            self._event_pages = {}
            self._single_event_page_event_id = None
            self._health_page = None
            self._context = None
            self._browser = None
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._playwright = None
                self._uses_persistent_context = False
                self._cdp_connected = False
                self._started = False
            return

        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            self._uses_persistent_context = False
            self._event_pages = {}
            self._single_event_page_event_id = None
            self._health_page = None

        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            self._cdp_connected = False

        try:
            if self._playwright is not None:
                self._playwright.stop()
        finally:
            self._playwright = None
            self._started = False

    # Bot/anti-block tokens to shed on a prolonged block. Login cookies are preserved.
    BLOCK_COOKIE_NAMES = ("datadome",)

    def clear_block_cookies(self) -> int:
        """Delete DataDome/bot tokens from the context while keeping login cookies, so a
        stuck block can get a fresh token on the next reload. Returns how many were removed."""
        if self._context is None:
            return 0
        try:
            cookies = self._context.cookies()
        except Exception:
            return 0
        keep = [
            c
            for c in cookies
            if not any(tok in str(c.get("name", "")).lower() for tok in self.BLOCK_COOKIE_NAMES)
        ]
        removed = len(cookies) - len(keep)
        if removed <= 0:
            return 0
        try:
            self._context.clear_cookies()
            if keep:
                self._context.add_cookies(keep)
        except Exception:
            return 0
        return removed

    def read_akamai_state(self) -> dict:
        """Read Akamai's `_abck` trust cookie without navigating (cheap cookie-jar read).

        The value's validation field is ``0`` once sensor.js has validated the session
        (trusted) and ``-1`` while it is unvalidated / flagged. This often flips to
        flagged *before* the visible "activity paused" screen, so it is a useful leading
        signal — and it is how we confirm recovery (trusted again) after a cooldown.
        Returns {abck_present, abck_trusted, abck_flagged}.
        """
        state = {"abck_present": False, "abck_trusted": False, "abck_flagged": False}
        if self._context is None:
            return state
        try:
            cookies = self._context.cookies()
        except Exception:
            return state
        for c in cookies:
            if str(c.get("name", "")).lower() != "_abck":
                continue
            state["abck_present"] = True
            parts = str(c.get("value", "")).split("~")
            flag = parts[1] if len(parts) > 1 else ""
            if flag == "0":
                state["abck_trusted"] = True
            elif flag == "-1":
                state["abck_flagged"] = True
            break
        return state

    @property
    def cdp_connected(self) -> bool:
        return bool(self._cdp_connected and self._started and self.session_mode == "cdp_attach")

    @staticmethod
    def _launch_kwargs(*, headless: bool, channel: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": headless,
            # Keep the OS sandbox ON: Playwright defaults it OFF (--no-sandbox), which
            # shows an infobar AND is a known automation tell. Also drop the
            # --enable-automation switch (the "controlled by test software" banner/flag).
            "chromium_sandbox": True,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--hide-crash-restore-bubble",
            ],
        }
        if channel:
            kwargs["channel"] = channel
        return kwargs

    def _context_options(self) -> dict[str, Any]:
        """Realistic context options to look like a normal returning browser.

        With real Chrome (channel=chrome) the User-Agent is already genuine, so we
        deliberately do NOT spoof it — a mismatched UA increases detection. Skipped
        entirely for cdp_attach (that's already a real external browser).
        """
        if not self.stealth_enabled or self.session_mode == "cdp_attach":
            return {}
        opts: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if self.locale:
            opts["locale"] = self.locale
        if self.timezone_id:
            opts["timezone_id"] = self.timezone_id
        return opts

    def _apply_stealth(self):
        """Harden the obvious automation tells via an init script on every page."""
        if not self.stealth_enabled or self.session_mode == "cdp_attach":
            return
        if self._context is None:
            return
        try:
            self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("stealth init script not applied: %s", exc)

    def check_session_health(
        self,
        url: str = "https://www.ticketmaster.com/my-account",
        *,
        warm_navigation: bool = True,
    ) -> dict:
        """Proactively check whether the browser session is still authenticated.

        Returns a dict with:
          healthy: bool
          reason: str  — "ok" | "http_401" | "http_403" | "challenge_detected"
                         | "login_redirect" | "login_page_title"
                         | "login_page_content"
          status: int | None  — final HTTP status code
          challenge: bool
          definitive_logged_out: bool
        """
        if not self._started:
            self.start()

        page = None
        try:
            page = self._get_or_create_health_page()
            timeout_ms = self.navigation_timeout_seconds * 1000
            if warm_navigation and self.session_mode != "cdp_attach":
                self._warm_session_health_navigation(page, timeout_ms)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            response_status: int | None = response.status if response is not None else None

            page.wait_for_timeout(random.randint(1500, 2500))
            html, body_text, page_title, challenge = self._session_health_page_state(page)
            if challenge:
                return {
                    "healthy": False,
                    "reason": "challenge_detected",
                    "status": response_status,
                    "challenge": True,
                    "definitive_logged_out": False,
                }

            if response_status in {401, 403}:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    try:
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass
                html, body_text, page_title, challenge = self._session_health_page_state(page)
                if challenge:
                    return {
                        "healthy": False,
                        "reason": "challenge_detected",
                        "status": response_status,
                        "challenge": True,
                        "definitive_logged_out": False,
                    }
                return {
                    "healthy": False,
                    "reason": f"http_{response_status}",
                    "status": response_status,
                    "challenge": False,
                    "definitive_logged_out": False,
                }

            # Check if we were redirected to a login page
            final_url = ""
            try:
                final_url = page.url.lower()
            except Exception:
                pass
            if any(pat in final_url for pat in ("signin", "login", "sign-in", "log-in")):
                return {
                    "healthy": False,
                    "reason": "login_redirect",
                    "status": response_status,
                    "challenge": False,
                    "definitive_logged_out": True,
                }
            if any(pat in page_title for pat in ("sign in", "log in", "login")):
                return {
                    "healthy": False,
                    "reason": "login_page_title",
                    "status": response_status,
                    "challenge": False,
                    "definitive_logged_out": True,
                }
            body_text_lower = body_text.lower()
            if self._has_password_input(page) or "sign in to your account" in body_text_lower:
                return {
                    "healthy": False,
                    "reason": "login_page_content",
                    "status": response_status,
                    "challenge": False,
                    "definitive_logged_out": True,
                }

            return {
                "healthy": True,
                "reason": "ok",
                "status": response_status,
                "challenge": False,
                "definitive_logged_out": False,
            }
        except Exception:
            raise
        finally:
            # Close rather than leave parked on the homepage: this tab is separate
            # from the event tab, so leaving it open means TWO Chrome tabs are always
            # visible even though only one is doing active monitoring. Closing it
            # keeps the window down to just the event tab between health checks;
            # _get_or_create_health_page() opens a fresh one next time it's due.
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            self._health_page = None

    def check_event(self, event_id: str, event_url: str) -> ProbeResult:
        """Check one event page for availability signals."""
        if not self._started:
            self.start()

        try:
            return self._check_event_impl(event_id=event_id, event_url=event_url, allow_retry=True)
        except Exception as exc:
            raise BrowserProbeError(f"Browser probe failed for {event_id}: {exc}") from exc

    def _check_event_impl(self, event_id: str, event_url: str, *, allow_retry: bool) -> ProbeResult:
        page: Any = None
        close_page = True
        response_handler = None

        try:
            if self.session_mode == "cdp_attach" or self.reuse_event_tabs:
                page, needs_navigation = self._get_or_create_event_page(
                    event_id=event_id,
                    event_url=event_url,
                )
                close_page = False
            else:
                page = self._context.new_page()
                needs_navigation = True

            network_snapshot = _NetworkSnapshot(
                availability_count=0,
                signals=set(),
                prices=[],
                sections=set(),
                listing_groups={},
            )
            def response_handler(response):
                self._capture_response(response, network_snapshot)
            page.on("response", response_handler)

            timeout_ms = self.navigation_timeout_seconds * 1000
            response_status: int | None = None
            # Occasionally browse the homepage first so traffic looks human; if we did,
            # the page is now on the homepage so we must goto (not reload) the event.
            warmed = self._maybe_warmup(page, event_id, needs_navigation, timeout_ms)
            if needs_navigation or warmed:
                response = page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
            else:
                try:
                    response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    response = page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)

            retry_after_seconds: int | None = None
            if response is not None:
                response_status = response.status
                if response_status == 429:
                    retry_after_seconds = self._parse_retry_after(response)

            # Jittered settle + occasional light scroll — avoids a robotic fixed rhythm.
            page.wait_for_timeout(self._event_dwell_timeout_ms())
            self._human_jitter(page)
            html = page.content()
            html_lower = html.lower()
            body_text = self._safe_inner_text(page, "body")
            body_text_lower = body_text.lower()
            page_title = self._safe_page_title(page).lower()

            challenge_detected = self._detect_challenge(
                body_text_lower=body_text_lower,
                html_lower=html_lower,
                page_title=page_title,
            )
            blocked = (response_status in {401, 403, 429}) or challenge_detected

            dom_signals = self._collect_dom_signals(page, body_text_lower)
            network_signals = sorted(network_snapshot.signals)
            available = self._is_available(
                blocked=blocked,
                challenge_detected=challenge_detected,
                dom_signals=dom_signals,
                network_count=network_snapshot.availability_count,
                body_text_lower=body_text_lower,
            )

            signal_type = self._signal_type(dom_signals, network_snapshot.availability_count)
            confidence = self._confidence(signal_type, blocked)
            prices = self._collect_prices(network_snapshot.prices)
            sections = sorted(network_snapshot.sections)
            abck = self.read_akamai_state()

            return ProbeResult(
                event_id=event_id,
                event_url=event_url,
                available=available,
                blocked=blocked,
                challenge_detected=challenge_detected,
                signal_type=signal_type,
                signal_confidence=confidence,
                price_summary=self._price_summary(prices),
                section_summary=self._section_summary(sections),
                raw_indicators={
                    "response_status": response_status,
                    "dom_signals": dom_signals,
                    "network_signals": network_signals,
                    "availability_count": network_snapshot.availability_count,
                    "listing_groups": self._listing_groups_debug(network_snapshot.listing_groups),
                    "page_title": page_title,
                    "retry_after_seconds": retry_after_seconds,
                    "abck_present": abck["abck_present"],
                    "abck_trusted": abck["abck_trusted"],
                    "abck_flagged": abck["abck_flagged"],
                },
                listing_summary=self._listing_summary(network_snapshot.listing_groups),
                abck_trusted=abck["abck_trusted"],
                abck_flagged=abck["abck_flagged"],
            )
        except Exception:
            if self.session_mode == "cdp_attach" and allow_retry:
                logger.warning("CDP event probe failed; reconnecting once before surfacing error")
                self._reconnect_cdp()
                return self._check_event_impl(event_id=event_id, event_url=event_url, allow_retry=False)
            raise
        finally:
            if page is not None and response_handler is not None:
                try:
                    page.remove_listener("response", response_handler)
                except Exception:
                    pass
            if page is not None and close_page:
                try:
                    page.close()
                except Exception:
                    pass

    def _reconnect_cdp(self):
        self.close()
        self.start()

    def _maybe_warmup(self, page: Any, event_id: str, first_visit: bool, timeout_ms: int) -> bool:
        """On a fresh tab (and periodically thereafter) browse the TM homepage before the
        event page so traffic looks like a person navigating. Returns True if it navigated.
        Skipped for cdp_attach (that's the user's own external browser — don't hijack it)."""
        if self.session_mode == "cdp_attach":
            return False
        interval = int(self.homepage_warmup_interval_seconds)
        if interval <= 0:
            return False
        now = time.monotonic()
        key = self.SINGLE_EVENT_PAGE_KEY if self.single_event_page and self.reuse_event_tabs else event_id
        last = self._last_warmup_at.get(key)
        due = first_visit or last is None or (now - last) >= interval
        if not due:
            return False
        try:
            page.goto(self.WARMUP_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(random.randint(400, 1200))
            self._last_warmup_at[key] = now
            return True
        except Exception:
            # Warm-up is best-effort; fall through to the normal event navigation.
            return False

    def _event_dwell_timeout_ms(self) -> int:
        low_seconds = max(0, int(self.event_dwell_min_seconds))
        high_seconds = max(low_seconds, int(self.event_dwell_max_seconds))
        if high_seconds <= 0:
            return 0
        return random.randint(low_seconds * 1000, high_seconds * 1000)

    def _warm_session_health_navigation(self, page: Any, timeout_ms: int):
        """Best-effort homepage warmup before the account health check."""
        try:
            page.goto(self.WARMUP_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(random.randint(400, 1200))
            self._human_jitter(page)
        except Exception:
            return

    @staticmethod
    def _human_jitter(page: Any):
        """Small, random human-like activity so the session isn't perfectly static:
        a little curved cursor movement (Akamai scores empty/synthetic mouse motion as
        bot near-instantly), an occasional hover, and sometimes a light scroll."""
        try:
            BrowserProbe._human_mouse_move(page)
        except Exception:
            pass
        if random.random() < 0.5:
            try:
                page.mouse.wheel(0, random.randint(200, 1200))
                page.wait_for_timeout(random.randint(150, 600))
            except Exception:
                pass

    @staticmethod
    def _human_mouse_move(page: Any):
        """Move the cursor along a short curved, variable-speed path with brief pauses.

        Real human motion accelerates, drifts between waypoints, and settles on a
        target — unlike a bot's straight, constant-speed jump. Keeping the rhythm
        irregular (and skipping some checks entirely) avoids a detectable fixed pattern.
        """
        if random.random() > 0.75:
            # Not every check needs cursor motion; keep the cadence human-irregular.
            return
        try:
            viewport = page.viewport_size or {}
        except Exception:
            viewport = {}
        width = int(viewport.get("width") or 1280)
        height = int(viewport.get("height") or 800)

        def _rand_point() -> tuple[int, int]:
            return (
                random.randint(int(width * 0.15), int(width * 0.85)),
                random.randint(int(height * 0.15), int(height * 0.75)),
            )

        # 2-4 waypoints, each reached in several small steps with a short dwell, so the
        # overall trajectory has curvature and non-uniform velocity.
        for _ in range(random.randint(2, 4)):
            x, y = _rand_point()
            try:
                page.mouse.move(x, y, steps=random.randint(8, 25))
                page.wait_for_timeout(random.randint(40, 220))
            except Exception:
                return

        # Occasionally settle by hovering a plausible on-page element.
        if random.random() < 0.4:
            for selector in ("a[href]", "button", "h1", "img"):
                try:
                    page.locator(selector).first.hover(timeout=800)
                    page.wait_for_timeout(random.randint(80, 300))
                    break
                except Exception:
                    continue

    def _get_or_create_event_page(self, event_id: str, event_url: str) -> tuple[Any, bool]:
        if self.single_event_page and self.reuse_event_tabs:
            return self._get_or_create_single_event_page(event_id=event_id, event_url=event_url)

        if self.session_mode != "cdp_attach":
            # We own this context (persistent/storage). Keep one tab per event open and
            # reload it instead of opening a fresh deep-link each check — more human-like
            # and it preserves the in-page session / DataDome cookie continuity.
            if self.reuse_event_tabs:
                cached = self._event_pages.get(event_id)
                if cached is not None:
                    try:
                        if not cached.is_closed():
                            return cached, False
                    except Exception:
                        pass
                # Reuse an idle blank tab (e.g. the one Chrome opens on launch) instead
                # of opening a new one, so blank tabs don't pile up.
                assigned = self._assigned_page_ids()
                for existing in list(getattr(self._context, "pages", [])):
                    try:
                        if id(existing) in assigned or existing.is_closed():
                            continue
                        existing_url = str(getattr(existing, "url", "")).lower()
                    except Exception:
                        continue
                    if existing_url in ("", "about:blank"):
                        self._event_pages[event_id] = existing
                        return existing, True
                page = self._context.new_page()
                self._event_pages[event_id] = page
                return page, True
            return self._context.new_page(), True

        if self.reuse_event_tabs:
            cached = self._event_pages.get(event_id)
            if cached is not None:
                try:
                    if not cached.is_closed():
                        return cached, False
                except Exception:
                    pass

            # Prefer matching an already-open tab for this event URL.
            pages = list(getattr(self._context, "pages", []))
            normalized_url = event_url.lower()
            for existing in pages:
                try:
                    existing_url = str(getattr(existing, "url", "")).lower()
                except Exception:
                    existing_url = ""
                if normalized_url and normalized_url in existing_url:
                    self._event_pages[event_id] = existing
                    return existing, False

            # Reuse the initial about:blank tab when no event tabs exist yet.
            assigned_pages = self._assigned_page_ids()
            for existing in pages:
                try:
                    existing_url = str(getattr(existing, "url", "")).lower()
                except Exception:
                    existing_url = ""
                if existing_url == "about:blank" and id(existing) not in assigned_pages:
                    self._event_pages[event_id] = existing
                    return existing, True

        page = self._context.new_page()
        if self.reuse_event_tabs:
            self._event_pages[event_id] = page
        return page, True

    def _get_or_create_single_event_page(self, event_id: str, event_url: str) -> tuple[Any, bool]:
        cached = self._event_pages.get(self.SINGLE_EVENT_PAGE_KEY)
        if cached is not None:
            try:
                if not cached.is_closed():
                    needs_navigation = self._single_event_page_event_id != event_id
                    self._single_event_page_event_id = event_id
                    return cached, needs_navigation
            except Exception:
                pass

        page, already_on_event = self._find_single_event_page(event_url=event_url)
        self._event_pages = {self.SINGLE_EVENT_PAGE_KEY: page}
        self._single_event_page_event_id = event_id
        return page, not already_on_event

    def _find_single_event_page(self, event_url: str) -> tuple[Any, bool]:
        pages = list(getattr(self._context, "pages", []))
        normalized_url = event_url.lower()
        assigned_pages = self._assigned_page_ids()

        for existing in pages:
            try:
                if id(existing) in assigned_pages or existing.is_closed():
                    continue
                existing_url = str(getattr(existing, "url", "")).lower()
            except Exception:
                continue
            if normalized_url and normalized_url in existing_url:
                return existing, True

        for existing in pages:
            try:
                if id(existing) in assigned_pages or existing.is_closed():
                    continue
                existing_url = str(getattr(existing, "url", "")).lower()
            except Exception:
                continue
            if existing_url in ("", "about:blank"):
                return existing, False

        return self._context.new_page(), False

    def _assigned_page_ids(self) -> set[int]:
        assigned = {id(p) for p in self._event_pages.values() if p is not None}
        if self._health_page is not None:
            assigned.add(id(self._health_page))
        return assigned

    def _get_or_create_health_page(self):
        cached = self._health_page
        if cached is not None:
            try:
                if not cached.is_closed():
                    return cached
            except Exception:
                pass

        assigned = self._assigned_page_ids()
        for existing in list(getattr(self._context, "pages", [])):
            try:
                if id(existing) in assigned or existing.is_closed():
                    continue
                existing_url = str(getattr(existing, "url", "")).lower()
            except Exception:
                continue
            if existing_url in ("", "about:blank"):
                self._health_page = existing
                return existing

        page = self._context.new_page()
        self._health_page = page
        return page

    def _capture_response(self, response, snapshot: _NetworkSnapshot):
        try:
            url = response.url.lower()
            if not any(key in url for key in self.NETWORK_KEYWORDS):
                return

            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return

            data = response.json()
            count, signals, prices, sections, listing_groups = self._extract_network_snapshot(data)
            snapshot.availability_count += count
            snapshot.signals.update(signals)
            snapshot.prices.extend(prices)
            snapshot.sections.update(sections)
            self._merge_listing_groups(snapshot.listing_groups, listing_groups)
        except Exception:
            # Network event parsing is best-effort.
            return

    def _extract_network_snapshot(
        self,
        payload: Any,
    ) -> tuple[int, set[str], list[float], set[str], dict[tuple[str, str, str], dict[str, Any]]]:
        availability_count = 0
        signals: set[str] = set()
        prices: list[float] = []
        sections: set[str] = set()
        listing_groups: dict[tuple[str, str, str], dict[str, Any]] = {}

        def walk(value: Any, path: tuple[str, ...] = ()):
            nonlocal availability_count
            if isinstance(value, dict):
                listing_group = self._extract_listing_group(value, path)
                if listing_group is not None:
                    group_key, group_value = listing_group
                    if group_key in listing_groups:
                        listing_groups[group_key]["count"] += group_value["count"]
                    else:
                        listing_groups[group_key] = dict(group_value)
                    prices.append(group_value["price"])
                for k, v in value.items():
                    lower_key = k.lower()
                    child_path = path + (lower_key,)
                    if lower_key in {"resale", "isresale", "facevalueexchange"} and bool(v):
                        signals.add("resale")
                    if lower_key in {"status", "availability"} and isinstance(v, str):
                        lower_val = v.lower()
                        if lower_val in {"onsale", "instock", "available"}:
                            availability_count += 1
                            signals.add("available_status")
                    if lower_key in {"available", "isavailable"}:
                        if isinstance(v, bool) and v:
                            availability_count += 1
                            signals.add("available_flag")
                        elif isinstance(v, int) and v > 0:
                            availability_count += int(v)
                            signals.add("available_count")
                    if lower_key in {"quantity", "availabletickets", "totalavailable"} and isinstance(v, int) and v > 0:
                        availability_count += int(v)
                        signals.add("quantity")
                    if lower_key in {"section", "sectionname"} and isinstance(v, str):
                        sections.add(v.strip())
                    walk(v, child_path)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item, path)
                return

            if isinstance(value, str):
                text = value.strip()
                lower_text = text.lower()
                if any(token in lower_text for token in ("face value exchange", "verified resale", "resale")):
                    signals.add("resale_text")
                if path and path[-1] in {"section", "sectionname"} and text:
                    sections.add(text)

        walk(payload)
        return availability_count, signals, prices, sections, listing_groups

    def _collect_dom_signals(self, page, body_text_lower: str) -> list[str]:
        signals: set[str] = set()
        for selector in self.CTA_SELECTORS:
            if page.locator(selector).count() > 0:
                signals.add("buy_cta_ui")
                break

        for selector in self.OFFER_CARD_SELECTORS:
            if page.locator(selector).count() > 0:
                signals.add("offer_card_ui")
                break

        for selector in self.RESALE_SELECTORS:
            if page.locator(selector).count() > 0:
                signals.add("resale_ui")
                break

        # Use rendered body text, not raw HTML, to avoid matching i18n dictionaries.
        if "face value exchange" in body_text_lower:
            signals.add("fve_text")
        if "verified resale" in body_text_lower:
            signals.add("verified_resale_text")
        if "tickets available" in body_text_lower:
            signals.add("tickets_available_text")
        if self._contains_any(body_text_lower, self.SOLD_OUT_PATTERNS):
            signals.add("sold_out_text")

        return sorted(signals)

    def _is_available(
        self,
        blocked: bool,
        challenge_detected: bool,
        dom_signals: list[str],
        network_count: int,
        body_text_lower: str,
    ) -> bool:
        if blocked or challenge_detected:
            return False

        # Network inventory is our strongest signal.
        if network_count > 0:
            return True

        # If visible text explicitly says sold out/unavailable, treat as not available.
        if self._contains_any(body_text_lower, self.SOLD_OUT_PATTERNS):
            return False

        # Strong DOM signals indicating actual inventory/offers.
        strong_dom = any(
            sig in {
                "offer_card_ui",
                "tickets_available_text",
            }
            for sig in dom_signals
        )
        if strong_dom:
            return True

        # Generic CTA buttons alone are too noisy and often present while offsale.
        return False

    @staticmethod
    def _signal_type(dom_signals: list[str], network_count: int) -> ProbeSignalType:
        has_dom = len(dom_signals) > 0
        has_network = network_count > 0
        if has_dom and has_network:
            return ProbeSignalType.DOM_AND_NETWORK
        if has_dom:
            return ProbeSignalType.DOM
        if has_network:
            return ProbeSignalType.NETWORK
        return ProbeSignalType.NONE

    @staticmethod
    def _confidence(signal_type: ProbeSignalType, blocked: bool) -> float:
        if blocked:
            return 0.0
        if signal_type == ProbeSignalType.DOM_AND_NETWORK:
            return 0.95
        if signal_type == ProbeSignalType.NETWORK:
            return 0.85
        if signal_type == ProbeSignalType.DOM:
            return 0.80
        return 0.20

    @staticmethod
    def _contains_any(text: str, values: Iterable[str]) -> bool:
        return any(v in text for v in values)

    @staticmethod
    def _parse_retry_after(response) -> int | None:
        """Extract a delay (seconds) from a 429's Retry-After header. Supports the
        delta-seconds form (e.g. "120"); returns None for absent/unparseable values
        or the HTTP-date form (rare here, not worth the dependency)."""
        try:
            raw = response.headers.get("retry-after")
        except Exception:
            return None
        if not raw:
            return None
        raw = str(raw).strip()
        if raw.isdigit():
            value = int(raw)
            return value if value > 0 else None
        return None

    def _detect_challenge(self, body_text_lower: str, html_lower: str, page_title: str) -> bool:
        # Prefer visible text + title, not raw HTML token noise.
        if self._contains_any(body_text_lower, self.CHALLENGE_PATTERNS):
            return True
        if self._contains_any(page_title, ("just a moment", "attention required", "captcha")):
            return True
        # Keep two strong HTML-only indicators.
        if "cf-challenge" in html_lower or "datadome" in html_lower:
            return True
        return False

    def _session_health_page_state(self, page) -> tuple[str, str, str, bool]:
        html = page.content()
        body_text = self._safe_inner_text(page, "body")
        page_title = self._safe_page_title(page).lower()
        challenge = self._detect_challenge(
            body_text_lower=body_text.lower(),
            html_lower=html.lower(),
            page_title=page_title,
        )
        return html, body_text, page_title, challenge

    @staticmethod
    def _has_password_input(page) -> bool:
        try:
            locator = page.locator("input[type='password'], input[name='password'], input#password")
            return locator.count() > 0
        except Exception:
            return False

    @staticmethod
    def _safe_inner_text(page, selector: str) -> str:
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                return ""
            text = locator.inner_text(timeout=2000)
            return text or ""
        except Exception:
            # Fallback for pages/tests where inner_text is unavailable.
            try:
                html = page.content() or ""
                text = re.sub(r"<[^>]+>", " ", html)
                return re.sub(r"\s+", " ", text).strip()
            except Exception:
                return ""

    @staticmethod
    def _safe_page_title(page) -> str:
        try:
            value = page.title()
            return value or ""
        except Exception:
            return ""

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace("$", "").replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @staticmethod
    def _collect_prices(network_prices: list[float]) -> list[float]:
        normalized = {round(p, 2) for p in network_prices if p > 0}
        return sorted(normalized)

    @staticmethod
    def _price_summary(prices: list[float]) -> str | None:
        if not prices:
            return None
        return f"${min(prices):.2f} - ${max(prices):.2f}"

    @staticmethod
    def _section_summary(sections: list[str]) -> str | None:
        if not sections:
            return None
        cleaned = [s for s in sections if s]
        if not cleaned:
            return None
        preview = cleaned[:5]
        suffix = " ..." if len(cleaned) > 5 else ""
        return ", ".join(preview) + suffix

    @staticmethod
    def _is_offer_like_path(path: tuple[str, ...]) -> bool:
        for part in path:
            if part in {"offer", "offers", "listing", "listings", "quickpick", "quickpicks"}:
                return True
            if any(token in part for token in ("offer", "listing", "quickpick")):
                return True
        return False

    @staticmethod
    def _merge_listing_groups(
        target: dict[tuple[str, str, str], dict[str, Any]],
        incoming: dict[tuple[str, str, str], dict[str, Any]],
    ):
        for key, group in incoming.items():
            existing = target.get(key)
            if existing is None or int(group.get("count", 0)) > int(existing.get("count", 0)):
                target[key] = dict(group)

    def _extract_listing_group(
        self,
        value: dict[str, Any],
        path: tuple[str, ...],
    ) -> tuple[tuple[str, str, str], dict[str, Any]] | None:
        if not self._is_offer_like_path(path):
            return None

        lowered = {str(k).lower(): v for k, v in value.items()}
        listing_anchor_keys = {
            "section",
            "sectionname",
            "row",
            "rowname",
            "quantity",
            "availabletickets",
            "totalavailable",
            "available",
            "isavailable",
            "offerid",
            "listingid",
        }
        if not any(anchor in lowered for anchor in listing_anchor_keys):
            return None

        price = None
        for price_key in ("listprice", "totalprice", "nochargesprice", "price"):
            maybe_price = self._to_float(lowered.get(price_key))
            if maybe_price is not None and maybe_price > 0:
                price = round(maybe_price, 2)
                break
        if price is None:
            return None

        section = self._normalize_section(lowered.get("sectionname") or lowered.get("section"))
        row = self._normalize_row(lowered.get("rowname") or lowered.get("row"))

        count = 1
        for quantity_key in ("quantity", "availabletickets", "totalavailable"):
            maybe_quantity = self._to_int(lowered.get(quantity_key))
            if maybe_quantity is not None and maybe_quantity > 0:
                count = maybe_quantity
                break
        if count <= 1:
            maybe_max_quantity = self._to_int(lowered.get("maxquantity"))
            if maybe_max_quantity is not None and maybe_max_quantity > 0:
                count = maybe_max_quantity
        if count <= 1:
            maybe_sellable_quantity = self._max_sellable_quantity(lowered.get("sellablequantities"))
            if maybe_sellable_quantity is not None and maybe_sellable_quantity > 0:
                count = maybe_sellable_quantity
        if count <= 1:
            maybe_seat_span = self._seat_span(lowered.get("seatfrom"), lowered.get("seatto"))
            if maybe_seat_span is not None and maybe_seat_span > 0:
                count = maybe_seat_span

        key = (section, row, f"{price:.2f}")
        return key, {"section": section, "row": row, "price": price, "count": count}

    @staticmethod
    def _normalize_section(value: Any) -> str:
        if not isinstance(value, str):
            return "?"
        section = re.sub(r"\s+", " ", value).strip()
        if not section:
            return "?"
        return section.upper()

    @staticmethod
    def _normalize_row(value: Any) -> str:
        if not isinstance(value, str):
            return "?"
        row = re.sub(r"\s+", " ", value).strip()
        if not row:
            return "?"
        lower = row.lower()
        if lower.startswith("row "):
            row = row[4:].strip()
            if not row:
                return "?"
        return row

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if not cleaned:
                return None
            try:
                return int(float(cleaned))
            except ValueError:
                return None
        return None

    @staticmethod
    def _max_sellable_quantity(value: Any) -> int | None:
        if not isinstance(value, list):
            return None
        max_qty: int | None = None
        for item in value:
            maybe = BrowserProbe._to_int(item)
            if maybe is None or maybe <= 0:
                continue
            if max_qty is None or maybe > max_qty:
                max_qty = maybe
        return max_qty

    @staticmethod
    def _seat_span(seat_from: Any, seat_to: Any) -> int | None:
        start = BrowserProbe._to_int(seat_from)
        end = BrowserProbe._to_int(seat_to)
        if start is None or end is None:
            return None
        if end < start:
            return None
        return (end - start) + 1

    @staticmethod
    def _listing_summary(groups: dict[tuple[str, str, str], dict[str, Any]]) -> str | None:
        if not groups:
            return None
        ordered = sorted(
            groups.values(),
            key=lambda item: (
                -int(item.get("count", 0)),
                float(item.get("price", 0.0)),
                str(item.get("section", "?")),
                str(item.get("row", "?")),
            ),
        )
        lines = []
        for item in ordered[:5]:
            section = str(item.get("section", "?")) or "?"
            row = str(item.get("row", "?")) or "?"
            price = float(item.get("price", 0.0))
            count = max(1, int(item.get("count", 1)))
            lines.append(f"{section} / Row {row} / ${price:.2f} x{count}")
        summary = "; ".join(lines)
        omitted = max(0, len(ordered) - len(lines))
        if omitted > 0:
            return f"{summary}; (+{omitted} more)"
        return summary

    @staticmethod
    def _listing_groups_debug(groups: dict[tuple[str, str, str], dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            groups.values(),
            key=lambda item: (
                str(item.get("section", "?")),
                str(item.get("row", "?")),
                float(item.get("price", 0.0)),
            ),
        )
        debug_items: list[dict[str, Any]] = []
        for item in ordered:
            debug_items.append(
                {
                    "section": str(item.get("section", "?")) or "?",
                    "row": str(item.get("row", "?")) or "?",
                    "price": round(float(item.get("price", 0.0)), 2),
                    "count": max(1, int(item.get("count", 1))),
                }
            )
        return debug_items

    # ---- Bootstrap helpers ----

    @staticmethod
    def save_cdp_attach_interactive(
        event_url: str,
        cdp_endpoint_url: str,
        navigation_timeout_seconds: int,
        *,
        stop_event=None,
    ):
        """Attach to an already-running Chrome instance and guide manual login in-place.

        stop_event: optional threading.Event; when set, proceeds without waiting for stdin.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import guard
            raise BrowserProbeError(PLAYWRIGHT_IMPORT_ERROR) from exc

        if not cdp_endpoint_url:
            raise BrowserProbeError("browser.cdp_endpoint_url is required when browser.session_mode is cdp_attach")

        with sync_playwright() as playwright:
            connect_timeout_ms = max(1000, int(navigation_timeout_seconds * 1000))
            browser = playwright.chromium.connect_over_cdp(cdp_endpoint_url, timeout=connect_timeout_ms)
            contexts = list(getattr(browser, "contexts", []))
            context = contexts[0] if contexts else browser.new_context()
            pages = list(getattr(context, "pages", []))
            page = pages[0] if pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.goto(event_url, wait_until="domcontentloaded", timeout=navigation_timeout_seconds * 1000)

            if stop_event is not None:
                stop_event.wait()
            else:
                print("\nComplete Ticketmaster login/challenge in the opened Chrome window.")
                print("This uses your dedicated persistent Chrome profile; credentials should persist.")
                input("Press Enter after login is complete...")

        logger.info("CDP bootstrap completed via %s", cdp_endpoint_url)

    @staticmethod
    def save_storage_state_interactive(
        event_url: str,
        output_path: str,
        navigation_timeout_seconds: int,
        *,
        stop_event=None,
    ):
        """Launch a headed browser for one-time manual login and persist storage state.

        stop_event: optional threading.Event; when set, proceeds without waiting for stdin.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import guard
            raise BrowserProbeError(PLAYWRIGHT_IMPORT_ERROR) from exc

        output_dir = os.path.dirname(output_path) or "."
        os.makedirs(output_dir, exist_ok=True)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False, args=["--disable-dev-shm-usage"])
            context = browser.new_context()
            page = context.new_page()
            page.goto(event_url, wait_until="domcontentloaded", timeout=navigation_timeout_seconds * 1000)

            if stop_event is not None:
                stop_event.wait()
            else:
                print("\nComplete Ticketmaster login/challenge in the opened browser window.")
                print("After you can load the event page normally, return to this terminal.")
                input("Press Enter to save session state...")

            context.storage_state(path=output_path)
            browser.close()

        os.chmod(output_path, 0o600)
        logger.info("Saved storage state to %s", output_path)

    @staticmethod
    def save_persistent_profile_interactive(
        event_url: str,
        user_data_dir: str,
        navigation_timeout_seconds: int,
        *,
        channel: str = "",
        stop_event=None,
    ):
        """Launch a headed persistent profile and wait for manual login/challenge completion.

        stop_event: optional threading.Event; when set, proceeds without waiting for stdin.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import guard
            raise BrowserProbeError(PLAYWRIGHT_IMPORT_ERROR) from exc

        if not user_data_dir:
            raise BrowserProbeError(
                "browser.user_data_dir is required when browser.session_mode is persistent_profile"
            )
        os.makedirs(user_data_dir, exist_ok=True)

        with sync_playwright() as playwright:
            launch_kwargs = BrowserProbe._launch_kwargs(headless=False, channel=channel)
            try:
                context = playwright.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
            except Exception as launch_exc:
                if channel and _is_channel_not_found_error(launch_exc):
                    logger.warning(
                        "Chrome channel %r not found — falling back to bundled Chromium.",
                        channel,
                    )
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir,
                        **BrowserProbe._launch_kwargs(headless=False, channel=None),
                    )
                else:
                    raise
            page = context.new_page()
            page.goto(event_url, wait_until="domcontentloaded", timeout=navigation_timeout_seconds * 1000)

            if stop_event is not None:
                stop_event.wait()
            else:
                print("\nComplete Ticketmaster login/challenge in the opened browser window.")
                print("After you can load the event page normally, return to this terminal.")
                input("Press Enter to continue...")

            context.close()

        logger.info("Persistent profile bootstrap completed for %s", user_data_dir)
