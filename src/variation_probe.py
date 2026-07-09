"""Block-scope diagnosis via browser-session variations.

When Ticketmaster pauses the session, the right remedy depends on the block's scope,
which a single browser state can't reveal. This probe checks the same event page from
four short-lived browser variations:

  signed_in_regular   — copy of the real profile (auth + DataDome/_abck trust cookies)
  signed_out_regular  — same profile copy with auth cookies stripped (fingerprint kept)
  signed_in_private   — fresh context + auth cookies injected from the storage state
  signed_out_private  — fresh context, nothing injected (cleanest possible slate)

and maps which of them get blocked to a scope verdict:

  profile    — the real profile's cookies are poisoned → clear/rebuild profile
  account    — the TM account itself is flagged → pause auth, re-login
  ip_device  — even a clean private window is blocked → only waiting (or a reboot,
               via the guardian's last-resort tier) can help
  none       — nothing is blocked (the pause was transient)
  unknown    — probe couldn't run enough variations / results are contradictory

The live ``secrets/tm_profile`` directory is never touched — the monitor's Chrome owns
it. Variations run against a temp copy that is deleted afterwards.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .browser_probe import BrowserProbe, BrowserProbeError
from .config import MonitorConfig

logger = logging.getLogger(__name__)

VARIATIONS = (
    "signed_in_regular",
    "signed_out_regular",
    "signed_in_private",
    "signed_out_private",
)

# Bot-management cookies to KEEP when stripping a profile down to "signed out":
# these carry the fingerprint/trust state that is exactly what the regular-window
# variations are meant to test. Everything else (auth/session cookies) is dropped.
_BOT_TRUST_COOKIE_PREFIXES = ("datadome", "_abck", "bm_", "ak_bmsc", "bm_sz")

# Chrome profile subtrees that are pure cache — skipping them makes the temp copy
# fast and small without changing cookies/fingerprint.
_PROFILE_COPY_IGNORES = shutil.ignore_patterns(
    "Cache*",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Service Worker",
    "*.log",
    "Crashpad",
)


@dataclass
class VariationOutcome:
    variation: str
    ran: bool
    blocked: bool = False
    challenge: bool = False
    abck_trusted: bool = False
    abck_flagged: bool = False
    error: str = ""


@dataclass
class VariationReport:
    at: str
    event_url: str
    scope: str
    outcomes: list[VariationOutcome] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "event_url": self.event_url,
            "scope": self.scope,
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def interpret_matrix(outcomes: list[VariationOutcome]) -> str:
    """Map which variations got blocked to a block-scope verdict (pure function)."""
    by_name = {o.variation: o for o in outcomes if o.ran}
    if len(by_name) < 2:
        return "unknown"

    def is_blocked(name: str) -> bool | None:
        outcome = by_name.get(name)
        return None if outcome is None else outcome.blocked

    clean_private = is_blocked("signed_out_private")
    # The cleanest slate blocked → nothing cookie- or account-scoped explains it.
    if clean_private:
        return "ip_device"

    blocked_names = {name for name, o in by_name.items() if o.blocked}
    if not blocked_names:
        return "none"

    regular = {"signed_in_regular", "signed_out_regular"} & set(by_name)
    signed_in = {"signed_in_regular", "signed_in_private"} & set(by_name)
    regular_blocked = regular and all(by_name[n].blocked for n in regular)
    signed_in_blocked = signed_in and all(by_name[n].blocked for n in signed_in)
    signed_out_private_ok = clean_private is False

    # Account flag: everything carrying auth is blocked while signed-out runs are fine.
    if signed_in_blocked and not by_name.get(
        "signed_out_regular", VariationOutcome("", ran=False)
    ).blocked and signed_out_private_ok:
        return "account"
    # Profile poison: both runs on the real profile's cookies are blocked, private is fine.
    if regular_blocked:
        return "profile"
    # Partial/contradictory (e.g. only signed_in_regular blocked): the profile is the
    # common denominator we can act on.
    if "signed_in_regular" in blocked_names or "signed_out_regular" in blocked_names:
        return "profile"
    return "unknown"


def run_variation_matrix(
    config: MonitorConfig,
    event_url: str | None = None,
    event_id: str = "variation-probe",
) -> VariationReport:
    """Run all four variations sequentially and return the scope report.

    Each variation is a fresh, short-lived BrowserProbe (own Chrome instance) so the
    live monitor browser and profile are never disturbed. Individual variation
    failures are recorded (ran=False) and don't abort the matrix.
    """
    if event_url is None:
        if not config.events:
            raise ValueError("No events configured and no event_url override given")
        event_url = config.events[0].url

    outcomes: list[VariationOutcome] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="tm_variation_probe_"))
    try:
        profile_copy = tmp_root / "profile"
        profile_ready = _copy_profile(Path(config.browser_user_data_dir), profile_copy)

        empty_state = tmp_root / "empty_storage_state.json"
        empty_state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        plans = [
            # (variation, session_mode, user_data_dir, storage_state, strip_auth, shed_bot_cookies)
            ("signed_in_regular", "persistent_profile", profile_copy, None, False, False),
            ("signed_out_regular", "persistent_profile", profile_copy, None, True, False),
            ("signed_in_private", "storage_state", None, config.browser_storage_state_path, False, True),
            ("signed_out_private", "storage_state", None, str(empty_state), False, False),
        ]
        for variation, mode, user_data_dir, storage_state, strip_auth, shed_bot in plans:
            if mode == "persistent_profile" and not profile_ready:
                outcomes.append(
                    VariationOutcome(variation, ran=False, error="profile copy unavailable")
                )
                continue
            if (
                storage_state
                and storage_state != str(empty_state)
                and not Path(storage_state).exists()
            ):
                outcomes.append(
                    VariationOutcome(variation, ran=False, error="storage state missing")
                )
                continue
            outcomes.append(
                _run_variation(
                    config,
                    variation=variation,
                    session_mode=mode,
                    user_data_dir=str(user_data_dir) if user_data_dir else "",
                    storage_state_path=str(storage_state) if storage_state else str(empty_state),
                    strip_auth_cookies=strip_auth,
                    shed_bot_cookies=shed_bot,
                    event_id=event_id,
                    event_url=event_url,
                )
            )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    scope = interpret_matrix(outcomes)
    report = VariationReport(
        at=datetime.now(timezone.utc).isoformat(),
        event_url=event_url,
        scope=scope,
        outcomes=outcomes,
    )
    logger.warning(
        "Variation probe verdict: scope=%s (%s)",
        scope,
        ", ".join(
            f"{o.variation}={'ERR' if not o.ran else 'BLOCKED' if o.blocked else 'ok'}"
            for o in outcomes
        ),
    )
    return report


def _copy_profile(source: Path, destination: Path) -> bool:
    if not source.is_dir():
        logger.info("Variation probe: no persistent profile at %s", source)
        return False
    try:
        shutil.copytree(source, destination, ignore=_PROFILE_COPY_IGNORES)
        # Chrome refuses to reuse a profile it thinks is running elsewhere.
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock = destination / lock_name
            if lock.exists() or lock.is_symlink():
                lock.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("Variation probe: profile copy failed: %s", exc)
        return False


def _strip_auth_cookies(context) -> None:
    """Drop everything except bot-management cookies, turning a signed-in profile
    context into 'same fingerprint, signed out'."""
    cookies = context.cookies()
    keep = [
        c
        for c in cookies
        if str(c.get("name", "")).lower().startswith(_BOT_TRUST_COOKIE_PREFIXES)
    ]
    context.clear_cookies()
    if keep:
        context.add_cookies(keep)


def _run_variation(
    config: MonitorConfig,
    *,
    variation: str,
    session_mode: str,
    user_data_dir: str,
    storage_state_path: str,
    strip_auth_cookies: bool,
    shed_bot_cookies: bool,
    event_id: str,
    event_url: str,
) -> VariationOutcome:
    probe = BrowserProbe(
        storage_state_path=storage_state_path,
        session_mode=session_mode,
        user_data_dir=user_data_dir,
        channel=config.browser_channel,
        headless=config.browser_headless,
        navigation_timeout_seconds=config.browser_navigation_timeout_seconds,
        stealth_enabled=config.browser_stealth_enabled,
        locale=config.browser_locale,
        timezone_id=config.browser_timezone_id,
        event_dwell_min_seconds=2,
        event_dwell_max_seconds=4,
        # No homepage warmup detour — each variation should hit the event page the
        # same way so the four results are comparable.
        homepage_warmup_interval_seconds=0,
    )
    try:
        probe.start()
        if strip_auth_cookies and probe._context is not None:
            _strip_auth_cookies(probe._context)
        if shed_bot_cookies:
            # signed_in_private tests "clean bot slate + auth": the storage state may
            # carry an old (possibly poisoned) DataDome token — shed it.
            probe.clear_block_cookies()
        result = probe.check_event(event_id, event_url)
        return VariationOutcome(
            variation=variation,
            ran=True,
            blocked=bool(result.blocked or result.challenge_detected),
            challenge=bool(result.challenge_detected),
            abck_trusted=bool(result.abck_trusted),
            abck_flagged=bool(result.abck_flagged),
        )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never raise
        logger.warning("Variation probe %s failed: %s", variation, exc)
        return VariationOutcome(variation=variation, ran=False, error=str(exc)[:200])
    finally:
        try:
            probe.close()
        except Exception:  # pragma: no cover - defensive
            pass


def main() -> int:
    """Standalone run: `python -m src.variation_probe [event_url]` — prints the report."""
    import sys

    from .config import ConfigError, load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc)
        return 1
    override = sys.argv[1] if len(sys.argv) > 1 else None
    report = run_variation_matrix(config, event_url=override)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
