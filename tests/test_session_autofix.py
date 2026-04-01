"""Tests for Ticketmaster session auto-fix helper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.session_autofix import AutoFixCredentialError, TicketmasterSessionAutoFixer


class _FakeLocator:
    def __init__(self, present: bool = False):
        self.present = present
        self.filled: list[str] = []
        self.clicked = 0
        self.pressed: list[str] = []

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self.present else 0

    def fill(self, value: str, timeout: int | None = None):
        del timeout
        self.filled.append(value)

    def click(self, timeout: int | None = None):
        del timeout
        self.clicked += 1

    def press(self, key: str):
        self.pressed.append(key)


class _FakePage:
    def __init__(self, locator_map: dict[str, _FakeLocator]):
        self._locator_map = locator_map
        self.url = "https://www.ticketmaster.com/event/abc"

    def locator(self, selector: str):
        return self._locator_map.get(selector, _FakeLocator(present=False))

    def goto(self, *_args, **_kwargs):
        return SimpleNamespace(status=200)

    def wait_for_timeout(self, _ms: int):
        return

    def wait_for_load_state(self, *_args, **_kwargs):
        return

    def title(self):
        return "Ticketmaster"

    def content(self):
        return "<html>ok</html>"


class _FakeContext:
    def __init__(self):
        self.page = object()

    def new_page(self):
        return self.page

    def storage_state(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"cookies":[],"origins":[]}')


class _FakePersistentContext:
    def __init__(self):
        self.page = object()
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()

    def new_context(self):
        return self.context

    def close(self):
        return


class _FakePlaywright:
    def __init__(self):
        self.chromium = SimpleNamespace(
            launch=lambda **_kwargs: _FakeBrowser(),
            launch_persistent_context=lambda *_args, **_kwargs: _FakePersistentContext(),
        )


class _FakeSyncPlaywright:
    def __enter__(self):
        return _FakePlaywright()

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False


def _make_autofixer() -> TicketmasterSessionAutoFixer:
    return TicketmasterSessionAutoFixer(
        keychain_service="svc",
        keychain_email_account="email",
        keychain_password_account="password",
    )


def test_load_credentials_reads_keychain(monkeypatch):
    responses = {
        "email": "user@example.com\n",
        "password": "secret-pass\n",
    }

    def fake_run(cmd, capture_output, text, check):
        del capture_output, text, check
        account = cmd[cmd.index("-a") + 1]
        return SimpleNamespace(returncode=0, stdout=responses[account], stderr="")

    monkeypatch.setattr("src.session_autofix.subprocess.run", fake_run)

    autofixer = _make_autofixer()
    email, password = autofixer.load_credentials()

    assert email == "user@example.com"
    assert password == "secret-pass"


def test_load_credentials_raises_when_keychain_item_missing(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=44, stdout="", stderr="item not found")

    monkeypatch.setattr("src.session_autofix.subprocess.run", fake_run)

    autofixer = _make_autofixer()
    with pytest.raises(AutoFixCredentialError):
        autofixer.load_credentials()


def test_perform_login_uses_fallback_selectors_and_enter_submit():
    autofixer = _make_autofixer()
    email_locator = _FakeLocator(present=True)
    password_locator = _FakeLocator(present=True)
    page = _FakePage(
        {
            "input[name='username']": email_locator,
            "input[name='password']": password_locator,
        }
    )

    ok, reason = autofixer._perform_interactive_login(
        page=page,
        event_url="https://www.ticketmaster.com/event/abc",
        email="user@example.com",
        password="secret-pass",
        timeout_ms=10000,
    )

    assert ok is True
    assert reason == "submitted_credentials"
    assert email_locator.filled == ["user@example.com"]
    assert password_locator.filled == ["secret-pass"]
    assert password_locator.pressed == ["Enter"]


def test_attempt_reauth_writes_storage_state_and_sets_permissions(tmp_path, monkeypatch):
    storage_state_path = str(tmp_path / "tm_storage_state.json")
    autofixer = _make_autofixer()

    monkeypatch.setattr(
        autofixer,
        "load_credentials",
        lambda: ("user@example.com", "secret-pass"),
    )
    monkeypatch.setattr(autofixer, "_get_sync_playwright", lambda: _FakeSyncPlaywright)
    monkeypatch.setattr(autofixer, "_perform_interactive_login", lambda **_kwargs: (True, "ok"))
    monkeypatch.setattr(autofixer, "_verify_authenticated_session", lambda **_kwargs: (True, "ok"))

    result = autofixer.attempt_reauth(
        event_url="https://www.ticketmaster.com/event/abc",
        storage_state_path=storage_state_path,
        timeout_seconds=20,
    )

    assert result.success is True
    assert result.reason == "session_refreshed"
    mode = oct(tmp_path.joinpath("tm_storage_state.json").stat().st_mode & 0o777)
    assert mode == "0o600"


def test_attempt_reauth_persistent_profile_success(tmp_path, monkeypatch):
    profile_dir = tmp_path / "tm_profile"
    autofixer = _make_autofixer()

    monkeypatch.setattr(
        autofixer,
        "load_credentials",
        lambda: ("user@example.com", "secret-pass"),
    )
    monkeypatch.setattr(autofixer, "_get_sync_playwright", lambda: _FakeSyncPlaywright)
    monkeypatch.setattr(autofixer, "_perform_interactive_login", lambda **_kwargs: (True, "ok"))

    verified_urls: list[str] = []

    def _verify(page, event_url: str, timeout_ms: int):
        del page, timeout_ms
        verified_urls.append(event_url)
        return True, "ok"

    monkeypatch.setattr(autofixer, "_verify_authenticated_session", _verify)

    result = autofixer.attempt_reauth(
        event_url="https://www.ticketmaster.com/event/abc",
        storage_state_path=str(tmp_path / "unused.json"),
        timeout_seconds=20,
        session_mode="persistent_profile",
        user_data_dir=str(profile_dir),
        channel="chrome",
        headless=False,
        verify_event_urls=[
            "https://www.ticketmaster.com/event/abc",
            "https://www.ticketmaster.com/event/def",
        ],
    )

    assert result.success is True
    assert result.reason == "profile_session_refreshed"
    assert verified_urls == [
        "https://www.ticketmaster.com/event/abc",
        "https://www.ticketmaster.com/event/def",
    ]


def test_attempt_reauth_persistent_profile_reports_challenge(tmp_path, monkeypatch):
    autofixer = _make_autofixer()

    monkeypatch.setattr(
        autofixer,
        "load_credentials",
        lambda: ("user@example.com", "secret-pass"),
    )
    monkeypatch.setattr(autofixer, "_get_sync_playwright", lambda: _FakeSyncPlaywright)
    monkeypatch.setattr(autofixer, "_perform_interactive_login", lambda **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        autofixer,
        "_verify_authenticated_session",
        lambda **_kwargs: (False, "challenge_detected"),
    )

    result = autofixer.attempt_reauth(
        event_url="https://www.ticketmaster.com/event/abc",
        storage_state_path=str(tmp_path / "unused.json"),
        timeout_seconds=20,
        session_mode="persistent_profile",
        user_data_dir=str(tmp_path / "tm_profile"),
        channel="chrome",
        headless=False,
    )

    assert result.success is False
    assert result.reason == "challenge_detected"


def test_attempt_reauth_persistent_profile_requires_user_data_dir(tmp_path, monkeypatch):
    autofixer = _make_autofixer()
    monkeypatch.setattr(
        autofixer,
        "load_credentials",
        lambda: ("user@example.com", "secret-pass"),
    )
    monkeypatch.setattr(autofixer, "_get_sync_playwright", lambda: _FakeSyncPlaywright)

    result = autofixer.attempt_reauth(
        event_url="https://www.ticketmaster.com/event/abc",
        storage_state_path=str(tmp_path / "unused.json"),
        timeout_seconds=20,
        session_mode="persistent_profile",
        user_data_dir="",
    )

    assert result.success is False
    assert result.reason == "persistent_profile_requires_user_data_dir"
