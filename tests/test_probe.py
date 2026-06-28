"""Tests for the SSRF-guarded network probers (modules.probe).

The security-critical part is base.resolve_public refusing any non-public
address; getaddrinfo is monkeypatched so the assertions are deterministic
and offline.  Command parsing is exercised with localhost (which always
resolves to loopback -> must be refused) and a mocked requests response.
"""
from __future__ import annotations

import socket

import pytest

from modules.base import resolve_public
import modules.probe as probe


def _patch_getaddrinfo(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))])


class TestSSRFGuard:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
        "169.254.169.254",          # cloud metadata
        "::1", "fe80::1", "224.0.0.1", "0.0.0.0", "240.0.0.1",
    ])
    def test_refuses_non_public(self, monkeypatch, ip):
        _patch_getaddrinfo(monkeypatch, ip)
        with pytest.raises(ValueError):
            resolve_public("evil.test")

    def test_accepts_public(self, monkeypatch):
        _patch_getaddrinfo(monkeypatch, "93.184.216.34")
        assert resolve_public("example.com")

    def test_empty_and_oversize(self):
        with pytest.raises(ValueError):
            resolve_public("")
        with pytest.raises(ValueError):
            resolve_public("a" * 300)

    def test_unresolvable(self, monkeypatch):
        def boom(*a, **k):
            raise socket.gaierror("no such host")
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(ValueError):
            resolve_public("nonexistent.invalid")


class TestProbersRefuseInternal:
    # localhost always resolves to loopback -> every prober must refuse it,
    # without a mock (proves the guard is wired into each command path).
    def test_tcp_localhost_refused(self):
        out = probe._tcp("localhost", "22")
        assert "refusing" in out or "invalid" in out or "resolve" in out

    def test_ssl_localhost_refused(self):
        assert "refusing" in probe._ssl_cert("localhost")

    def test_headers_localhost_refused(self):
        assert "refusing" in probe._headers("http://localhost/", "ua")

    def test_down_localhost_refused(self):
        assert "refusing" in probe._down("localhost", "ua")

    def test_tcp_bad_port(self):
        assert "port" in probe._tcp("example.com", "99999")


class TestHeadersParsing:
    def test_headers_format(self, monkeypatch):
        # _headers now fetches via _netsafe.safe_open; mock that context manager.
        from contextlib import contextmanager

        class Resp:
            status_code = 301
            is_redirect = True
            is_permanent_redirect = False
            headers = {
                "Server": "nginx", "Content-Type": "text/html; charset=utf-8",
                "Location": "https://example.com/", "Strict-Transport-Security": "max-age=1",
            }

        @contextmanager
        def fake_open(method, url, ua, **kw):
            yield Resp()
        monkeypatch.setattr(probe, "safe_open", fake_open)
        out = probe._headers("http://example.com", "ua")
        assert "HTTP 301" in out and "nginx" in out and "HSTS" in out and "-> https://example.com/" in out
