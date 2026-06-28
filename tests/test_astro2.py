"""Tests for modules/astro2.py.

Network helpers (_fetch_solar, _fetch_neo, _fetch_launches) are exercised
by monkeypatching the module-level ``fetch_json`` with canned responses —
no real network is hit.  Pure helpers (moon_phase / _moon, sky_lookup) are
tested directly.
"""

from datetime import datetime, timezone

import pytest

import modules.astro2 as astro2


# ── .solar ────────────────────────────────────────────────────────────
def test_solar_happy(monkeypatch):
    def fake(url, **kw):
        if "xray" in url:
            return [{"max_class": "M1.5", "max_time": "2026-06-22T10:00Z"}]
        return [{"ssn": 142}]
    monkeypatch.setattr(astro2, "fetch_json", fake)
    out = astro2._fetch_solar("UA/1.0")
    assert "M1.5" in out
    assert "2026-06-22T10:00Z" in out
    assert "SSN 142" in out


def test_solar_ssn_failure_is_nonfatal(monkeypatch):
    def fake(url, **kw):
        if "xray" in url:
            return [{"max_class": "C2.0", "max_time": "t"}]
        raise astro2.requests.RequestException("boom")
    monkeypatch.setattr(astro2, "fetch_json", fake)
    out = astro2._fetch_solar("UA/1.0")
    assert "C2.0" in out
    assert "SSN" not in out


def test_solar_request_error(monkeypatch):
    def fake(url, **kw):
        raise astro2.requests.RequestException("down")
    monkeypatch.setattr(astro2, "fetch_json", fake)
    assert astro2._fetch_solar("UA/1.0") == "solar lookup failed"


def test_solar_malformed(monkeypatch):
    monkeypatch.setattr(astro2, "fetch_json", lambda url, **kw: "not-a-dict")
    assert astro2._fetch_solar("UA/1.0") == "solar data unavailable"


# ── .neo ──────────────────────────────────────────────────────────────
def test_neo_happy(monkeypatch):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "near_earth_objects": {
            today: [
                {"name": "Far One",
                 "close_approach_data": [
                     {"miss_distance": {"kilometers": "5000000"}}]},
                {"name": "Close One",
                 "close_approach_data": [
                     {"miss_distance": {"kilometers": "120000"}}]},
            ]
        }
    }
    monkeypatch.setattr(astro2, "fetch_json", lambda url, **kw: payload)
    out = astro2._fetch_neo("DEMO_KEY", "UA/1.0")
    assert "2" in out
    assert "Close One" in out
    assert "120,000 km" in out


def test_neo_empty(monkeypatch):
    monkeypatch.setattr(astro2, "fetch_json",
                        lambda url, **kw: {"near_earth_objects": {}})
    out = astro2._fetch_neo("DEMO_KEY", "UA/1.0")
    assert "no near-earth objects" in out


def test_neo_request_error(monkeypatch):
    def fake(url, **kw):
        raise astro2.requests.RequestException("nope")
    monkeypatch.setattr(astro2, "fetch_json", fake)
    assert astro2._fetch_neo("DEMO_KEY", "UA/1.0") == "NEO lookup failed"


def test_neo_malformed(monkeypatch):
    monkeypatch.setattr(astro2, "fetch_json", lambda url, **kw: 12345)
    assert astro2._fetch_neo("DEMO_KEY", "UA/1.0") == "NEO data unavailable"


# ── .launches ─────────────────────────────────────────────────────────
def test_launches_happy(monkeypatch):
    payload = {
        "results": [
            {"name": "Falcon 9 | Starlink",
             "launch_service_provider": {"name": "SpaceX"},
             "net": "2026-06-23T12:00:00Z",
             "pad": {"name": "SLC-40"}},
        ]
    }
    captured = {}

    def fake(url, **kw):
        captured.update(kw.get("params", {}))
        return payload
    monkeypatch.setattr(astro2, "fetch_json", fake)
    out = astro2._fetch_launches(1, "UA/1.0")
    assert "Falcon 9 | Starlink" in out
    assert "SpaceX" in out
    assert "SLC-40" in out
    assert captured.get("limit") == 1


def test_launches_clamped(monkeypatch):
    captured = {}

    def fake(url, **kw):
        captured.update(kw.get("params", {}))
        return {"results": []}
    monkeypatch.setattr(astro2, "fetch_json", fake)
    astro2._fetch_launches(99, "UA/1.0")
    assert captured.get("limit") == 3


def test_launches_empty(monkeypatch):
    monkeypatch.setattr(astro2, "fetch_json", lambda url, **kw: {"results": []})
    assert astro2._fetch_launches(1, "UA/1.0") == "no upcoming launches found"


def test_launches_request_error(monkeypatch):
    def fake(url, **kw):
        raise astro2.requests.RequestException("x")
    monkeypatch.setattr(astro2, "fetch_json", fake)
    assert astro2._fetch_launches(1, "UA/1.0") == "launch lookup failed"


def test_launches_malformed(monkeypatch):
    monkeypatch.setattr(astro2, "fetch_json", lambda url, **kw: [])
    assert astro2._fetch_launches(1, "UA/1.0") == "launch data unavailable"


# ── .moon (pure) ──────────────────────────────────────────────────────
def test_moon_known_new_moon():
    # 2000-01-06 is the reference new moon.
    dt = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    out = astro2.moon_phase(dt)
    assert "New Moon" in out
    assert "% illuminated" in out
    assert "days old" in out


def test_moon_full_moon():
    # ~half a synodic month after the reference new moon -> Full.
    dt = datetime(2000, 1, 21, 12, 0, tzinfo=timezone.utc)
    out = astro2.moon_phase(dt)
    assert "Full Moon" in out


def test_moon_command_parses_date():
    out = astro2._moon("2024-12-25")
    assert "Moon 2024-12-25:" in out


def test_moon_command_today():
    out = astro2._moon(None)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"Moon {today}:" in out


def test_moon_command_bad_date():
    assert astro2._moon("not-a-date") == "usage: .moon [YYYY-MM-DD]"


# ── .sky (pure) ───────────────────────────────────────────────────────
def test_sky_by_number():
    out = astro2.sky_lookup("M31")
    assert "M31" in out
    assert "Andromeda Galaxy" in out
    assert "Spiral galaxy" in out
    assert "mag 3.4" in out


def test_sky_by_bare_number():
    out = astro2.sky_lookup("42")
    assert "M42" in out
    assert "Orion Nebula" in out


def test_sky_by_name():
    out = astro2.sky_lookup("Pleiades")
    assert "M45" in out
    assert "Pleiades" in out


def test_sky_not_found():
    out = astro2.sky_lookup("M999")
    assert "no Messier object" in out


def test_sky_empty():
    out = astro2.sky_lookup("")
    assert out.startswith("usage:")


def test_sky_unnamed_object():
    out = astro2.sky_lookup("M2")
    assert "M2" in out
    assert "Globular cluster" in out
    # No common name -> no parenthetical.
    assert "M2 (" not in out
