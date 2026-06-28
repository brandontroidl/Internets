"""Tests for the shared SSRF-safe fetch (modules._netsafe).

getaddrinfo is monkeypatched so checks are deterministic and offline.  The
core guarantees: block all non-public ranges (incl IPv4-mapped-IPv6),
reject if ANY DNS answer is private (rebinding defense), and re-validate +
re-pin every redirect hop so a 3xx to an internal host is refused.
"""
from __future__ import annotations

import ipaddress
import socket

import pytest

import modules._netsafe as ns


class TestIpBlocked:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254",
        "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1", "0.0.0.0",
    ])
    def test_blocked(self, ip):
        assert ns.ip_is_blocked(ipaddress.ip_address(ip))

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
    def test_allowed(self, ip):
        assert not ns.ip_is_blocked(ipaddress.ip_address(ip))


class TestResolveSafeIp:
    def test_literal_internal_blocked(self):
        assert ns.resolve_safe_ip("127.0.0.1") is None
        assert ns.resolve_safe_ip("::ffff:10.0.0.1") is None   # mapped-address bypass
        assert ns.resolve_safe_ip("169.254.169.254") is None

    def test_literal_public_ok(self):
        assert ns.resolve_safe_ip("8.8.8.8") == "8.8.8.8"

    def test_metadata_host_blocked(self):
        assert ns.resolve_safe_ip("metadata.google.internal") is None

    def test_any_private_answer_blocks(self, monkeypatch):
        # one public + one private answer -> reject (DNS rebinding defense)
        monkeypatch.setattr(ns, "_orig_getaddrinfo", lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))])
        assert ns.resolve_safe_ip("rebind.test") is None

    def test_all_public_picks_first(self, monkeypatch):
        monkeypatch.setattr(ns, "_orig_getaddrinfo", lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
        assert ns.resolve_safe_ip("ok.test") == "93.184.216.34"


class TestSafeOpen:
    def test_internal_literal_raises(self):
        with pytest.raises(ns.SSRFBlocked):
            with ns.safe_open("GET", "http://127.0.0.1/x", "ua"):
                pass

    def test_bad_scheme_raises(self):
        with pytest.raises(ns.SSRFBlocked):
            with ns.safe_open("GET", "file:///etc/passwd", "ua"):
                pass

    def test_redirect_to_internal_blocked(self, monkeypatch):
        # public host -> 302 -> metadata IP; safe_open must re-validate the hop.
        monkeypatch.setattr(ns, "_orig_getaddrinfo", lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("93.184.216.34" if host == "example.com" else "169.254.169.254", 0))])

        class Resp:
            is_redirect = True
            is_permanent_redirect = False
            headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
            def close(self): pass

        class Sess:
            def mount(self, *a): pass
            def request(self, *a, **k): return Resp()
            def close(self): pass

        monkeypatch.setattr(ns.requests, "Session", lambda: Sess())
        with pytest.raises(ns.SSRFBlocked):
            with ns.safe_open("GET", "https://example.com/a", "ua"):
                pass
