"""Lightweight public-egress (IP + ASN) lookup, used only for visibility/logging.

The monitor cannot change its egress IP, but knowing which IP/network it is on — and how
Akamai is likely to classify it — makes the health record interpretable: it tells you
whether a bad stretch is tied to one home IP, and whether the ISP silently handed out a
new one after a router reboot. This is diagnostic only; it never changes behavior.

IMPORTANT (macOS fork safety): the lookup runs via a `curl` subprocess, NOT an in-process
`urllib`/`requests` call. Making an HTTP call in-process loads Apple's Network.framework,
whose atfork handlers are not fork-safe — and the monitor forks subprocesses constantly
(Playwright's driver/browser). A network call in-process followed by that fork segfaults
the child ("Python quit unexpectedly"). Doing the fetch in an exec'd `curl` child keeps
Network.framework out of this process entirely, so the later Playwright fork stays safe.

Called sparingly (startup / browser recycle) and cached, so it adds no meaningful request
volume and never touches Ticketmaster. Fails soft — any error yields an "unknown" record.
"""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

# ip-api.com is free for low volume and returns ASN + mobile/hosting/proxy flags, which
# is exactly the reputation signal Akamai keys on (mobile/CGNAT = trusted, hosting = not).
# Its free tier is plain HTTP only — and iCloud Private Relay proxies ALL unencrypted
# HTTP through Fastly, which made the self-lookup report Fastly's CDN IP instead of the
# real egress. So: resolve the true IP over HTTPS first (Private Relay leaves HTTPS from
# non-Safari apps alone), then ask ip-api about that explicit IP.
_IP_URL = "https://api.ipify.org"
_LOOKUP_URL_TEMPLATE = "http://ip-api.com/json/{ip}?fields=status,query,as,isp,mobile,proxy,hosting"
_LOOKUP_URL_FALLBACK = "http://ip-api.com/json/?fields=status,query,as,isp,mobile,proxy,hosting"

_cache: dict | None = None


def _classify(data: dict) -> str:
    if data.get("mobile"):
        return "mobile"  # carrier CGNAT — highest baseline Akamai trust
    if data.get("hosting") or data.get("proxy"):
        return "datacenter"  # VPN/hosting — lowest trust
    return "residential"


def _curl(url: str, timeout: float) -> str | None:
    """Fetch a URL via a curl subprocess (fork-safe on macOS). Never raises."""
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), url],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except Exception as exc:  # curl missing, timeout, etc.
        logger.debug("egress curl failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _fetch(timeout: float) -> dict | None:
    """Resolve the true egress IP over HTTPS, then classify it via ip-api."""
    ip = _curl(_IP_URL, timeout)
    if ip and all(part.isdigit() for part in ip.split(".")) and ip.count(".") == 3:
        url = _LOOKUP_URL_TEMPLATE.format(ip=ip)
    else:
        url = _LOOKUP_URL_FALLBACK
    raw = _curl(url, timeout)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def get_egress(*, timeout: float = 4.0, force: bool = False) -> dict:
    """Return {ip, asn, isp, kind, ok}. Cached for the process lifetime; never raises."""
    global _cache
    if _cache is not None and not force:
        return _cache

    record = {"ip": None, "asn": None, "isp": None, "kind": "unknown", "ok": False}
    data = _fetch(timeout)
    if data and data.get("status") == "success":
        record = {
            "ip": data.get("query"),
            "asn": data.get("as"),
            "isp": data.get("isp"),
            "kind": _classify(data),
            "ok": True,
        }

    _cache = record
    return record


def describe(record: dict) -> str:
    """One-line human summary for logs/UI, e.g. '73.x — Comcast (residential)'."""
    if not record.get("ok"):
        return "unknown"
    return f"{record.get('ip')} — {record.get('isp')} ({record.get('kind')})"
