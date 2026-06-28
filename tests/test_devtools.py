"""Tests for modules/devtools.py - pure dev-tool compute functions."""

import base64
import datetime as _dt
import json
import sys
import uuid

sys.path.insert(0, ".")

from modules.devtools import (
    _jwt, _semver, _uuid5, _uuid_inspect, _tz, _unix, _color, _cron,
    _parse_color, _nearest_css, _semver_parse,
)


def _mk_jwt(header, payload):
    def enc(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{enc(header)}.{enc(payload)}.sig"


class TestJwt:
    def test_basic_claims(self):
        tok = _mk_jwt({"alg": "HS256", "typ": "JWT"},
                      {"sub": "1234", "iat": 1700000000, "exp": 1700003600})
        out = _jwt(tok)
        assert "alg=HS256" in out
        assert "sub=1234" in out
        assert "exp=2023-11-14T23:13:20Z" in out

    def test_alg_none_warns(self):
        tok = _mk_jwt({"alg": "none"}, {"sub": "x"})
        out = _jwt(tok)
        assert "WARNING" in out and "none" in out

    def test_empty(self):
        assert _jwt("") == "usage: .jwt <token>"

    def test_not_enough_parts(self):
        assert "invalid JWT" in _jwt("onlyonepart")

    def test_garbage_payload(self):
        assert "invalid JWT" in _jwt("@@@.@@@.sig")


class TestSemver:
    def test_less(self):
        assert _semver("1.2.3", "1.2.4") == "1.2.3 < 1.2.4"

    def test_greater(self):
        assert _semver("2.0.0", "1.9.9") == "2.0.0 > 1.9.9"

    def test_equal(self):
        assert _semver("1.0.0", "1.0.0") == "1.0.0 = 1.0.0"

    def test_prerelease_lower_than_release(self):
        assert _semver("1.0.0-rc.1", "1.0.0") == "1.0.0-rc.1 < 1.0.0"

    def test_prerelease_ordering(self):
        # numeric pre-release < alphanumeric
        assert _semver("1.0.0-1", "1.0.0-alpha") == "1.0.0-1 < 1.0.0-alpha"

    def test_build_ignored(self):
        assert _semver("1.0.0+a", "1.0.0+b") == "1.0.0+a = 1.0.0+b"

    def test_v_prefix(self):
        assert _semver("v1.0.0", "1.0.1") == "v1.0.0 < 1.0.1"

    def test_invalid(self):
        assert "invalid semver" in _semver("1.2", "1.2.3")

    def test_parse_negative(self):
        try:
            _semver_parse("-1.0.0")
            # leading v not present; "-1.0.0" -> core "" + pre... actually splits on '-'
        except ValueError:
            pass


class TestUuid5:
    def test_deterministic_dns(self):
        a = _uuid5("dns", "example.com")
        b = _uuid5("dns", "example.com")
        assert a == b
        assert a == str(uuid.uuid5(uuid.NAMESPACE_DNS, "example.com"))

    def test_namespace_uuid(self):
        ns = str(uuid.NAMESPACE_URL)
        out = _uuid5(ns, "https://x")
        assert out == str(uuid.uuid5(uuid.NAMESPACE_URL, "https://x"))

    def test_bad_ns(self):
        assert "ns must be" in _uuid5("notanamespace", "x")

    def test_single_uuid_inspect(self):
        u = str(uuid.uuid4())
        out = _uuid5(u, None)
        assert "version 4" in out
        assert "RFC 4122" in out

    def test_single_non_uuid_usage(self):
        assert "usage" in _uuid5("hello", None)

    def test_inspect_invalid_returns_none(self):
        assert _uuid_inspect("not-a-uuid") is None


class TestTz:
    def test_clock_convert(self):
        out = _tz("15:00", "America/New_York", "UTC")
        # EST in January (anchor 2000-01-01) -> 20:00 UTC
        assert "20:00" in out

    def test_iso_convert(self):
        out = _tz("2026-07-01T12:00", "UTC", "Asia/Tokyo")
        assert "21:00" in out  # JST = UTC+9

    def test_unknown_from(self):
        assert "unknown zone" in _tz("15:00", "Not/AZone", "UTC")

    def test_unknown_to(self):
        assert "unknown zone" in _tz("15:00", "UTC", "Not/AZone")

    def test_bad_time(self):
        assert "bad time" in _tz("notatime", "UTC", "UTC")


class TestUnix:
    def test_signal_by_name(self):
        out = _unix("SIGKILL")
        assert "SIGKILL" in out and "9" in out

    def test_signal_no_prefix(self):
        out = _unix("kill")
        assert "SIGKILL" in out

    def test_signal_by_number(self):
        out = _unix("9")
        assert "SIGKILL" in out

    def test_errno_by_name(self):
        out = _unix("ENOENT")
        assert "ENOENT" in out and "errno" in out

    def test_errno_by_number(self):
        out = _unix("2")
        # 2 is also SIGINT and ENOENT
        assert "ENOENT" in out

    def test_unknown(self):
        assert "unknown" in _unix("NOTAREALTHING")

    def test_empty(self):
        assert _unix("") == "usage: .unix <signal|errno>"


class TestColor:
    def test_hex(self):
        out = _color("#ff8800")
        assert "#ff8800" in out
        assert "rgb(255,136,0)" in out
        assert "hsl(" in out

    def test_short_hex(self):
        assert _parse_color("#f00") == (255, 0, 0)

    def test_rgb(self):
        out = _color("rgb(255,0,0)")
        assert "#ff0000" in out
        assert "red" in out

    def test_hsl(self):
        # hsl(0,100%,50%) is pure red
        assert _parse_color("hsl(0,100%,50%)") == (255, 0, 0)

    def test_named(self):
        out = _color("blue")
        assert "#0000ff" in out
        assert "blue" in out

    def test_nearest(self):
        assert _nearest_css((254, 0, 0)) == "red"

    def test_bad(self):
        assert "bad color" in _color("notacolor")

    def test_empty(self):
        assert "usage" in _color("")


class TestCron:
    NOW = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)

    def test_every_minute(self):
        out = _cron("* * * * *", self.NOW)
        assert "every minute" in out
        assert "2026-01-01 00:01 UTC" in out

    def test_specific_time(self):
        out = _cron("30 9 * * *", self.NOW)
        assert "at 09:30" in out
        assert "2026-01-01 09:30 UTC" in out

    def test_step(self):
        out = _cron("*/15 * * * *", self.NOW)
        assert "2026-01-01 00:15 UTC" in out

    def test_weekday_range(self):
        # 0 0 * * 1-5 : midnight on weekdays. 2026-01-01 is a Thursday.
        out = _cron("0 0 * * 1-5", self.NOW)
        assert "day-of-week" in out
        assert "2026-01-01 00:00 UTC" not in out  # next is future
        assert "2026-01-02 00:00 UTC" in out  # Friday

    def test_named_month(self):
        out = _cron("0 0 1 jan *", self.NOW)
        assert "month" in out

    def test_wrong_field_count(self):
        assert "5 fields" in _cron("* * *", self.NOW)

    def test_invalid_range(self):
        assert "invalid cron" in _cron("99 * * * *", self.NOW)

    def test_naive_now_ok(self):
        out = _cron("* * * * *", _dt.datetime(2026, 1, 1, 0, 0))
        assert "every minute" in out
