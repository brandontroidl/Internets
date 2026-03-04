#!/usr/bin/env python3
"""
Standalone test runner — no pytest required.

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

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pass = 0
_fail = 0
_errors: list[str] = []


def test(name: str):
    """Decorator that registers and runs a test function."""
    def decorator(fn):
        global _pass, _fail
        try:
            fn()
            _pass += 1
            print(f"  ✓ {name}")
        except Exception as e:
            _fail += 1
            tb = traceback.format_exc()
            _errors.append(f"  ✗ {name}\n{tb}")
            print(f"  ✗ {name}: {e}")
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
        data = json.loads(Path(os.path.join(tmp, "loc.json")).read_text())
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
    # 55 nested sin() calls — should hit depth limit
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
# modules/weather.py (merge/format logic)
# ══════════════════════════════════════════════════════════════════════
print("\n=== modules/weather.py ===")
from modules.weather import _merge_current, _format_current

_BASE_DICT = {
    "conditions": "Clear", "temp_c": 20.0, "feels_c": 20.0,
    "feels_label": "Feels like", "dewpoint_c": 10.0,
    "pressure_mb": 1013.0, "humidity": 50.0, "visibility_m": 16000.0,
    "wind_kph": 15.0, "wind_deg": 180, "wind_gusts_kph": 20.0,
    "updated": "2026-03-03T12:00",
}

@test("weather merge: both None → None")
def _():
    assert _merge_current(None, None) is None

@test("weather merge: primary None → fallback")
def _():
    fb = dict(_BASE_DICT)
    assert _merge_current(None, fb) is fb

@test("weather merge: fallback None → primary")
def _():
    pr = dict(_BASE_DICT)
    assert _merge_current(pr, None) is pr

@test("weather merge: primary wins, fallback fills gaps")
def _():
    pr = {"conditions": None, "temp_c": 10.0, "pressure_mb": None,
           "humidity": 91.0, "feels_c": None, "feels_label": None,
           "dewpoint_c": 8.0, "visibility_m": None,
           "wind_kph": 23.0, "wind_deg": 0, "wind_gusts_kph": None,
           "updated": "2026-03-03T06:55"}
    fb = dict(_BASE_DICT)
    m = _merge_current(pr, fb)
    assert m["temp_c"] == 10.0      # NWS wins
    assert m["humidity"] == 91.0    # NWS wins
    assert m["conditions"] == "Clear"  # OM fills gap
    assert m["pressure_mb"] == 1013.0  # OM fills gap
    assert m["visibility_m"] == 16000.0  # OM fills gap

@test("weather merge: NWS heat index label preserved")
def _():
    pr = dict(_BASE_DICT, feels_c=35.0, feels_label="Heat index")
    fb = dict(_BASE_DICT, feels_c=30.0, feels_label="Feels like")
    m = _merge_current(pr, fb)
    assert m["feels_label"] == "Heat index"

@test("weather format: complete dict produces valid output")
def _():
    body = _format_current(_BASE_DICT)
    assert body is not None
    assert "Conditions Clear" in body
    assert "Temperature" in body
    assert "Humidity 50%" in body

@test("weather format: None → None")
def _():
    assert _format_current(None) is None

@test("weather format: calm wind (< 1 kph)")
def _():
    d = dict(_BASE_DICT, wind_kph=0.5)
    body = _format_current(d)
    assert "Calm" in body

@test("weather format: gusts only shown when > 1.3x wind")
def _():
    d_gusty = dict(_BASE_DICT, wind_kph=20.0, wind_gusts_kph=30.0)
    assert "gusts" in _format_current(d_gusty)
    d_mild = dict(_BASE_DICT, wind_kph=20.0, wind_gusts_kph=25.0)
    assert "gusts" not in _format_current(d_mild)

@test("weather format: no N/A when all fields present")
def _():
    body = _format_current(_BASE_DICT)
    assert "N/A" not in body

@test("weather format: feels-like hidden when < 2° diff")
def _():
    d = dict(_BASE_DICT, temp_c=20.0, feels_c=20.5)
    body = _format_current(d)
    assert "Feels like" not in body
    d2 = dict(_BASE_DICT, temp_c=20.0, feels_c=15.0)
    body2 = _format_current(d2)
    assert "Feels like" in body2


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
    sent: list[bytes] = []
    class FakeSock:
        def sendall(self, data: bytes): sent.append(data)
    s = Sender()
    s.sock = FakeSock()
    s._write("PRIVMSG #test :hello\r\nQUIT :injected\x00evil")
    assert len(sent) == 1
    assert b"\r\n" in sent[0]  # only the trailing CRLF
    line = sent[0].decode().rstrip("\r\n")
    assert "\r" not in line and "\n" not in line and "\x00" not in line

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
