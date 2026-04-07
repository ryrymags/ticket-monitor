"""Auto-fix helpers for Ticketmaster browser session re-authentication."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _is_channel_not_found_error(exc: BaseException) -> bool:
    """Return True if the exception indicates a Playwright browser channel is not installed."""
    msg = str(exc).lower()
    return "not found" in msg and ("distribution" in msg or "channel" in msg or "chrome" in msg)


class AutoFixCredentialError(Exception):
    """Raised when Keychain credentials cannot be loaded."""


@dataclass
class AutoReauthResult:
    success: bool
    reason: str


class TicketmasterSessionAutoFixer:
    """Performs unattended re-auth using macOS Keychain credentials."""

    SIGN_IN_SELECTORS = [
        "button:has-text('Sign In')",
        "a:has-text('Sign In')",
        "button:has-text('Log In')",
        "a:has-text('Log In')",
        "button:has-text('Continue with Email')",
        "[data-bdd*='sign-in']",
        "[data-testid*='sign-in']",
    ]
    EMAIL_SELECTORS = [
        "input[type='email']",
        "input[name='email']",
        "input[id*='email']",
        "input[name='username']",
        "input[autocomplete='username']",
    ]
    PASSWORD_SELECTORS = [
        "input[type='password']",
        "input[name='password']",
        "input[id*='password']",
        "input[autocomplete='current-password']",
    ]
    NEXT_SELECTORS = [
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Continue with Email')",
        "button[type='submit']",
        "input[type='submit']",
    ]
    SUBMIT_SELECTORS = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Log In')",
        "button:has-text('Continue')",
    ]
    CHALLENGE_PATTERNS = (
        "verify you are human",
        "attention required",
        "captcha",
        "datadome",
        "cf-challenge",
        "press & hold",
    )
    LOGIN_URL_PATTERNS = (
        "auth.ticketmaster.com",
        "/login",
        "/signin",
        "/sign-in",
    )
    LOGIN_ENTRY_URL = "https://www.ticketmaster.com/"
    LOGIN_FALLBACK_URL = "https://www.ticketmaster.com/member"

    def __init__(
        self,
        keychain_service: str,
        keychain_email_account: str,
        keychain_password_account: str,
    ):
        self.keychain_service = keychain_service
        self.keychain_email_account = keychain_email_account
        self.keychain_password_account = keychain_password_account

    def validate_credentials(self):
        """Verify configured Keychain entries are readable."""
        self.load_credentials()

    def load_credentials(self) -> tuple[str, str]:
        """Load Ticketmaster email/password from Keychain."""
        email = self._keychain_lookup(self.keychain_email_account)
        password = self._keychain_lookup(self.keychain_password_account)
        if not email:
            raise AutoFixCredentialError(
                f"Keychain item is empty: service={self.keychain_service} account={self.keychain_email_account}"
            )
        if not password:
            raise AutoFixCredentialError(
                f"Keychain item is empty: service={self.keychain_service} account={self.keychain_password_account}"
            )
        return email, password

    def attempt_reauth(
        self,
        event_url: str,
        storage_state_path: str,
        timeout_seconds: int,
        *,
        session_mode: str = "storage_state",
        user_data_dir: str = "",
        channel: str = "",
        headless: bool = True,
        verify_event_urls: list[str] | None = None,
    ) -> AutoReauthResult:
        """Attempt unattended re-auth and refresh browser session state."""
        try:
            email, password = self.load_credentials()
        except AutoFixCredentialError as exc:
            return AutoReauthResult(success=False, reason=str(exc))

        try:
            sync_playwright = self._get_sync_playwright()
        except Exception:
            return AutoReauthResult(
                success=False,
                reason=(
                    "Playwright is not installed. Install dependencies and run: "
                    "python -m playwright install chromium "
                    "(and python -m playwright install chrome when using browser.channel=chrome)"
                ),
            )

        timeout_ms = max(5_000, int(timeout_seconds * 1000))
        normalized_mode = (session_mode or "storage_state").strip().lower()
        verify_targets = [url for url in (verify_event_urls or [event_url]) if str(url).strip()]
        if not verify_targets:
            verify_targets = [event_url]

        try:
            if normalized_mode == "persistent_profile":
                if not user_data_dir:
                    return AutoReauthResult(
                        success=False,
                        reason="persistent_profile_requires_user_data_dir",
                    )
                os.makedirs(user_data_dir, exist_ok=True)

                with sync_playwright() as playwright:
                    launch_kwargs = self._launch_kwargs(headless=headless, channel=channel)
                    try:
                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir,
                            **launch_kwargs,
                        )
                    except Exception as launch_exc:
                        if channel and _is_channel_not_found_error(launch_exc):
                            logger.warning(
                                "Chrome channel %r not found — falling back to bundled Chromium. "
                                "Install Google Chrome for best Ticketmaster compatibility.",
                                channel,
                            )
                            context = playwright.chromium.launch_persistent_context(
                                user_data_dir,
                                **self._launch_kwargs(headless=headless, channel=None),
                            )
                        else:
                            raise
                    pages = getattr(context, "pages", None)
                    page = pages[0] if pages else context.new_page()

                    login_ok, login_reason = self._perform_interactive_login(
                        page=page,
                        event_url=event_url,
                        email=email,
                        password=password,
                        timeout_ms=timeout_ms,
                    )
                    if not login_ok:
                        context.close()
                        return AutoReauthResult(success=False, reason=login_reason)

                    for verify_url in verify_targets:
                        verified, verify_reason = self._verify_authenticated_session(
                            page=page,
                            event_url=verify_url,
                            timeout_ms=timeout_ms,
                        )
                        if not verified:
                            context.close()
                            return AutoReauthResult(success=False, reason=verify_reason)

                    context.close()

                logger.info("Auto re-auth succeeded; refreshed persistent profile at %s", user_data_dir)
                return AutoReauthResult(success=True, reason="profile_session_refreshed")

            if normalized_mode != "storage_state":
                return AutoReauthResult(success=False, reason=f"unsupported_session_mode:{normalized_mode}")

            output_dir = os.path.dirname(storage_state_path) or "."
            os.makedirs(output_dir, exist_ok=True)

            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(
                        **self._launch_kwargs(headless=True, channel=channel),
                    )
                except Exception as launch_exc:
                    if channel and _is_channel_not_found_error(launch_exc):
                        logger.warning(
                            "Chrome channel %r not found — falling back to bundled Chromium. "
                            "Install Google Chrome for best Ticketmaster compatibility.",
                            channel,
                        )
                        browser = playwright.chromium.launch(
                            **self._launch_kwargs(headless=True, channel=None),
                        )
                    else:
                        raise
                context = browser.new_context()
                page = context.new_page()

                login_ok, login_reason = self._perform_interactive_login(
                    page=page,
                    event_url=event_url,
                    email=email,
                    password=password,
                    timeout_ms=timeout_ms,
                )
                if not login_ok:
                    browser.close()
                    return AutoReauthResult(success=False, reason=login_reason)

                verified, verify_reason = self._verify_authenticated_session(
                    page=page,
                    event_url=verify_targets[0],
                    timeout_ms=timeout_ms,
                )
                if not verified:
                    browser.close()
                    return AutoReauthResult(success=False, reason=verify_reason)

                context.storage_state(path=storage_state_path)
                browser.close()
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            return AutoReauthResult(success=False, reason=self._sanitize_runtime_error(exc))

        os.chmod(storage_state_path, 0o600)
        logger.info("Auto re-auth succeeded; refreshed storage state at %s", storage_state_path)
        return AutoReauthResult(success=True, reason="session_refreshed")

    def _perform_interactive_login(
        self,
        page: Any,
        event_url: str,
        email: str,
        password: str,
        timeout_ms: int,
    ) -> tuple[bool, str]:
        # Start from home page because event URLs may return hard 401 pages with no sign-in controls.
        page.goto(self.LOGIN_ENTRY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)

        self._click_first(page, self.SIGN_IN_SELECTORS)
        page.wait_for_timeout(1800)

        email_field = self._wait_for_any_locator(
            page=page,
            selectors=self.EMAIL_SELECTORS,
            timeout_ms=max(3000, int(timeout_ms * 0.5)),
        )
        if email_field is None:
            # Fallback route that forces auth redirect in many flows.
            page.goto(self.LOGIN_FALLBACK_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            email_field = self._wait_for_any_locator(
                page=page,
                selectors=self.EMAIL_SELECTORS,
                timeout_ms=max(3000, int(timeout_ms * 0.5)),
            )
            if email_field is None:
                return False, "email_field_not_found"
        email_field.fill(email, timeout=timeout_ms)

        if self._wait_for_any_locator(page=page, selectors=self.PASSWORD_SELECTORS, timeout_ms=800) is None:
            if not self._click_first(page, self.NEXT_SELECTORS):
                try:
                    email_field.press("Enter")
                except Exception:
                    pass
            page.wait_for_timeout(1200)

        password_field = self._wait_for_any_locator(
            page=page,
            selectors=self.PASSWORD_SELECTORS,
            timeout_ms=max(3000, int(timeout_ms * 0.6)),
        )
        if password_field is None:
            return False, "password_field_not_found"

        password_field.fill(password, timeout=timeout_ms)
        if not self._click_first(page, self.SUBMIT_SELECTORS):
            try:
                password_field.press("Enter")
            except Exception:
                return False, "submit_action_not_found"

        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1400)
        return True, "submitted_credentials"

    def _verify_authenticated_session(
        self,
        page: Any,
        event_url: str,
        timeout_ms: int,
    ) -> tuple[bool, str]:
        response = page.goto(event_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1200)

        status = None
        try:
            if response is not None:
                status = response.status
        except Exception:
            status = None

        current_url = str(getattr(page, "url", "") or "").lower()
        title = self._safe_page_title(page).lower()
        html = self._safe_page_content(page).lower()
        combined = " ".join((current_url, title, html))

        if status in {401, 403}:
            return False, "auth_required_status"
        if status == 429:
            return False, "rate_limited"
        if any(token in current_url for token in self.LOGIN_URL_PATTERNS):
            return False, "still_on_login_page"
        if any(token in combined for token in self.CHALLENGE_PATTERNS):
            return False, "challenge_detected"

        return True, "verified_event_access"

    def _keychain_lookup(self, account: str) -> str:
        try:
            proc = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    self.keychain_service,
                    "-a",
                    account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AutoFixCredentialError("macOS security CLI not found") from exc

        if proc.returncode != 0:
            raise AutoFixCredentialError(
                f"Keychain item not found: service={self.keychain_service} account={account}"
            )

        value = (proc.stdout or "").strip()
        if not value:
            raise AutoFixCredentialError(
                f"Keychain item is empty: service={self.keychain_service} account={account}"
            )
        return value

    @staticmethod
    def _get_sync_playwright():
        from playwright.sync_api import sync_playwright

        return sync_playwright

    @staticmethod
    def _launch_kwargs(*, headless: bool, channel: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        }
        if channel:
            kwargs["channel"] = channel
        return kwargs

    @staticmethod
    def _sanitize_runtime_error(exc: Exception) -> str:
        text = str(exc).strip().lower()
        if "timeout" in text:
            return "reauth_timeout"
        if "net::" in text:
            return "reauth_network_error"
        detail = str(exc).strip().replace("\n", " ")
        if detail:
            detail = detail[:120]
            return f"reauth_failed:{type(exc).__name__}:{detail}"
        return f"reauth_failed:{type(exc).__name__}"

    @staticmethod
    def _safe_page_title(page: Any) -> str:
        try:
            value = page.title()
            return value or ""
        except Exception:
            return ""

    @staticmethod
    def _safe_page_content(page: Any) -> str:
        try:
            value = page.content()
            return value or ""
        except Exception:
            return ""

    @staticmethod
    def _first_locator(page: Any, selectors: list[str]):
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() <= 0:
                    continue
                first = getattr(locator, "first", None)
                if first is not None:
                    return first
                try:
                    return locator.nth(0)
                except Exception:
                    return locator
            except Exception:
                continue
        return None

    @classmethod
    def _click_first(cls, page: Any, selectors: list[str]) -> bool:
        locator = cls._first_locator(page, selectors)
        if locator is None:
            return False
        try:
            locator.click(timeout=3000)
            return True
        except Exception:
            return False

    @classmethod
    def _wait_for_any_locator(
        cls,
        page: Any,
        selectors: list[str],
        timeout_ms: int,
        poll_ms: int = 250,
    ):
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while time.monotonic() < deadline:
            locator = cls._first_locator(page, selectors)
            if locator is not None:
                return locator
            try:
                page.wait_for_timeout(poll_ms)
            except Exception:
                break
        return cls._first_locator(page, selectors)
