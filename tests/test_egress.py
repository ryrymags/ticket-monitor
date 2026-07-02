from __future__ import annotations

from src.egress import _classify, describe


class TestClassify:
    def test_mobile_is_trusted_class(self):
        assert _classify({"mobile": True}) == "mobile"

    def test_hosting_or_proxy_is_datacenter(self):
        assert _classify({"hosting": True}) == "datacenter"
        assert _classify({"proxy": True}) == "datacenter"

    def test_plain_home_is_residential(self):
        assert _classify({"mobile": False, "hosting": False, "proxy": False}) == "residential"


class TestDescribe:
    def test_unknown_when_not_ok(self):
        assert describe({"ok": False}) == "unknown"

    def test_summary_when_ok(self):
        record = {"ok": True, "ip": "73.1.2.3", "isp": "Comcast", "kind": "residential"}
        assert describe(record) == "73.1.2.3 — Comcast (residential)"
