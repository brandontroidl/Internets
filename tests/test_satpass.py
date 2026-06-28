"""Tests for the N2YO satellite-pass module (key-gated, HTTP mocked)."""
from __future__ import annotations

import asyncio
from configparser import ConfigParser

import requests

import modules.satpass as sp


class FakeBot:
    def __init__(self, key: str = "") -> None:
        self.cfg = ConfigParser()
        self.cfg.read_dict({
            "bot": {"command_prefix": "."},
            "weather": {"user_agent": "t"},
            "satpass": {"n2yo_api_key": key},
        })
        self.out: list[str] = []

    def rate_limited(self, n): return False
    def notice(self, n, m): self.out.append(m)
    def privmsg(self, t, m): self.out.append(m)


def _mod(key: str) -> sp.SatpassModule:
    m = sp.SatpassModule(FakeBot(key))
    m.on_load()
    return m


def test_fetch_format(monkeypatch):
    monkeypatch.setattr(sp, "fetch_json", lambda url, **k: {
        "info": {"satname": "SPACE STATION"},
        "passes": [{"startUTC": 1700000000, "maxEl": 45, "duration": 600}]})
    out = sp._fetch(25544, 34.1, -117.8, "k", "ua")
    assert "SPACE STATION" in out and "max elevation 45" in out and "600s" in out


def test_fetch_no_passes(monkeypatch):
    monkeypatch.setattr(sp, "fetch_json", lambda url, **k: {"info": {"satname": "ISS"}, "passes": []})
    assert "no visible passes" in sp._fetch(25544, 0, 0, "k", "ua")


def test_fetch_error_friendly(monkeypatch):
    def boom(url, **k):
        raise requests.RequestException("x")
    monkeypatch.setattr(sp, "fetch_json", boom)
    assert "lookup failed" in sp._fetch(25544, 0, 0, "k", "ua")


def test_is_configured_gating():
    assert _mod("ABC").is_configured() is True
    assert _mod("").is_configured() is False


def test_no_key_message():
    b = FakeBot(""); m = sp.SatpassModule(b); m.on_load()
    asyncio.run(m.cmd_passes("n", "#c", "iss 34,-117"))
    assert any("n2yo_api_key" in x for x in b.out)


def test_bad_location(monkeypatch):
    monkeypatch.setattr(sp, "fetch_json", lambda u, **k: {"info": {}, "passes": []})
    b = FakeBot("ABC"); m = sp.SatpassModule(b); m.on_load()
    asyncio.run(m.cmd_passes("n", "#c", "iss notalatlon"))
    assert any("lat,lon" in x for x in b.out)


def test_unknown_sat():
    b = FakeBot("ABC"); m = sp.SatpassModule(b); m.on_load()
    asyncio.run(m.cmd_passes("n", "#c", "zzz 34,-117"))
    assert any("unknown satellite" in x for x in b.out)


def test_named_sat_resolves(monkeypatch):
    captured = {}
    def fake(url, **k):
        captured["url"] = url
        return {"info": {"satname": "ISS"}, "passes": []}
    monkeypatch.setattr(sp, "fetch_json", fake)
    b = FakeBot("ABC"); m = sp.SatpassModule(b); m.on_load()
    asyncio.run(m.cmd_passes("n", "#c", "iss 34.1,-117.8"))
    assert "/25544/" in captured["url"]   # iss -> NORAD 25544
