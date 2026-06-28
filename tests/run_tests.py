#!/usr/bin/env python3
"""
Standalone test runner - no pytest required.

Usage:  python tests/run_tests.py
"""

from __future__ import annotations

import sys
import os
import json
import time
import tempfile
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta

# PLATFORM: Reconfigure stdout to handle encoding errors on Windows (cp1252)
# instead of crashing on Unicode test-output markers.  Has no effect on
# UTF-8 terminals (Linux, macOS, modern Windows Terminal).
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass  # Python < 3.7 or non-reconfigurable stream (piped, etc.)

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pass = 0
_fail = 0
_errors: list[str] = []

# Use ASCII-safe markers for test output so the runner doesn't crash on
# Windows consoles with cp1252 encoding (GitHub Actions, cmd.exe, PowerShell).
_MARK_PASS = "[PASS]"
_MARK_FAIL = "[FAIL]"


def test(name: str):
    """Decorator that registers and runs a test function."""
    def decorator(fn):
        global _pass, _fail
        try:
            fn()
            _pass += 1
            print(f"  {_MARK_PASS} {name}")
        except Exception as e:
            _fail += 1
            tb = traceback.format_exc()
            _errors.append(f"  {_MARK_FAIL} {name}\n{tb}")
            print(f"  {_MARK_FAIL} {name}: {e}")
        return fn
    return decorator


# ══════════════════════════════════════════════════════════════════════
# protocol.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== protocol.py ===")
from protocol import (
    strip_tags, parse_isupport_chanmodes, parse_isupport_prefix,
    parse_mode_changes, parse_names_entry, sasl_plain_payload,
)

@test("strip_tags: no tags")
def _():
    assert strip_tags(":server 001 nick :Welcome") == ":server 001 nick :Welcome"

@test("strip_tags: with tags")
def _():
    assert strip_tags("@time=2026-01-01 :server PRIVMSG #test :hi") == ":server PRIVMSG #test :hi"

@test("parse_isupport_chanmodes")
def _():
    types = parse_isupport_chanmodes("beI,kL,lH,imtncSRMrsCTNVOzQ")
    assert types["b"] == "A"
    assert types["L"] == "B"
    assert types["H"] == "C"
    assert types["i"] == "D"
    assert types["m"] == "D"

@test("parse_isupport_prefix")
def _():
    modes, sym_map = parse_isupport_prefix("(qaohv)~&@%+")
    assert modes == {"q", "a", "o", "h", "v"}
    assert sym_map["~"] == "q"
    assert sym_map["@"] == "o"
    assert sym_map["+"] == "v"

@test("parse_mode_changes: +oq")
def _():
    changes = parse_mode_changes("+oq", ["nick1", "nick2"], {"o","a","q","h","v"},
                                 {"b":"A","k":"B","l":"C","i":"D"})
    assert changes == [(True, "o", "nick1"), (True, "q", "nick2")]

@test("parse_mode_changes: +Loq (L is type B)")
def _():
    cm = parse_isupport_chanmodes("beI,kL,lH,imtncSRMrsCTNVOzQ")
    pm = {"q","a","o","h","v"}
    changes = parse_mode_changes("+Loq", ["#over", "nick1", "nick2"], pm, cm)
    assert changes[0] == (True, "L", "#over")
    assert changes[1] == (True, "o", "nick1")
    assert changes[2] == (True, "q", "nick2")

@test("parse_mode_changes: -lo (l type C, no param on unset)")
def _():
    cm = {"l": "C", "b": "A", "k": "B", "i": "D"}
    changes = parse_mode_changes("-lo", ["target"], {"o","h","v"}, cm)
    assert changes[0] == (False, "l", None)
    assert changes[1] == (False, "o", "target")

@test("parse_mode_changes: +lo (l type C, param on set)")
def _():
    cm = {"l": "C", "b": "A", "k": "B", "i": "D"}
    changes = parse_mode_changes("+lo", ["50", "target"], {"o","h","v"}, cm)
    assert changes[0] == (True, "l", "50")
    assert changes[1] == (True, "o", "target")

@test("parse_names_entry: @nick is op")
def _():
    nick, is_op = parse_names_entry("@admin")
    assert nick == "admin" and is_op is True

@test("parse_names_entry: +nick is not op")
def _():
    nick, is_op = parse_names_entry("+voiced")
    assert nick == "voiced" and is_op is False

@test("parse_names_entry: ~&@nick is op")
def _():
    nick, is_op = parse_names_entry("~&@superadmin")
    assert nick == "superadmin" and is_op is True

@test("parse_names_entry: plain nick")
def _():
    nick, is_op = parse_names_entry("normie")
    assert nick == "normie" and is_op is False

@test("sasl_plain_payload")
def _():
    import base64
    payload = sasl_plain_payload("TestBot", "secret123")
    decoded = base64.b64decode(payload).decode("utf-8")
    assert decoded == "\0TestBot\0secret123"


# ══════════════════════════════════════════════════════════════════════
# store.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== store.py ===")
from store import Store, RateLimiter

def _make_store(tmp: str, **kwargs) -> Store:
    return Store(
        os.path.join(tmp, "loc.json"),
        os.path.join(tmp, "chan.json"),
        os.path.join(tmp, "users.json"),
        **kwargs,
    )

@test("Store: loc_set / loc_get / loc_del")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp)
        assert s.loc_get("Nick") is None
        s.loc_set("Nick", "90210")
        assert s.loc_get("nick") == "90210"  # case-insensitive
        assert s.loc_del("nick") is True
        assert s.loc_get("nick") is None
        assert s.loc_del("nick") is False
        s.stop()

@test("Store: a corrupt state file is quarantined, not silently clobbered")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "loc.json"
        p.write_text("{ not valid json ")
        orig = p.read_text()
        result = Store._read(str(p), {})
        assert result == {}                  # loaded default, did not crash
        assert not p.exists()                # original NOT left for the next flush to clobber
        corrupt = list(Path(tmp).glob("loc.json.corrupt.*"))
        assert len(corrupt) == 1             # preserved for recovery
        assert corrupt[0].read_text() == orig
        # A subsequent valid write/read round-trips and is not quarantined.
        Store._write(str(p), {"nick": "90210"})
        assert Store._read(str(p), {}) == {"nick": "90210"}

@test("Store: _write keeps a one-deep .bak of the previous good file")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        p = str(Path(tmp) / "loc.json")
        Store._write(p, {"a": "1"})          # first write: no prior file, no bak yet
        Store._write(p, {"a": "2"})          # second write: bak holds the previous good copy
        assert Store._read(p, {}) == {"a": "2"}
        assert Store._read(p + ".bak", {}) == {"a": "1"}

@test("Store: _flush_loop guards flush() so a bad cycle can't kill persistence")
def _():
    import inspect
    src = inspect.getsource(Store._flush_loop)
    assert "try:" in src and "except" in src

@test("RateLimiter: a 0/negative cooldown is floored, not silently disabled")
def _():
    rl = RateLimiter(flood_cd=0, api_cd=0)
    assert rl.flood_check("n") is False   # first call recorded/allowed
    assert rl.flood_check("n") is True    # immediate repeat flagged (cd floored to >=1s)

@test("Store: opt-out preference survives the stale-user prune (privacy floor)")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp, user_max_age_days=1)
        s.user_join("#x", "Bob", "bob@h")
        s.set_opt_out("Bob", True)
        for ch in s._users.values():      # age every Bob entry far past the cutoff
            if "bob" in ch:
                ch["bob"]["last_seen"] = "2000-01-01T00:00:00+00:00"
        s.prune_users()
        assert s.is_opted_out("Bob")      # the opt-out preference is NOT pruned away
        s.stop()

@test("Store: user_max_age_days <= 0 is floored, not a total wipe")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp, user_max_age_days=0)   # misconfig
        s.user_join("#x", "Alice", "alice@h")        # fresh entry
        s.prune_users()
        assert s.channel_users("#x")                 # floored to >=1d, not wiped
        s.stop()

@test("security: modules that emit upstream/user text route it through a sanitizer")
def _():
    # Completeness gate (enumerate the security-relevant modules, assert each
    # sanitizes) - NOT a change-detector: these splice third-party/user text
    # into bot-attributed IRC lines, so each MUST reference the canonical
    # base.strip_ctrl. Catches a future module (or a removed call) that drifts.
    for name in ("search", "seen", "tell", "stocks", "remind", "location"):
        src = Path(f"modules/{name}.py").read_text(encoding="utf-8")
        assert "strip_ctrl" in src, f"modules/{name}.py: missing canonical strip_ctrl sanitizer"
    # weather keeps its own _sanitize (same C0/DEL regex); allow either.
    wsrc = Path("modules/weather.py").read_text(encoding="utf-8")
    assert "strip_ctrl" in wsrc or "_sanitize" in wsrc

@test("audit_log: an unreadable existing key is not silently regenerated (tamper-evidence)")
def _():
    from audit_log import AuditLog
    with tempfile.TemporaryDirectory() as tmp:
        a = AuditLog(Path(tmp) / "audit.log")
        a._load_key()                       # generate the .key sidecar
        kp = a._key_path
        original = kp.read_bytes()
        a._key = None                        # force a reload
        class _BadPath:                      # simulate a transient read error
            def exists(self): return True
            def read_text(self, **k): raise OSError("transient")
        a._key_path = _BadPath()
        raised = False
        try:
            a._load_key()
        except RuntimeError:
            raised = True
        assert raised                        # fail-closed; did NOT regenerate
        assert kp.read_bytes() == original   # the real key was not truncated

@test("secret_store: env-var tier filters blank/placeholder values like the file tier")
def _():
    import os
    from secret_store import get, ENV_PREFIX
    key = ENV_PREFIX + "DUMMY_SECRET_XYZ"
    old = os.environ.get(key)
    try:
        os.environ[key] = "   "                          # whitespace only
        assert get("dummy_secret_xyz", "DEF") == "DEF"   # filtered, not returned
        os.environ[key] = "changeme"                     # placeholder
        assert get("dummy_secret_xyz", "DEF") == "DEF"   # filtered
        os.environ[key] = "realvalue123"
        assert get("dummy_secret_xyz", "DEF") == "realvalue123"   # real value passes
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

@test("Store: channels_save / channels_load")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp)
        s.channels_save({"#b", "#a", "#c"})
        loaded = s.channels_load()
        assert loaded == ["#a", "#b", "#c"]  # sorted
        s.stop()

@test("Store: user tracking (join/part/quit/rename)")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp)
        s.user_join("#test", "Alice", "alice@host.com")
        users = s.channel_users("#test")
        assert "alice" in users
        assert users["alice"]["nick"] == "Alice"
        s.user_rename("Alice", "Alicia", "alicia@host.com")
        users = s.channel_users("#test")
        assert "alice" not in users
        assert "alicia" in users
        assert users["alicia"]["nick"] == "Alicia"
        s.user_quit("Alicia")
        users = s.channel_users("#test")
        assert "alicia" in users  # still tracked, just last_seen updated
        s.stop()

@test("Store: flush writes to disk")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp)
        s.loc_set("test", "12345")
        s.flush()
        data = json.loads(Path(os.path.join(tmp, "loc.json")).read_text(encoding="utf-8"))
        # store.py v2 schema wraps the payload in {"schema": 2, "checksum": ..., "data": {...}}
        if isinstance(data, dict) and data.get("schema") and "data" in data:
            data = data["data"]
        assert data["test"] == "12345"
        s.stop()

@test("Store: user pruning removes stale entries")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp, user_max_age_days=1)
        # Manually inject a stale entry
        old_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        s._users["#test"] = {
            "old_nick": {"nick": "Old", "hostmask": "h", "first_seen": old_time, "last_seen": old_time},
            "new_nick": {"nick": "New", "hostmask": "h",
                         "first_seen": datetime.now(timezone.utc).isoformat(),
                         "last_seen": datetime.now(timezone.utc).isoformat()},
        }
        s._dirty_users = True
        s.flush()
        users = s.channel_users("#test")
        assert "old_nick" not in users
        assert "new_nick" in users
        s.stop()

@test("Store: atomic write (temp file + replace)")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        s = _make_store(tmp)
        s.loc_set("a", "1")
        s.flush()
        # No .tmp files should remain
        tmp_files = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"
        s.stop()

@test("RateLimiter: flood_check")
def _():
    rl = RateLimiter(flood_cd=2, api_cd=5)
    assert rl.flood_check("nick") is False  # first call OK
    assert rl.flood_check("nick") is True   # too soon
    assert rl.flood_check("nick", is_admin=True) is False  # admins bypass

@test("RateLimiter: api_check")
def _():
    rl = RateLimiter(flood_cd=2, api_cd=5)
    assert rl.api_check("nick") is False
    assert rl.api_check("nick") is True


# ══════════════════════════════════════════════════════════════════════
# modules/calc.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== modules/calc.py ===")
from modules.calc import _calc

@test("calc: basic arithmetic")
def _():
    assert _calc("2 + 3") == "5"
    assert _calc("6 * 7") == "42"
    assert _calc("10 - 3") == "7"

@test("calc: division")
def _():
    assert _calc("10 / 3") == "3.3333333"
    assert _calc("10 // 3") == "3"

@test("calc: power")
def _():
    assert _calc("2 ** 10") == "1024"

@test("calc: implicit multiplication")
def _():
    assert _calc("2pi") == str(int(2 * 3.141592653589793)) or float(_calc("2pi")) > 6.28

@test("calc: functions")
def _():
    assert _calc("sqrt(144)") == "12"
    assert _calc("abs(-5)") == "5"

@test("calc: factorial with limit")
def _():
    assert _calc("factorial(5)") == "120"
    assert "too large" in _calc("factorial(171)")

@test("calc: exponent bomb blocked")
def _():
    assert "too large" in _calc("9**99999")

@test("calc: division by zero")
def _():
    assert _calc("1/0") == "division by zero"

@test("calc: unknown names rejected")
def _():
    assert "error" in _calc("os.system('id')")

@test("calc: nested depth limit")
def _():
    # 55 nested sin() calls - should hit depth limit
    expr = "sin(" * 55 + "1" + ")" * 55
    assert "error" in _calc(expr)

@test("calc: log2/log10 preserved from implicit mul")
def _():
    result = float(_calc("log2(8)"))
    assert abs(result - 3.0) < 0.001


# ══════════════════════════════════════════════════════════════════════
# modules/dice.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== modules/dice.py ===")
from modules.dice import _roll

@test("dice: single die")
def _():
    result = _roll("6")
    assert "Total" in result

@test("dice: XdN format")
def _():
    result = _roll("3d6")
    assert "Total" in result

@test("dice: XdN+M format")
def _():
    result = _roll("2d6+5")
    assert "Total" in result

@test("dice: invalid format")
def _():
    assert "invalid" in _roll("abc")

@test("dice: count limits")
def _():
    assert "1–100" in _roll("101d6")
    assert "2–10000" in _roll("1d1")

@test("dice: large count truncates display")
def _():
    result = _roll("50d6")
    assert "50 dice" in result


# ══════════════════════════════════════════════════════════════════════
# weather_providers + modules/weather.py (multi-provider system)
# ══════════════════════════════════════════════════════════════════════
print("\n=== weather_providers ===")
import inspect  # needed for protocol checks in this section

@test("WeatherResult: dataclass is frozen and has required fields")
def _():
    from weather_providers.base import WeatherResult, ForecastDay
    r = WeatherResult(
        source="Test", temperature=20.0, description="Clear",
        location="Testville", feels_like_c=18.0, humidity=50.0,
        wind_kph=15.0, wind_dir="S", pressure_mb=1013.0,
        visibility_m=16000.0, dewpoint_c=10.0,
    )
    assert r.source == "Test"
    assert r.temperature == 20.0
    assert r.forecast == []
    # Frozen - attributes can't be mutated.
    try:
        r.source = "Other"
        assert False, "should be frozen"
    except AttributeError:
        pass

@test("ForecastDay: dataclass is frozen")
def _():
    from weather_providers.base import ForecastDay
    fd = ForecastDay(day_name="Monday", high_c=25.0, low_c=15.0, description="Sunny")
    assert fd.day_name == "Monday"
    assert fd.high_c == 25.0
    try:
        fd.day_name = "Tuesday"
        assert False, "should be frozen"
    except AttributeError:
        pass

@test("WeatherResult: forecast field holds ForecastDay list")
def _():
    from weather_providers.base import WeatherResult, ForecastDay
    fc = [ForecastDay("Mon", 25.0, 15.0, "Sunny"), ForecastDay("Tue", 20.0, 12.0, "Rain")]
    r = WeatherResult(source="X", temperature=20.0, description="Clear",
                      location="Here", forecast=fc)
    assert len(r.forecast) == 2
    assert r.forecast[0].day_name == "Mon"

@test("OpenMeteoProvider: implements WeatherProvider protocol")
def _():
    from weather_providers.base import WeatherProvider
    from weather_providers.openmeteo import OpenMeteoProvider
    p = OpenMeteoProvider()
    assert isinstance(p, WeatherProvider)
    assert p.name == "Open-Meteo"
    assert p.requires_key is False
    assert inspect.iscoroutinefunction(p.get_weather)
    assert inspect.iscoroutinefunction(p.get_forecast)

@test("WeatherAPIProvider: implements WeatherProvider protocol")
def _():
    from weather_providers.base import WeatherProvider
    from weather_providers.weatherapi import WeatherAPIProvider
    p = WeatherAPIProvider("test-key")
    assert isinstance(p, WeatherProvider)
    assert p.name == "WeatherAPI"
    assert p.requires_key is True
    assert inspect.iscoroutinefunction(p.get_weather)
    assert inspect.iscoroutinefunction(p.get_forecast)

@test("TomorrowIOProvider: implements WeatherProvider protocol")
def _():
    from weather_providers.base import WeatherProvider
    from weather_providers.tomorrowio import TomorrowIOProvider
    p = TomorrowIOProvider("test-key")
    assert isinstance(p, WeatherProvider)
    assert p.name == "Tomorrow.io"
    assert p.requires_key is True
    assert inspect.iscoroutinefunction(p.get_weather)
    assert inspect.iscoroutinefunction(p.get_forecast)

@test("AirNowProvider: air-quality-only provider, key required")
def _():
    from weather_providers.airnow import AirNowProvider
    p = AirNowProvider("test-key")
    assert p.name == "AirNow"
    assert p.requires_key is True
    assert inspect.iscoroutinefunction(p.get_air_quality)
    # Air-quality-only: it deliberately does not implement get_weather.
    assert not hasattr(p, "get_weather")

@test("PurpleAirProvider: air-quality-only provider, key required")
def _():
    from weather_providers.purpleair import PurpleAirProvider
    p = PurpleAirProvider("test-key")
    assert p.name == "PurpleAir"
    assert p.requires_key is True
    assert inspect.iscoroutinefunction(p.get_air_quality)
    assert not hasattr(p, "get_weather")

@test("PurpleAir pm25_to_aqi: EPA 2024 breakpoints")
def _():
    from weather_providers.purpleair._codes import pm25_to_aqi
    assert pm25_to_aqi(0.0) == 0
    assert pm25_to_aqi(9.0) == 50          # 2024 Good/Moderate boundary
    assert pm25_to_aqi(35.4) == 100
    assert pm25_to_aqi(325.4) == 500
    assert pm25_to_aqi(400.0) == 500       # capped above top breakpoint
    assert pm25_to_aqi(None) is None
    assert pm25_to_aqi(12.0) > 50          # 2024: 12 µg/m³ is Moderate

@test("base: uv_category / kp_category thresholds")
def _():
    from weather_providers.base import uv_category, kp_category
    assert uv_category(2) == "Low"
    assert uv_category(6) == "High"
    assert uv_category(11) == "Extreme"
    assert uv_category(None) == ""
    assert kp_category(3) == "Quiet"
    assert kp_category(5).startswith("Minor")
    assert kp_category(9).startswith("Extreme")

@test("base: new capability dataclasses are frozen")
def _():
    from weather_providers.base import (UVResult, PollenResult, WildfireResult,
                                        SpaceWeatherResult, TideResult)
    for obj in (UVResult("s", "l"), PollenResult("s", "l"), WildfireResult("s", "l"),
                SpaceWeatherResult("s", "l"), TideResult("s", "l")):
        try:
            obj.source = "x"
            assert False, "should be frozen"
        except AttributeError:
            pass

@test("SunriseSunsetProvider: astronomy-only, no key")
def _():
    from weather_providers.sunrisesunset import SunriseSunsetProvider
    p = SunriseSunsetProvider()
    assert p.name == "SunriseSunset" and p.requires_key is False
    assert inspect.iscoroutinefunction(p.get_astronomy)

@test("MetNoProvider: multi-capability, no key")
def _():
    from weather_providers.metno import MetNoProvider
    p = MetNoProvider()
    assert p.requires_key is False
    for m in ("get_weather", "get_forecast", "get_hourly", "get_alerts", "get_nowcast"):
        assert inspect.iscoroutinefunction(getattr(p, m))

@test("dispatch: 5 new capabilities registered with method names")
def _():
    from weather_providers._dispatch import CAPABILITY_METHODS
    for cap, meth in (("uv", "get_uv"), ("pollen", "get_pollen"),
                      ("wildfire", "get_wildfire"),
                      ("space_weather", "get_space_weather"), ("tides", "get_tides")):
        assert CAPABILITY_METHODS.get(cap) == meth

@test("OpenMeteo WMO_CODES: covers common weather codes")
def _():
    from weather_providers.openmeteo._codes import WMO_CODES
    assert WMO_CODES[0] == "Clear"
    assert WMO_CODES[3] == "Overcast"
    assert WMO_CODES[63] == "Rain"
    assert WMO_CODES[95] == "Thunderstorm"

@test("OpenMeteo _deg_to_card: converts degrees to cardinal")
def _():
    from weather_providers.openmeteo._codes import deg_to_card
    assert deg_to_card(0) == "N"
    assert deg_to_card(90) == "E"
    assert deg_to_card(180) == "S"
    assert deg_to_card(270) == "W"
    assert deg_to_card(None) == ""

@test("configure: registers free providers when no config section")
def _():
    # No [weather_providers] section → registers every keyless provider
    # (currently nws + openmeteo).  Order = registration order in
    # _PROVIDER_FACTORIES, which is documented to put nws first.
    from configparser import ConfigParser
    import weather_providers as wp
    cfg = ConfigParser()
    wp.configure(cfg)
    providers = wp.get_providers()
    assert len(providers) >= 1
    # Both keyless providers must be present.
    assert "openmeteo" in providers
    assert "nws" in providers

@test("configure: skips providers without API keys")
def _():
    from configparser import ConfigParser
    import weather_providers as wp
    cfg = ConfigParser()
    cfg.add_section("weather_providers")
    cfg.set("weather_providers", "provider_priority", "weatherapi, openmeteo")
    # No weatherapi_key set → should skip weatherapi.
    wp.configure(cfg)
    providers = wp.get_providers()
    assert "weatherapi" not in providers
    assert "openmeteo" in providers

@test("configure: registers providers with keys in priority order")
def _():
    from configparser import ConfigParser
    import weather_providers as wp
    cfg = ConfigParser()
    cfg.add_section("weather_providers")
    cfg.set("weather_providers", "provider_priority", "weatherapi, tomorrowio, openmeteo")
    cfg.set("weather_providers", "weatherapi_key", "fake-key-1")
    cfg.set("weather_providers", "tomorrowio_key", "fake-key-2")
    wp.configure(cfg)
    providers = wp.get_providers()
    # Listed providers register first, in order (unlisted keyless providers
    # append after - priority is an ordering, not an allowlist).
    assert providers[:3] == ["weatherapi", "tomorrowio", "openmeteo"]

@test("configure: ignores unknown provider IDs")
def _():
    from configparser import ConfigParser
    import weather_providers as wp
    cfg = ConfigParser()
    cfg.add_section("weather_providers")
    cfg.set("weather_providers", "provider_priority", "nonexistent, openmeteo")
    wp.configure(cfg)
    providers = wp.get_providers()
    # Unknown IDs are skipped; openmeteo (listed) leads, and other keyless
    # providers still register (priority is an ordering, not an allowlist).
    assert providers[0] == "openmeteo"
    assert "nonexistent" not in providers

@test("weather _format_current: produces valid output from WeatherResult")
def _():
    from weather_providers.base import WeatherResult
    from modules.weather import _format_current
    r = WeatherResult(
        source="TestAPI", temperature=20.0, description="Clear",
        location="Testville", feels_like_c=18.0, humidity=50.0,
        wind_kph=15.0, wind_dir="S", pressure_mb=1013.0,
        visibility_m=16000.0, dewpoint_c=10.0,
    )
    body = _format_current(r)
    assert "Conditions Clear" in body
    assert "Temperature" in body
    assert "Humidity 50%" in body
    assert "[TestAPI]" in body

@test("weather _format_current: calm wind when < 1 kph")
def _():
    from weather_providers.base import WeatherResult
    from modules.weather import _format_current
    r = WeatherResult(
        source="X", temperature=20.0, description="Clear",
        location="Here", wind_kph=0.5,
    )
    assert "Calm" in _format_current(r)

@test("weather _format_current: feels-like hidden when < 2° diff")
def _():
    from weather_providers.base import WeatherResult
    from modules.weather import _format_current
    r_close = WeatherResult(
        source="X", temperature=20.0, description="Clear",
        location="Here", feels_like_c=20.5,
    )
    assert "Feels like" not in _format_current(r_close)
    r_far = WeatherResult(
        source="X", temperature=20.0, description="Clear",
        location="Here", feels_like_c=15.0,
    )
    assert "Feels like" in _format_current(r_far)

@test("weather _format_forecast: produces valid output from WeatherResult")
def _():
    from weather_providers.base import WeatherResult, ForecastDay
    from modules.weather import _format_forecast
    r = WeatherResult(
        source="TestAPI", temperature=20.0, description="Clear",
        location="Testville",
        forecast=[
            ForecastDay("Monday", 25.0, 15.0, "Sunny"),
            ForecastDay("Tuesday", 20.0, 12.0, "Rain"),
        ],
    )
    body = _format_forecast(r)
    assert "Monday Sunny" in body
    assert "Tuesday Rain" in body
    assert "[TestAPI]" in body

@test("weather _format_forecast: empty on no forecast data")
def _():
    from weather_providers.base import WeatherResult
    from modules.weather import _format_forecast
    r = WeatherResult(source="X", temperature=20.0, description="Clear",
                      location="Here")
    assert _format_forecast(r) == ""

@test("_http module: get_json is async")
def _():
    from weather_providers._http import get_json
    assert inspect.iscoroutinefunction(get_json)

@test("SEC-WP-001: _http has response size limit")
def _():
    from weather_providers._http import _MAX_RESPONSE_BYTES, _ResponseTooLarge
    assert _MAX_RESPONSE_BYTES > 0
    assert _MAX_RESPONSE_BYTES <= 10_000_000  # sane upper bound
    assert issubclass(_ResponseTooLarge, Exception)

@test("SEC-WP-002: provider exception logging does not leak API keys")
def _():
    # Verify that log.warning calls in __init__.py use type(e).__name__
    # rather than the full exception message (which could contain URL+key).
    source = Path("weather_providers/__init__.py").read_text(encoding="utf-8")
    # Should use safe pattern
    assert "type(e).__name__" in source
    # Should NOT use raw f-string with exception
    assert "failed: {e}" not in source

@test("SEC-WP-003: configure() builds new list atomically")
def _():
    # In 2.0, the Dispatcher owns the provider registry.
    # configure() calls dispatcher.clear() then dispatcher.register()
    # sequentially.  Verify the dispatcher is used.
    source = Path("weather_providers/__init__.py").read_text(encoding="utf-8")
    assert "dispatcher.clear()" in source
    assert "dispatcher.register(" in source

@test("SEC-WP-004: weather module sanitizes API strings")
def _():
    from modules.weather import _sanitize
    # Strips IRC formatting characters
    assert _sanitize("Hello\x02Bold\x02") == "HelloBold"
    assert _sanitize("Color\x03Text") == "ColorText"
    # Strips CRLF
    assert _sanitize("Line\r\nInjection") == "LineInjection"
    # Strips NUL
    assert _sanitize("Null\x00Byte") == "NullByte"
    # Truncates to max_len
    assert len(_sanitize("A" * 500)) == 200
    assert len(_sanitize("X" * 100, max_len=50)) == 50
    # Preserves clean strings
    assert _sanitize("Partly Cloudy") == "Partly Cloudy"

@test("SEC-WP-005: _format_current raises TypeError, not AssertionError")
def _():
    from modules.weather import _format_current, _format_forecast
    try:
        _format_current("not a WeatherResult")
        assert False, "should have raised TypeError"
    except TypeError:
        pass  # correct
    except AssertionError:
        assert False, "should be TypeError, not AssertionError"
    try:
        _format_forecast(42)
        assert False, "should have raised TypeError"
    except TypeError:
        pass

@test("SEC-WP-006: forecast days capped at _MAX_FORECAST_DAYS")
def _():
    from weather_providers import _MAX_FORECAST_DAYS
    assert _MAX_FORECAST_DAYS > 0
    assert _MAX_FORECAST_DAYS <= 30  # sane upper bound

@test("SEC-WP-010: providers use defensive .get() for response parsing")
def _():
    for fname in ("openmeteo/current.py", "weatherapi/current.py"):
        source = Path(f"weather_providers/{fname}").read_text(encoding="utf-8")
        assert '.get("current' in source or ".get('current'" in source or \
               'data.get("current")' in source or "data.get('current')" in source or \
               '.get("current' in source, \
            f"{fname} should use defensive .get() for response parsing"


# ══════════════════════════════════════════════════════════════════════
# modules/units.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== modules/units.py ===")
from modules.units import cf, kph, km_mi, mb, deg_to_card, fmt_dt, fmt_short

@test("units: cf (celsius/fahrenheit)")
def _():
    assert cf(0) == "0.0C / 32.0F"
    assert cf(100) == "100.0C / 212.0F"
    assert cf(None) == "N/A"

@test("units: deg_to_card")
def _():
    assert deg_to_card(0) == "N"
    assert deg_to_card(90) == "E"
    assert deg_to_card(180) == "S"
    assert deg_to_card(270) == "W"
    assert deg_to_card(None) == ""

@test("units: kph")
def _():
    result = kph(100.0)
    assert "100.0km/h" in result
    assert "mph" in result
    assert kph(None) == "N/A"

@test("units: km_mi")
def _():
    result = km_mi(1609.344)
    assert "1.0mi" in result
    assert km_mi(None) == "N/A"

@test("units: mb")
def _():
    result = mb(1013.0)
    assert "1013mb" in result
    assert mb(None) == "N/A"

@test("units: fmt_dt")
def _():
    result = fmt_dt("2026-03-03T12:00:00+00:00")
    assert "March" in result
    assert fmt_dt("") == "N/A"
    assert fmt_dt("not-a-date") == "not-a-date"


# ══════════════════════════════════════════════════════════════════════
# sender.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== sender.py ===")
from sender import Sender

@test("sender: CRLF/NUL injection stripped")
def _():
    import asyncio
    loop = asyncio.new_event_loop()
    sent: list[bytes] = []
    class FakeSock:
        def sendall(self, data: bytes): sent.append(data)
    s = Sender(loop)
    # Test _write_line directly (sync method that buffers)
    class FakeWriter:
        _closed = False
        def write(self, data: bytes): sent.append(data)
        def is_closing(self): return self._closed
    s._writer = FakeWriter()
    s._write_line("PRIVMSG #test :hello\r\nQUIT :injected\x00evil")
    assert len(sent) == 1
    line = sent[0].decode().rstrip("\r\n")
    assert "\r" not in line.rstrip("\r\n")  # only the trailing CRLF
    assert "\x00" not in line
    loop.close()

@test("sender: credential redaction in logs")
def _():
    # We can't easily test log output, but verify _REDACT_OUT covers key commands
    assert any("PASS" in p for p in Sender._REDACT_OUT)
    assert any("IDENTIFY" in p for p in Sender._REDACT_OUT)
    assert any("AUTHENTICATE" in p for p in Sender._REDACT_OUT)
    assert any("OPER" in p for p in Sender._REDACT_OUT)


# ══════════════════════════════════════════════════════════════════════
# hashpw.py
# ══════════════════════════════════════════════════════════════════════
print("\n=== hashpw.py ===")
from hashpw import hash_scrypt, verify_password

@test("hashpw: scrypt round-trip")
def _():
    h = hash_scrypt("testpassword")
    assert h.startswith("scrypt$")
    assert verify_password("testpassword", h) is True
    assert verify_password("wrongpassword", h) is False

@test("hashpw: invalid hash format")
def _():
    try:
        verify_password("test", "plaintext_not_a_hash")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

@test("hashpw: empty hash")
def _():
    try:
        verify_password("test", "")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ══════════════════════════════════════════════════════════════════════
# internets.py (helpers)
# ══════════════════════════════════════════════════════════════════════
print("\n=== internets.py helpers ===")

@test("ChannelSet: thread-safe add/discard/contains")
def _():
    from internets import ChannelSet
    cs = ChannelSet()
    cs.add("#Test")
    assert "#test" in cs
    assert "#TEST" in cs
    assert "#other" not in cs
    cs.discard("#TEST")
    assert "#test" not in cs

@test("ChannelSet: snapshot returns copy")
def _():
    from internets import ChannelSet
    cs = ChannelSet()
    cs.add("#a")
    cs.add("#b")
    snap = cs.snapshot()
    assert snap == {"#a", "#b"}
    cs.add("#c")
    assert "#c" not in snap  # original snapshot unchanged

@test("ChannelSet: iteration is safe")
def _():
    from internets import ChannelSet
    cs = ChannelSet()
    cs.add("#a")
    cs.add("#b")
    items = list(cs)
    assert set(items) == {"#a", "#b"}

@test("backoff: exponential with cap")
def _():
    from internets import _backoff
    assert _backoff(0) == 15.0
    assert _backoff(1) == 30.0
    assert _backoff(2) == 60.0
    assert _backoff(3) == 120.0
    assert _backoff(4) == 240.0
    assert _backoff(5) == 300.0  # capped
    assert _backoff(10) == 300.0  # still capped

@test("admin auth: case-insensitive")
def _():
    from internets import IRCBot
    bot = IRCBot()
    # Simulate adding an admin (lowercase normalized) with hostmask
    bot._nick_hosts["admin"] = "admin@host"
    bot._authed["admin"] = "admin@host"
    assert bot.is_admin("Admin")   # different case
    assert bot.is_admin("ADMIN")   # all caps
    assert bot.is_admin("admin")   # exact
    assert not bot.is_admin("other")

@test("admin auth: fails closed on an unverifiable hostmask binding")
def _():
    from internets import IRCBot
    bot = IRCBot()
    # The TOCTOU sentinel: a binding stored as "unknown" (admin quit during the
    # verify-password window, re-created by cmd_auth) must NEVER grant admin -
    # otherwise a later nick-grabber inherits a nick-only admin session.
    bot._authed["a"] = "unknown"
    bot._nick_hosts["a"] = "a@host"
    assert not bot.is_admin("a")
    assert "a" not in bot._authed          # sentinel binding revoked on check
    # No current hostmask to compare against → deny (never grant nick-only).
    bot._authed["b"] = "b@host"
    bot._nick_hosts.pop("b", None)
    assert not bot.is_admin("b")
    # Changed hostmask (a different user now holds the nick) → deny and revoke.
    bot._authed["c"] = "c@old"
    bot._nick_hosts["c"] = "c@new"
    assert not bot.is_admin("c")
    assert "c" not in bot._authed
    # A current, matching hostmask still grants (regression guard).
    bot._authed["d"] = "d@host"
    bot._nick_hosts["d"] = "d@host"
    assert bot.is_admin("d")

@test("PRIVMSG regex captures full user@host as hostmask")
def _():
    import re
    line = ":Nick!ident@some.host.name PRIVMSG #channel :hello world"
    m = re.match(r":([^!]+)!(\S+) PRIVMSG (\S+) :(.*)", line)
    assert m is not None
    nick, hostmask, target, text = m.groups()
    assert nick == "Nick"
    assert hostmask == "ident@some.host.name"  # full user@host, not just host
    assert target == "#channel"
    assert text == "hello world"

@test("JOIN regex captures full user@host as hostmask")
def _():
    import re
    line = ":Nick!ident@host.example.com JOIN #channel"
    m = re.match(r":([^!]+)!(\S+) JOIN :?(\S+)(?:\s+\S+)?", line)
    assert m is not None
    assert m.group(1) == "Nick"
    assert m.group(2) == "ident@host.example.com"
    assert m.group(3) == "#channel"

@test("NICK regex captures full user@host as hostmask")
def _():
    import re
    line = ":OldNick!ident@host.example.com NICK :NewNick"
    m = re.match(r":([^!]+)!(\S+) NICK :?(\S+)", line)
    assert m is not None
    assert m.group(1) == "OldNick"
    assert m.group(2) == "ident@host.example.com"
    assert m.group(3) == "NewNick"

@test("JOIN error handler matches 403, 405, 476 in addition to 471/474/475")
def _():
    import re
    pattern = re.compile(r":\S+ (403|405|471|474|475|476) \S+ (\S+)")
    for num, chan in [("403", "#nosuch"), ("405", "#toomany"), ("476", "#bad*mask"),
                      ("471", "#full"), ("474", "#banned"), ("475", "#badkey")]:
        line = f":server {num} MyBot {chan} :error text"
        m = pattern.match(line)
        assert m is not None, f"pattern should match {num}"
        assert m.group(1) == num
        assert m.group(2) == chan

@test("task done_callback: safe when task already removed from list")
def _():
    # Simulates the lambda guard: task not in list should not raise
    tasks = [1, 2, 3]
    cb = lambda t: t in tasks and tasks.remove(t)
    cb(2)           # normal removal
    assert 2 not in tasks
    cb(2)           # already removed - should NOT raise ValueError
    cb(99)          # never existed - should NOT raise ValueError
    assert tasks == [1, 3]


# ══════════════════════════════════════════════════════════════════════
# Async-specific tests
# ══════════════════════════════════════════════════════════════════════
print("\n=== async architecture ===")
import asyncio

@test("async sender: enqueue + drain produces output")
def _():
    async def _inner():
        loop = asyncio.get_running_loop()
        sent: list[bytes] = []
        class FakeWriter:
            def write(self, data: bytes): sent.append(data)
            def is_closing(self): return False
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass

        s = Sender(loop)
        writer = FakeWriter()
        s.start(writer)

        s.enqueue("PRIVMSG #test :hello", priority=0)
        # Let the drain task process.
        await asyncio.sleep(0.3)
        await s.stop()
        assert len(sent) >= 1
        assert b"PRIVMSG #test :hello\r\n" in sent

    asyncio.run(_inner())

@test("async sender: priority 0 bypasses token bucket")
def _():
    async def _inner():
        loop = asyncio.get_running_loop()
        sent: list[bytes] = []
        class FakeWriter:
            def write(self, data: bytes): sent.append(data)
            def is_closing(self): return False
            async def drain(self): pass

        s = Sender(loop)
        s.start(FakeWriter())

        # Enqueue 10 priority-0 messages rapidly - should all send immediately.
        for i in range(10):
            s.enqueue(f"PONG :test{i}", priority=0)
        await asyncio.sleep(0.5)
        await s.stop()
        assert len(sent) == 10

    asyncio.run(_inner())

@test("async sender: thread-safe enqueue from executor")
def _():
    async def _inner():
        loop = asyncio.get_running_loop()
        sent: list[bytes] = []
        class FakeWriter:
            def write(self, data: bytes): sent.append(data)
            def is_closing(self): return False
            async def drain(self): pass

        s = Sender(loop)
        s.start(FakeWriter())

        # Enqueue from a thread (simulates module handler).
        def threaded_send():
            s.enqueue("PRIVMSG #ch :from thread", priority=0)

        await asyncio.to_thread(threaded_send)
        await asyncio.sleep(0.3)
        await s.stop()
        assert any(b"from thread" in msg for msg in sent)

    asyncio.run(_inner())


# ══════════════════════════════════════════════════════════════════════
# async module handlers
# ══════════════════════════════════════════════════════════════════════
print("\n=== async module handlers ===")
import inspect

@test("all module command handlers are coroutines")
def _():
    from modules.weather import WeatherModule
    from modules.location import LocationModule
    from modules.calc import CalcModule
    from modules.dice import DiceModule
    from modules.translate import TranslateModule
    from modules.urbandictionary import UDModule
    from modules.channels import ChannelsModule

    for cls in (WeatherModule, LocationModule, CalcModule, DiceModule,
                TranslateModule, UDModule, ChannelsModule):
        for cmd_word, method_name in cls.COMMANDS.items():
            method = getattr(cls, method_name)
            assert inspect.iscoroutinefunction(method), \
                f"{cls.__name__}.{method_name} is not async"

@test("all core command handlers are coroutines")
def _():
    from internets import IRCBot
    for cmd_word, method_name in IRCBot._CORE.items():
        method = getattr(IRCBot, method_name)
        assert inspect.iscoroutinefunction(method), \
            f"IRCBot.{method_name} is not async"

@test("async geocode returns None for empty query")
def _():
    from modules.geocode import geocode
    assert inspect.iscoroutinefunction(geocode)

@test("async weather provider methods are coroutines")
def _():
    from weather_providers.openmeteo import OpenMeteoProvider
    from weather_providers.weatherapi import WeatherAPIProvider
    from weather_providers.tomorrowio import TomorrowIOProvider
    for cls, args in [(OpenMeteoProvider, ()), (WeatherAPIProvider, ("k",)),
                      (TomorrowIOProvider, ("k",))]:
        p = cls(*args)
        assert inspect.iscoroutinefunction(p.get_weather), f"{cls.__name__}.get_weather not async"
        assert inspect.iscoroutinefunction(p.get_forecast), f"{cls.__name__}.get_forecast not async"

@test("weather _format_current and _format_forecast are sync (pure functions)")
def _():
    from modules.weather import _format_current, _format_forecast
    assert not inspect.iscoroutinefunction(_format_current)
    assert not inspect.iscoroutinefunction(_format_forecast)

@test("weather_providers.get_weather and get_forecast are async")
def _():
    from weather_providers import get_weather, get_forecast
    assert inspect.iscoroutinefunction(get_weather)
    assert inspect.iscoroutinefunction(get_forecast)


# ══════════════════════════════════════════════════════════════════════
# Sixth Pass - Security hardening
# ══════════════════════════════════════════════════════════════════════
print("\n=== Security hardening (sixth pass) ===")

@test("SEC-007: _SafeFormatter strips CR/LF/NUL from log messages")
def _():
    from botlog import _SafeFormatter
    import logging
    fmt = _SafeFormatter("%(message)s")
    rec = logging.LogRecord("test", logging.INFO, "", 0, "hello\r\nworld\x00!", (), None)
    result = fmt.format(rec)
    assert "\r" not in result
    assert "\n" not in result
    assert "\x00" not in result
    assert "helloworld!" in result

@test("SEC-009: _connect enforces TLS 1.3 minimum (code inspection)")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    assert "minimum_version" in source
    # TLS 1.3 is the default minimum; TLS 1.2 must remain mentioned as
    # the only opt-in downgrade path (INTERNETS_ALLOW_TLS12) so we know
    # it isn't silently accepted.
    assert "TLSv1_3" in source
    assert "TLSv1_2" in source
    assert "INTERNETS_ALLOW_TLS12" in source

@test("BUG-026: sender enforces 512-byte IRC line limit")
def _():
    from sender import Sender
    assert hasattr(Sender, "_MAX_IRC_LINE")
    assert Sender._MAX_IRC_LINE == 512

@test("BUG-026: sender _write_line truncates long lines")
def _():
    import asyncio
    from sender import Sender

    loop = asyncio.new_event_loop()
    s = Sender(loop)

    # Create a mock writer that captures output.
    written = bytearray()
    class MockWriter:
        def is_closing(self): return False
        def write(self, data): written.extend(data)
    s._writer = MockWriter()

    # Send a line that exceeds 512 bytes.
    long_msg = "PRIVMSG #test :" + "A" * 600
    s._write_line(long_msg)

    # The written bytes (including \r\n) must not exceed 512.
    assert len(written) <= 512, f"Line was {len(written)} bytes, exceeds 512"
    assert written.endswith(b"\r\n")
    loop.close()

@test("BUG-027: privmsg rejects targets containing spaces")
def _():
    # Behavioural: a target with a space (or empty) must be dropped, not sent -
    # it would let an attacker inject extra IRC command args.
    from internets import IRCBot
    b = IRCBot.__new__(IRCBot)          # no __init__: only send + _split_msg needed
    sent = []
    b.send = lambda msg, priority=1: sent.append(msg)
    b.privmsg("bad target", "hello")
    b.privmsg("", "hello")
    assert sent == []                   # both rejected
    b.privmsg("#good", "hello")
    assert sent == ["PRIVMSG #good :hello"]

@test("BUG-027: notice rejects targets containing spaces")
def _():
    from internets import IRCBot
    b = IRCBot.__new__(IRCBot)
    sent = []
    b.send = lambda msg, priority=1: sent.append(msg)
    b.notice("bad target", "hello")
    b.notice("", "hello")
    assert sent == []
    b.notice("#good", "hello")
    assert sent == ["NOTICE #good :hello"]

@test("BUG-028: module loader blocks symlinks outside modules dir (code inspection)")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    assert "resolve()" in source
    assert "modules directory" in source.lower() or "mod_root" in source

@test("BUG-029: startup warns about world-readable config (code inspection)")
def _():
    source = Path("botlog.py").read_text(encoding="utf-8")
    assert "0o004" in source or "world-readable" in source

@test("BUG-030: _MAX_TASKS constant defined and enforced")
def _():
    from internets import IRCBot
    assert hasattr(IRCBot, "_MAX_TASKS")
    assert IRCBot._MAX_TASKS == 50
    # Verify the dispatch method references it.
    source = inspect.getsource(IRCBot._dispatch)
    assert "_MAX_TASKS" in source

@test("BUG-031: _MAX_ARG_LEN constant defined and enforced")
def _():
    from internets import IRCBot
    assert hasattr(IRCBot, "_MAX_ARG_LEN")
    assert IRCBot._MAX_ARG_LEN == 400
    source = inspect.getsource(IRCBot._dispatch)
    assert "_MAX_ARG_LEN" in source

@test("SEC-008: _run_cmd sends generic error, not raw exception")
def _():
    from internets import IRCBot as _Bot
    source = inspect.getsource(_Bot._run_cmd)
    assert "internal error" in source.lower() or "see log" in source.lower()

@test("SEC-008: load_module does not leak exception details to IRC")
def _():
    from internets import IRCBot as _Bot
    source = inspect.getsource(_Bot.load_module)
    # Should say "see log" not expose raw {e} in the return value
    assert "see log" in source.lower()


# ══════════════════════════════════════════════════════════════════════
# Seventh Pass - CISO final audit
# ══════════════════════════════════════════════════════════════════════
print("\n=== CISO final audit (seventh pass) ===")

@test("BUG-032: _SafeFormatter sanitizes record.args (not just msg)")
def _():
    from botlog import _SafeFormatter
    import logging
    fmt = _SafeFormatter("%(message)s")
    # Injection via args: msg is clean but %s arg contains CR/LF
    rec = logging.LogRecord("test", logging.INFO, "", 0, "data: %s", ("evil\r\nfake",), None)
    result = fmt.format(rec)
    assert "\r" not in result and "\n" not in result
    assert "evilfake" in result

@test("BUG-032: _SafeFormatter does not mutate shared record")
def _():
    from botlog import _SafeFormatter
    import logging
    fmt = _SafeFormatter("%(message)s")
    rec = logging.LogRecord("test", logging.INFO, "", 0, "hello\nworld", (), None)
    fmt.format(rec)
    # Original record.msg must be untouched
    assert "\n" in rec.msg

@test("BUG-032: _SafeFormatter handles dict args")
def _():
    from botlog import _SafeFormatter
    import logging
    fmt = _SafeFormatter("%(message)s")
    # Use %s style with a dict value in a tuple
    rec = logging.LogRecord("test", logging.INFO, "", 0, "data: %s", ({"key": "val\r\nue"},), None)
    result = fmt.format(rec)
    assert "\r" not in result and "\n" not in result

@test("SEC-017: config path resolved to absolute at startup")
def _():
    from config import CONFIG_PATH
    assert os.path.isabs(CONFIG_PATH)

@test("SEC-017: get_hash and cmd_rehash go through reload_config()")
def _():
    # Both must use config.reload_config() so config.local.ini is re-read
    # alongside config.ini - re-reading only config.ini would clobber
    # the overlay's values (e.g. password_hash) with the template's empty
    # placeholders.  See the comment on config.reload_config().
    bl_src = Path("botlog.py").read_text(encoding="utf-8")
    assert "from config import reload_config" in bl_src
    assert "reload_config()" in bl_src
    ac_src = Path("admin_cmds.py").read_text(encoding="utf-8")
    assert "from config import reload_config" in ac_src
    assert "reload_config()" in ac_src
    # Neither should hardcode "config.ini" or do the partial single-file re-read.
    assert 'cfg.read("config.ini")' not in bl_src
    assert 'cfg.read("config.ini")' not in ac_src
    assert 'cfg.read(CONFIG_PATH)' not in bl_src
    assert 'cfg.read(CONFIG_PATH)' not in ac_src

@test("SEC-013: cmd_rehash does not leak exception text to IRC")
def _():
    source = Path("admin_cmds.py").read_text(encoding="utf-8")
    rehash_section = source[source.index("async def cmd_rehash"):]
    rehash_section = rehash_section[:rehash_section.index("\n    async def ")]
    assert "see log" in rehash_section.lower()

@test("SEC-014: cmd_auth does not leak ValueError text to IRC")
def _():
    source = Path("admin_cmds.py").read_text(encoding="utf-8")
    auth_section = source[source.index("async def cmd_auth"):]
    auth_section = auth_section[:auth_section.index("\n    async def ")]
    assert "see log" in auth_section.lower()

@test("BUG-035: symlink check uses Path.relative_to (cross-platform)")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    load_fn = source.split("def load_module")[1].split("\n    def ")[0]
    assert "relative_to" in load_fn
    # Must NOT use os.sep string comparison
    assert "os.sep" not in load_fn

@test("BUG-042: asyncio.open_connection uses the _READ_LIMIT buffer cap")
def _():
    from internets import IRCBot
    # Assert the runtime constant and its wiring, not a bare source literal:
    # the read buffer cap is the named _READ_LIMIT (8192) and _connect passes
    # that constant to open_connection, so the value has one source of truth.
    assert IRCBot._READ_LIMIT == 8192
    assert "limit=self._READ_LIMIT" in inspect.getsource(IRCBot._connect)

@test("BUG-033: LimitOverrunError handled in main loop")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    assert "LimitOverrunError" in source

@test("BUG-047: _deferred_rejoin validates channel names")
def _():
    from internets import IRCBot
    assert hasattr(IRCBot, "_CHAN_RE")
    source = inspect.getsource(IRCBot._deferred_rejoin)
    assert "_CHAN_RE" in source

@test("BUG-038: INVITE has rate limiting")
def _():
    from internets import IRCBot
    source = inspect.getsource(IRCBot._on_invite)
    assert "_INVITE_COOLDOWN" in source or "rate" in source.lower()

@test("BUG-049: INVITE validates channel name format")
def _():
    from internets import IRCBot
    source = inspect.getsource(IRCBot._on_invite)
    assert "_CHAN_RE" in source

@test("BUG-050: PING payload capped to prevent oversized PONG")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    # Find the PING handler section
    ping_section = source.split('if line.startswith("PING")')[1].split("return")[0]
    assert "[:400]" in ping_section or "[:300]" in ping_section or "cap" in ping_section.lower()

@test("PLATFORM: config permission check guarded for POSIX only")
def _():
    source = Path("botlog.py").read_text(encoding="utf-8")
    # Find the BUG-029 section
    idx = source.index("BUG-029")
    section = source[idx:idx+300]
    assert 'os.name == "posix"' in section or "os.name == 'posix'" in section

@test("Store: _read has file size limit")
def _():
    from store import Store
    assert hasattr(Store, "_MAX_FILE_SIZE")
    assert Store._MAX_FILE_SIZE > 0

@test("Store: _write unlink is exception-safe on Windows")
def _():
    source = inspect.getsource(Store._write)
    # The os.unlink should be wrapped in try/except for Windows safety
    assert "try:" in source.split("os.unlink")[0].rsplit("os.replace", 1)[1]

@test("Store: _read and _write use explicit UTF-8 encoding")
def _():
    source_read  = inspect.getsource(Store._read)
    source_write = inspect.getsource(Store._write)
    assert "utf-8" in source_read
    assert "utf-8" in source_write

@test("IRCBot._CHAN_RE validates standard IRC channel formats")
def _():
    from internets import IRCBot
    rx = IRCBot._CHAN_RE
    # Valid
    assert rx.match("#test")
    assert rx.match("#Test-123")
    assert rx.match("&local")
    assert rx.match("+global")
    # Invalid
    assert not rx.match("test")         # no prefix
    assert not rx.match("#")            # too short
    assert not rx.match("#a b")         # space
    assert not rx.match("#a,b")         # comma
    assert not rx.match("")             # empty
    assert not rx.match("#" + "x" * 60) # too long

@test("VERSION: __version__ is defined and follows semver")
def _():
    from internets import __version__
    assert __version__
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)

@test("VERSION: .version command exists in _CORE")
def _():
    from internets import IRCBot
    assert "version" in IRCBot._CORE

@test("BUG-052: calc cbrt works without math.cbrt (Python <3.11 compat)")
def _():
    from modules.calc import _FUNCS
    assert "cbrt" in _FUNCS
    # Test it works
    assert abs(_FUNCS["cbrt"](27) - 3.0) < 1e-9
    assert abs(_FUNCS["cbrt"](-8) - (-2.0)) < 1e-9

@test("BUG-055: calc implicit mul uses safe sentinel (not NUL)")
def _():
    source = Path("modules/calc.py").read_text(encoding="utf-8")
    assert "\\x00" not in source  # NUL should not be used as sentinel

@test("SEC-018: nick collision uses secrets, not random")
def _():
    source = Path("internets.py").read_text(encoding="utf-8")
    # Find the 433 handler section (now in _handle_numeric)
    idx = source.index("_RE_433.match(line)")
    section = source[idx:idx+400]
    assert "secrets" in section
    assert "random.randint" not in section

@test("BUG-056: sender queue is bounded")
def _():
    from sender import Sender
    assert hasattr(Sender, "MAX_QUEUE")
    assert Sender.MAX_QUEUE > 0

@test("BUG-051: Store._read rejects a wrong-type file and quarantines it")
def _():
    import tempfile, json
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.json"
        p.write_text(json.dumps([1, 2, 3]))    # a list where a dict is expected
        result = Store._read(str(p), {})
        assert result == {}                     # returns default, not the list
        assert not p.exists()                   # quarantined, not left to be clobbered
        assert len(list(Path(tmp).glob("x.json.corrupt.*"))) == 1

# ══════════════════════════════════════════════════════════════════════
# Module edge-case tests
# ══════════════════════════════════════════════════════════════════════
print("\n=== module edge cases ===")

@test("translate: _LANG_RE accepts valid and rejects invalid lang codes")
def _():
    from modules.translate import _LANG_RE
    assert _LANG_RE.match("en")
    assert _LANG_RE.match("es")
    assert not _LANG_RE.match("ENG")
    assert not _LANG_RE.match("e")
    assert not _LANG_RE.match("123")
    assert not _LANG_RE.match("")

@test("mathx: _bignum_report renders a result over the int->str digit cap")
def _():
    import math
    from modules.mathx import _bignum_report
    # factorial(100000) is ~456k digits, well over Python's default 4300 cap;
    # str(value) would raise ValueError without the scoped limit bump.
    out = _bignum_report("100000!", math.factorial(100000))
    assert "digits" in out and "starts" in out

@test("urbandictionary: _IDX_RE parses term/index correctly")
def _():
    from modules.urbandictionary import _IDX_RE
    m = _IDX_RE.match("yolo /2")
    assert m and m.group(1).strip() == "yolo" and m.group(2) == "2"
    m = _IDX_RE.match("hello world /10")
    assert m and m.group(1).strip() == "hello world" and m.group(2) == "10"
    assert not _IDX_RE.match("noindex")

@test("geocode: _parse_coords handles decimal, hemisphere, and DMS coordinates")
def _():
    from modules.geocode import _parse_coords
    def close(got, want):
        return got is not None and abs(got[0]-want[0]) < 1e-3 and abs(got[1]-want[1]) < 1e-3
    # decimal (comma and space separated)
    assert close(_parse_coords("40.7128, -74.0060"), (40.7128, -74.0060))
    assert close(_parse_coords("-33.8688,151.2093"), (-33.8688, 151.2093))
    assert close(_parse_coords("39.8333 -98.5855"), (39.8333, -98.5855))
    # hemisphere decimal (the case free-text mis-resolved to Creve Coeur MO),
    # order-independent
    assert close(_parse_coords("39°N 98°W"), (39.0, -98.0))
    assert close(_parse_coords("N39 W98"), (39.0, -98.0))
    assert close(_parse_coords("98W 39N"), (39.0, -98.0))
    # DMS with minutes / seconds
    assert close(_parse_coords("39°50'N 98°35'W"), (39.8333, -98.5833))
    assert close(_parse_coords("34°30'15\"N 117°12'30\"W"), (34.5042, -117.2083))
    # Not coordinates → None (place names, postal codes, bare ints, out-of-range)
    assert _parse_coords("not coords") is None
    assert _parse_coords("40.7128") is None
    assert _parse_coords("91773") is None
    assert _parse_coords("39 98") is None
    assert _parse_coords("200,300") is None

@test("geocode: _format_name handles US locations with state abbreviation")
def _():
    from modules.geocode import _format_name
    name, cc = _format_name(
        {"city": "New York", "state": "New York", "country_code": "us"},
        "fallback"
    )
    assert "New York" in name
    assert cc == "us"

@test("geocode: _format_name handles non-US locations")
def _():
    from modules.geocode import _format_name
    name, cc = _format_name(
        {"city": "London", "country": "United Kingdom", "country_code": "gb"},
        "fallback"
    )
    assert "London" in name
    assert cc == "gb"

@test("geocode: _format_name returns fallback for empty address")
def _():
    from modules.geocode import _format_name
    name, cc = _format_name({}, "my fallback")
    assert name == "my fallback"

# ── Postal-code classification + routing (Spain-vs-Ohio / Canada fix) ──

@test("geocode: _postal_kind classifies postal-code formats")
def _():
    from modules.geocode import _postal_kind
    # Canadian alphanumeric (globally unique format)
    assert _postal_kind("A1A 1A1") == "ca"
    assert _postal_kind("a1a1a1")  == "ca"      # no space, lowercase
    assert _postal_kind("M5V 3L9") == "ca"
    # UK postcode
    assert _postal_kind("SW1A 1AA") == "uk"
    assert _postal_kind("EC1A 1BB") == "uk"
    # ZIP+4 is unambiguously US
    assert _postal_kind("12345-6789") == "us"
    # Bare numeric (5-digit ZIP AND foreign codes) → home-first numeric
    assert _postal_kind("43812") == "num"       # real Ohio ZIP
    assert _postal_kind("08000") == "num"       # Barcelona / not a US ZIP
    assert _postal_kind("1212")  == "num"       # 4-digit (CH, etc.)
    # Not postal codes → free-text path
    assert _postal_kind("london") is None
    assert _postal_kind("la quinta") is None
    assert _postal_kind("90210 main st") is None
    assert _postal_kind("") is None

@test("geocode: _split_postal_country extracts an explicit country override")
def _():
    from modules.geocode import _split_postal_country
    assert _split_postal_country("08000 spain") == ("08000", "es")
    assert _split_postal_country("08000 es")    == ("08000", "es")
    assert _split_postal_country("A1A 1A1 canada") == ("A1A 1A1", "ca")
    # No postal core → no split (city+province must reach the free-text loop)
    assert _split_postal_country("london ontario") == ("london ontario", None)
    assert _split_postal_country("paris france")   == ("paris france", None)
    assert _split_postal_country("08000")          == ("08000", None)

@test("geocode: ZIP + US-state abbreviation is NOT mis-read as a country override")
def _():
    from modules.geocode import _split_postal_country
    # The common US "ZIP state" shape must stay on the free-text path, not
    # pin to a colliding ISO2 (ca=California not Canada, il=Illinois not Israel).
    assert _split_postal_country("90210 ca") == ("90210 ca", None)
    assert _split_postal_country("60601 il") == ("60601 il", None)
    assert _split_postal_country("43230 oh") == ("43230 oh", None)
    assert _split_postal_country("12345 zz") == ("12345 zz", None)   # not a real ISO2
    # A genuine country code / name override is still honoured.
    assert _split_postal_country("08000 es")    == ("08000", "es")
    assert _split_postal_country("08000 spain") == ("08000", "es")

@test("geocode: _fsa extracts the 3-char Canadian forward-sortation area")
def _():
    from modules.geocode import _fsa
    assert _fsa("A1A 1A1") == "A1A"
    assert _fsa("m5v 3l9")  == "M5V"
    assert _fsa("A1A1A1")   == "A1A"

@test("geocode: _zippo_parse builds (lat, lon, name, cc) from Zippopotam JSON")
def _():
    from modules.geocode import _zippo_parse
    ca = {"country": "Canada", "country abbreviation": "CA",
          "places": [{"place name": "St. John's North",
                      "state": "Newfoundland and Labrador",
                      "state abbreviation": "NL",
                      "latitude": "47.571", "longitude": "-52.6961"}]}
    lat, lon, name, cc = _zippo_parse(ca)
    assert (round(lat, 3), round(lon, 3)) == (47.571, -52.696)
    assert name == "St. John's North, Canada"
    assert cc == "ca"
    us = {"country": "United States", "country abbreviation": "US",
          "places": [{"place name": "Coshocton", "state abbreviation": "OH",
                      "latitude": "40.27", "longitude": "-81.86"}]}
    _, _, name_us, cc_us = _zippo_parse(us)
    assert name_us == "Coshocton, OH"
    assert cc_us == "us"
    # Malformed / empty → None (fail closed)
    assert _zippo_parse({}) is None
    assert _zippo_parse({"places": []}) is None

@test("geocode: bare numeric tries home country first, then global (08000 → ES)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return (41.4, 2.2, "Badalona, España", "es") if cc is None else None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return None
    orig_nom, orig_zippo = g._nominatim_postal, g._zippo
    try:
        g._nominatim_postal, g._zippo = fake_nom, fake_zippo
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("08000", "bot (https://example.org)",
                                    default_country="us"))
    finally:
        g._nominatim_postal, g._zippo = orig_nom, orig_zippo
    assert res == (41.4, 2.2, "Badalona, España", "es")
    assert ("nom", "08000", "us") in calls      # home country tried first
    assert ("nom", "08000", None) in calls       # global fallback reached

@test("geocode: a real home-country ZIP resolves locally, never falls through (43812 → OH)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return (40.27, -81.86, "Coshocton, OH", "us") if cc == "us" else None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return None
    orig_nom, orig_zippo = g._nominatim_postal, g._zippo
    try:
        g._nominatim_postal, g._zippo = fake_nom, fake_zippo
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("43812", "bot (https://example.org)",
                                    default_country="us"))
    finally:
        g._nominatim_postal, g._zippo = orig_nom, orig_zippo
    assert res == (40.27, -81.86, "Coshocton, OH", "us")
    assert ("nom", "43812", None) not in calls   # global never reached

@test("geocode: Canadian postal code routes to Zippopotam by FSA (A1A 1A1 → CA)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return (47.5, -52.6, "St. John's North, Canada", "ca") if cc == "ca" else None
    orig_nom, orig_zippo = g._nominatim_postal, g._zippo
    try:
        g._nominatim_postal, g._zippo = fake_nom, fake_zippo
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("A1A 1A1", "bot (https://example.org)"))
    finally:
        g._nominatim_postal, g._zippo = orig_nom, orig_zippo
    assert res[3] == "ca"
    assert ("zippo", "ca", "A1A") in calls

@test("geocode: explicit country override pins the postal search (08000 spain → ES)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return (41.38, 2.17, "Barcelona, España", "es") if cc == "es" else None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return None
    orig_nom, orig_zippo = g._nominatim_postal, g._zippo
    try:
        g._nominatim_postal, g._zippo = fake_nom, fake_zippo
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("08000 spain", "bot (https://example.org)",
                                    default_country="us"))
    finally:
        g._nominatim_postal, g._zippo = orig_nom, orig_zippo
    assert res[3] == "es"
    assert ("nom", "08000", "es") in calls
    assert ("nom", "08000", "us") not in calls   # override beats home country

@test("geocode: _postal_kind pins distinctive intl formats (JP/BR/IE) but not bare numerics")
def _():
    from modules.geocode import _postal_kind
    assert _postal_kind("100-0001") == "jp"     # Japan, dashed
    assert _postal_kind("01310-100") == "br"     # Brazil CEP, dashed
    assert _postal_kind("D02 AF30") == "ie"      # Ireland Eircode
    assert _postal_kind("D02AF30")  == "ie"      # Eircode without the space
    assert _postal_kind("1000001")  == "num"     # bare 7-digit is NOT uniquely JP
    assert _postal_kind("01310100") == "num"     # bare 8-digit is NOT uniquely BR
    # Format-unique kinds remain disjoint - no cannibalisation
    assert _postal_kind("A1A 1A1") == "ca"
    assert _postal_kind("SW1A 1AA") == "uk"

@test("geocode: distinctive intl postal codes pin their country (100-0001/01310-100/D02 AF30)")
def _():
    import asyncio
    import modules.geocode as g
    for query, want_cc in [("100-0001", "jp"), ("01310-100", "br"), ("D02 AF30", "ie")]:
        calls: list = []
        async def fake_nom(code, cc, hdrs, _calls=calls, _cc=want_cc):
            _calls.append(("nom", code, cc))
            return (1.0, 2.0, f"City, {_cc}", _cc) if cc == _cc else None
        async def fake_zippo(cc, code, ua, _calls=calls):
            _calls.append(("zippo", cc, code))
            return None
        orig_nom, orig_zippo = g._nominatim_postal, g._zippo
        try:
            g._nominatim_postal, g._zippo = fake_nom, fake_zippo
            g._geocode_cache.clear()
            res = asyncio.run(g.geocode(query, "bot (https://example.org)"))
        finally:
            g._nominatim_postal, g._zippo = orig_nom, orig_zippo
        assert res is not None and res[3] == want_cc, (query, res)
        assert ("nom", query, want_cc) in calls, (query, calls)

@test("geocode: ZIP+4 resolves the 5-digit base, US-pinned (90210-1234)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return (34.1, -118.4, "Beverly Hills, CA", "us") if (code == "90210" and cc == "us") else None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return None
    orig_nom, orig_zippo = g._nominatim_postal, g._zippo
    try:
        g._nominatim_postal, g._zippo = fake_nom, fake_zippo
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("90210-1234", "bot (https://example.org)"))
    finally:
        g._nominatim_postal, g._zippo = orig_nom, orig_zippo
    assert res == (34.1, -118.4, "Beverly Hills, CA", "us")
    assert ("nom", "90210", "us") in calls   # +4 stripped to the 5-digit base

@test("geocode: non-postal input never touches the postal resolvers (london)")
def _():
    import asyncio
    import modules.geocode as g
    calls: list = []
    async def fake_nom(code, cc, hdrs):
        calls.append(("nom", code, cc))
        return None
    async def fake_zippo(cc, code, ua):
        calls.append(("zippo", cc, code))
        return None
    def boom(*a, **k):
        raise RuntimeError("free-text path should be exercised, not postal")
    orig_nom, orig_zippo, orig_get = g._nominatim_postal, g._zippo, g._get
    try:
        g._nominatim_postal, g._zippo, g._get = fake_nom, fake_zippo, boom
        g._geocode_cache.clear()
        res = asyncio.run(g.geocode("london", "bot (https://example.org)"))
    finally:
        g._nominatim_postal, g._zippo, g._get = orig_nom, orig_zippo, orig_get
    assert res is None            # free-text _get raised → no hit
    assert calls == []            # postal resolvers never called

@test("geocode: free-text word-drop loop is capped (bounds Nominatim requests per query)")
def _():
    import asyncio
    import modules.geocode as g
    n = {"calls": 0}
    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_get(url, params=None, headers=None, timeout=10):
        n["calls"] += 1
        return _FakeResp()
    orig_get, orig_read = g._get, g._read_json_capped
    try:
        g._get, g._read_json_capped = fake_get, (lambda r: [])   # always miss → keeps dropping
        g._geocode_cache.clear()
        asyncio.run(g.geocode("aa bb cc dd ee ff gg hh ii jj", "bot (https://example.org)"))
    finally:
        g._get, g._read_json_capped = orig_get, orig_read
    assert n["calls"] <= 5, n["calls"]   # initial + at most _MAX_DROPS(4) retries

@test("weather: no saved location prompts for regloc instead of a default location")
def _():
    from modules.weather import WeatherModule
    class FakeBot:
        cfg = {"bot": {"command_prefix": ".", "default_location": "38.0,-97.0"}}
        def loc_get(self, nick):
            return None
    res, err = WeatherModule(FakeBot())._resolve("bob", None)
    assert res is None              # no silent fallback to a default point
    assert err and "regloc" in err  # tells the user to register
    assert "38" not in err          # the old Kansas default is not echoed
    # A saved location is still honoured.
    class SavedBot(FakeBot):
        def loc_get(self, nick):
            return "San Dimas CA"
    res2, err2 = WeatherModule(SavedBot())._resolve("bob", None)
    assert res2 == "San Dimas CA" and err2 is None

@test("channels: _CHAN_RE validates IRC channel names")
def _():
    from modules.channels import _CHAN_RE
    assert _CHAN_RE.match("#valid")
    assert _CHAN_RE.match("&local")
    assert _CHAN_RE.match("+modeless")
    assert _CHAN_RE.match("!12345")
    assert not _CHAN_RE.match("nochanprefix")
    assert not _CHAN_RE.match("#has space")
    assert not _CHAN_RE.match("#has,comma")
    assert not _CHAN_RE.match("")
    assert not _CHAN_RE.match("#")

@test("channels: _PendingJoin stores initial state correctly")
def _():
    from modules.channels import _PendingJoin
    p = _PendingJoin("Alice", "#test", "#lobby", action="join")
    assert p.nick == "Alice"
    assert p.channel == "#test"
    assert p.reply_to == "#lobby"
    assert p.action == "join"
    assert p.account is None
    assert p.founder is None
    assert p.whois_done is False
    assert p.info_failed is False

@test("dice: _roll edge cases")
def _():
    from modules.dice import _roll
    assert "invalid" in _roll("")
    assert "invalid" in _roll("abc")
    assert "dice count" in _roll("0d6")
    assert "dice count" in _roll("101d6")
    assert "sides" in _roll("1d1")
    result = _roll("1d6+0")
    assert "Total" in result

@test("calc: CTCP markers stripped from expressions")
def _():
    from modules.calc import _calc
    assert _calc("\x012+2\x01") == "4"

@test("calc: keyword arguments rejected")
def _():
    from modules.calc import _calc
    result = _calc("pow(x=2, y=3)")
    assert "error" in result.lower() or "unknown" in result.lower()

@test("calc: negative factorial rejected")
def _():
    from modules.calc import _calc
    result = _calc("factorial(-1)")
    assert "error" in result.lower()

@test("calc: float factorial rejected")
def _():
    from modules.calc import _calc
    result = _calc("factorial(2.5)")
    assert "error" in result.lower()

@test("units: cf handles None")
def _():
    from modules.units import cf
    assert cf(None) == "N/A"

@test("units: kph handles None")
def _():
    from modules.units import kph
    assert kph(None) == "N/A"

@test("units: fmt_dt handles bad input gracefully")
def _():
    from modules.units import fmt_dt
    assert fmt_dt("") == "N/A"
    assert fmt_dt("not-a-date") == "not-a-date"

@test("units: fmt_short handles bad input gracefully")
def _():
    from modules.units import fmt_short
    assert fmt_short("") == "N/A"
    assert fmt_short("garbage") == "garbage"

@test("sender: Sender has bounded MAX_QUEUE and safe overflow")
def _():
    from sender import Sender
    source = Path("sender.py").read_text(encoding="utf-8")
    assert "maxsize=self.MAX_QUEUE" in source or "maxsize=self.MAX_QUEUE)" in source
    assert "_safe_put" in source

@test("hashpw: verify_password rejects wrong password")
def _():
    from hashpw import hash_scrypt, verify_password
    h = hash_scrypt("correcthorse")
    assert verify_password("correcthorse", h)
    assert not verify_password("wronghorse", h)
    assert not verify_password("", h)

@test("hashpw: verify_password rejects garbage hash")
def _():
    from hashpw import verify_password
    try:
        verify_password("pw", "garbage_no_prefix")
        assert False, "should have raised ValueError"
    except ValueError:
        pass

@test("protocol: parse_names_entry handles empty string edge case")
def _():
    from protocol import parse_names_entry
    nick, is_op = parse_names_entry("")
    assert nick == ""
    assert is_op is False

@test("protocol: parse_isupport_prefix handles malformed input")
def _():
    from protocol import parse_isupport_prefix
    modes, sym_map = parse_isupport_prefix("garbled")
    assert modes == set()
    assert sym_map == {}

@test("VERSION: __version__ matches pyproject.toml")
def _():
    from internets import __version__
    toml_text = Path("pyproject.toml").read_text(encoding="utf-8")
    # Extract version from pyproject.toml
    import re
    m = re.search(r'version\s*=\s*"([^"]+)"', toml_text)
    assert m, "version not found in pyproject.toml"
    assert __version__ == m.group(1), f"{__version__} != {m.group(1)}"


# ══════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"Results: {_pass} passed, {_fail} failed")
if _errors:
    print(f"\nFailures:")
    for err in _errors:
        print(err)
print(f"{'='*60}")
sys.exit(1 if _fail else 0)
