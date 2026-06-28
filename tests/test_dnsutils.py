"""Tests for modules/dnsutils.py.

Every test monkeypatches ``modules.dnsutils.fetch_json`` with a canned
responder so nothing ever hits the real network.  We assert the
formatting of each command's ``_*_sync`` helper and the pure helpers
(happy path + not-found + malformed).
"""

import sys

sys.path.insert(0, ".")

import pytest

import modules.dnsutils as dns
from modules.dnsutils import (
    _answers, _reverse_name, _rdap_registrar, _rdap_event,
    _rdap_nameservers, _dns_sync, _rdns_sync, _caa_sync, _whois_sync,
    _asn_sync,
)

UA = "Internets/1.0-test"


def _patch(monkeypatch, responder):
    """Replace fetch_json with a callable(url, **kw) -> responder(url, kw)."""
    def fake(url, **kw):
        return responder(url, kw)
    monkeypatch.setattr(dns, "fetch_json", fake)


# ── pure helpers ──────────────────────────────────────────────────────────
class TestAnswers:
    def test_extracts_data(self):
        data = {"Answer": [{"type": 1, "data": "1.2.3.4"},
                           {"type": 1, "data": "5.6.7.8"}]}
        assert _answers(data, "A") == ["1.2.3.4", "5.6.7.8"]

    def test_type_filter(self):
        data = {"Answer": [{"type": 5, "data": "cname.example.com."},
                           {"type": 1, "data": "1.2.3.4"}]}
        assert _answers(data, "A") == ["1.2.3.4"]

    def test_no_filter_returns_all(self):
        data = {"Answer": [{"type": 5, "data": "x."}, {"type": 1, "data": "y"}]}
        assert _answers(data) == ["x.", "y"]

    def test_empty_and_malformed(self):
        assert _answers({}) == []
        assert _answers(None) == []
        assert _answers({"Answer": [None, "junk", {"type": 1}]}, "A") == []

    def test_strips_control_chars(self):
        data = {"Answer": [{"type": 16, "data": "v=spf1\r\ninjected"}]}
        out = _answers(data, "TXT")
        assert "\r" not in out[0] and "\n" not in out[0]


class TestReverseName:
    def test_ipv4(self):
        assert _reverse_name("8.8.8.8") == "8.8.8.8.in-addr.arpa"

    def test_ipv6(self):
        out = _reverse_name("2001:4860:4860::8888")
        assert out.endswith(".ip6.arpa")

    def test_invalid(self):
        assert _reverse_name("not-an-ip") is None
        assert _reverse_name("999.999.999.999") is None


class TestRdapHelpers:
    def test_registrar_from_vcard(self):
        entities = [{"roles": ["registrar"],
                     "vcardArray": ["vcard", [["fn", {}, "text", "MarkMonitor"]]]}]
        assert _rdap_registrar(entities) == "MarkMonitor"

    def test_registrar_handle_fallback(self):
        entities = [{"roles": ["registrar"], "handle": "292"}]
        assert _rdap_registrar(entities) == "292"

    def test_registrar_none(self):
        assert _rdap_registrar([{"roles": ["technical"]}]) == ""
        assert _rdap_registrar("junk") == ""

    def test_event(self):
        events = [{"eventAction": "registration", "eventDate": "1997-09-15T04:00:00Z"},
                  {"eventAction": "expiration", "eventDate": "2028-09-14T04:00:00Z"}]
        assert _rdap_event(events, "registration") == "1997-09-15"
        assert _rdap_event(events, "expiration") == "2028-09-14"
        assert _rdap_event(events, "missing") == ""
        assert _rdap_event("junk", "registration") == ""

    def test_nameservers(self):
        ns = [{"ldhName": "NS1.GOOGLE.COM"}, {"ldhName": "NS2.GOOGLE.COM"}, {}]
        assert _rdap_nameservers(ns) == ["ns1.google.com", "ns2.google.com"]
        assert _rdap_nameservers(None) == []


# ── .dns ──────────────────────────────────────────────────────────────────
class TestDns:
    def test_happy_a(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {
            "Status": 0, "Answer": [{"type": 1, "data": "93.184.216.34"}]})
        out = _dns_sync("example.com", "A", UA)
        assert "example.com" in out and "A:" in out and "93.184.216.34" in out

    def test_mx(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {
            "Status": 0, "Answer": [{"type": 15, "data": "10 mail.example.com."}]})
        out = _dns_sync("example.com", "mx", UA)
        assert "MX:" in out and "mail.example.com" in out

    def test_no_records(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {"Status": 0, "Answer": []})
        out = _dns_sync("example.com", "AAAA", UA)
        assert "no AAAA records" in out

    def test_nxdomain(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {"Status": 3})
        out = _dns_sync("nope.invalid", "A", UA)
        assert "NXDOMAIN" in out

    def test_invalid_host(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert _dns_sync("bad host!", "A", UA) == "invalid host"

    def test_unknown_type(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert "unknown type" in _dns_sync("example.com", "ZZZ", UA)

    def test_malformed_raises_handled(self, monkeypatch):
        def boom(u, kw):
            raise ValueError("bad json")
        _patch(monkeypatch, boom)
        assert _dns_sync("example.com", "A", UA) == "lookup failed"

    def test_network_error_handled(self, monkeypatch):
        def boom(u, kw):
            raise RuntimeError("connection reset")
        _patch(monkeypatch, boom)
        assert _dns_sync("example.com", "A", UA) == "lookup failed"


# ── .rdns ─────────────────────────────────────────────────────────────────
class TestRdns:
    def test_happy(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {
            "Status": 0, "Answer": [{"type": 12, "data": "dns.google."}]})
        out = _rdns_sync("8.8.8.8", UA)
        assert "8.8.8.8" in out and "PTR:" in out and "dns.google" in out

    def test_no_ptr(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {"Status": 0, "Answer": []})
        assert "no PTR record" in _rdns_sync("203.0.113.1", UA)

    def test_invalid_ip(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert _rdns_sync("notanip", UA) == "invalid IP"

    def test_error_handled(self, monkeypatch):
        def boom(u, kw):
            raise RuntimeError("timeout")
        _patch(monkeypatch, boom)
        assert _rdns_sync("8.8.8.8", UA) == "lookup failed"


# ── .caa ──────────────────────────────────────────────────────────────────
class TestCaa:
    def test_happy_with_spf_dmarc(self, monkeypatch):
        def resp(url, kw):
            name = kw["params"]["name"]
            t = kw["params"]["type"]
            if t == "CAA":
                return {"Answer": [{"type": 257, "data": "0 issue \"letsencrypt.org\""}]}
            if name == "example.com" and t == "TXT":
                return {"Answer": [{"type": 16, "data": "v=spf1 -all"}]}
            if name == "_dmarc.example.com":
                return {"Answer": [{"type": 16, "data": "v=DMARC1; p=reject"}]}
            return {"Answer": []}
        _patch(monkeypatch, resp)
        out = _caa_sync("example.com", UA)
        assert "CAA:" in out and "letsencrypt" in out
        assert "SPF:" in out and "DMARC:" in out

    def test_no_caa(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {"Answer": []})
        out = _caa_sync("example.com", UA)
        assert "none" in out and "any CA" in out

    def test_invalid_domain(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert _caa_sync("bad domain!", UA) == "invalid domain"

    def test_spf_dmarc_errors_are_nonfatal(self, monkeypatch):
        def resp(url, kw):
            if kw["params"]["type"] == "CAA":
                return {"Answer": [{"type": 257, "data": "0 issue \"x\""}]}
            raise RuntimeError("txt lookup failed")
        _patch(monkeypatch, resp)
        out = _caa_sync("example.com", UA)
        assert "CAA:" in out  # still produced despite SPF/DMARC failures

    def test_caa_error_handled(self, monkeypatch):
        def boom(u, kw):
            raise RuntimeError("down")
        _patch(monkeypatch, boom)
        assert _caa_sync("example.com", UA) == "lookup failed"


# ── .whois ────────────────────────────────────────────────────────────────
def _rdap_domain():
    return {
        "entities": [{"roles": ["registrar"],
                      "vcardArray": ["vcard", [["fn", {}, "text", "RESERVED-IANA"]]]}],
        "events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2025-08-13T04:00:00Z"},
        ],
        "nameservers": [{"ldhName": "A.IANA-SERVERS.NET"},
                        {"ldhName": "B.IANA-SERVERS.NET"}],
        "status": ["client transfer prohibited"],
    }


class TestWhois:
    def test_happy(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: _rdap_domain())
        out = _whois_sync("example.com", UA)
        assert "registrar RESERVED-IANA" in out
        assert "created 1995-08-14" in out
        assert "expires 2025-08-13" in out
        assert "a.iana-servers.net" in out
        assert "client transfer prohibited" in out

    def test_not_found(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: None)  # allow_404 → None
        assert "no RDAP record" in _whois_sync("nope.invalid", UA)

    def test_empty_payload(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: {})
        out = _whois_sync("example.com", UA)
        assert "no detail fields" in out

    def test_invalid_domain(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert _whois_sync("bad domain!", UA) == "invalid domain"

    def test_malformed_handled(self, monkeypatch):
        def boom(u, kw):
            raise ValueError("nope")
        _patch(monkeypatch, boom)
        assert _whois_sync("example.com", UA) == "lookup failed"


# ── .asn ──────────────────────────────────────────────────────────────────
class TestAsn:
    def test_ip_happy(self, monkeypatch):
        seen = {}

        def resp(url, kw):
            seen["url"] = url
            return {"name": "GOGL", "handle": "GOGL", "country": "US",
                    "startAddress": "8.8.8.0", "endAddress": "8.8.8.255",
                    "type": "DIRECT ALLOCATION"}
        _patch(monkeypatch, resp)
        out = _asn_sync("8.8.8.8", UA)
        assert "/ip/8.8.8.8" in seen["url"]
        assert "GOGL" in out and "8.8.8.0" in out and "8.8.8.255" in out
        assert "US" in out

    def test_asn_input(self, monkeypatch):
        seen = {}

        def resp(url, kw):
            seen["url"] = url
            return {"name": "GOOGLE", "handle": "AS15169"}
        _patch(monkeypatch, resp)
        out = _asn_sync("AS15169", UA)
        assert "/autnum/15169" in seen["url"]
        assert "AS15169" in out and "GOOGLE" in out

    def test_not_found(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: None)
        assert "no RDAP record" in _asn_sync("203.0.113.5", UA)

    def test_invalid_input(self, monkeypatch):
        _patch(monkeypatch, lambda u, kw: pytest.fail("should not fetch"))
        assert "give an IP address or ASn" in _asn_sync("not valid!", UA)

    def test_error_handled(self, monkeypatch):
        def boom(u, kw):
            raise RuntimeError("rdap down")
        _patch(monkeypatch, boom)
        assert _asn_sync("8.8.8.8", UA) == "lookup failed"


# ── module surface ────────────────────────────────────────────────────────
class TestModuleSurface:
    def test_commands_map(self):
        assert set(dns.DnsutilsModule.COMMANDS) == {
            "dns", "rdns", "caa", "whois", "asn"}

    def test_help_lines(self):
        class FakeBot:
            pass
        m = dns.DnsutilsModule(FakeBot())
        lines = m.help_lines(".")
        assert len(lines) == 5
        assert all(isinstance(s, str) for s in lines)
        assert m.is_configured() is True
