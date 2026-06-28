"""Tests for modules/physcalc.py — pure physics/engineering calculators."""

import sys
sys.path.insert(0, ".")

from modules.physcalc import (
    _ly, _sr, _escape, _ohm, _rc, _baud, _parse_framing, _fmt_time,
)


class TestLy:
    def test_au_light_time(self):
        # 1 AU is ~8.3 light-minutes
        out = _ly("1 au")
        assert "light: 8.3" in out and "min" in out

    def test_ly_roundtrips_self(self):
        out = _ly("4.2 ly")
        assert out.startswith("4.2 ly = 4.2 ly")
        assert "yr" in out  # ~4.2 light-years travel time

    def test_km(self):
        out = _ly("384400 km")
        assert "384,400 km" in out
        assert "1.28" in out  # ~1.28 s light time to the Moon

    def test_light_minute_to_distance(self):
        out = _ly("8 min")
        assert out.startswith("light-min")
        assert "au" in out and "km" in out

    def test_negative_rejected(self):
        assert "non-negative" in _ly("-5 ly")

    def test_bad_input(self):
        assert _ly("hello").startswith("usage:")

    def test_unknown_unit(self):
        assert "unknown unit" in _ly("5 furlongs")

    def test_empty(self):
        assert _ly("").startswith("usage:")


class TestSr:
    def test_099c(self):
        out = _sr("0.99c")
        assert "gamma 7.08" in out
        assert "length contraction x0.141" in out

    def test_fraction_no_c(self):
        assert "gamma" in _sr("0.5")

    def test_zero(self):
        out = _sr("0")
        assert "gamma 1" in out

    def test_v_equals_c(self):
        assert "unphysical" in _sr("1.0")

    def test_superluminal(self):
        assert "[0, 1)" in _sr("1.5")

    def test_bad_input(self):
        assert _sr("fast").startswith("usage:")


class TestEscape:
    def test_earth(self):
        out = _escape("earth")
        assert out.startswith("earth:")
        assert "11.1" in out  # ~11.2 km/s
        assert "g)" in out

    def test_moon(self):
        out = _escape("moon")
        assert "2.37" in out  # ~2.38 km/s

    def test_explicit_mass_radius(self):
        out = _escape("5.97219e24 6.371e6")
        assert "11.1" in out  # matches Earth

    def test_unknown_body_lists(self):
        out = _escape("zorp")
        assert "usage:" in out and "earth" in out

    def test_bad_numbers(self):
        assert "usage:" in _escape("foo bar")

    def test_zero_radius(self):
        assert "positive" in _escape("1e24 0")


class TestOhm:
    def test_v_and_r(self):
        out = _ohm("V=12 R=4")
        assert "I 3 A" in out
        assert "P 36 W" in out

    def test_i_and_p(self):
        out = _ohm("I=2 P=24")
        assert "V 12 V" in out
        assert "R 6 ohm" in out

    def test_r_and_p(self):
        out = _ohm("R=4 P=36")
        assert "V 12 V" in out
        assert "I 3 A" in out

    def test_too_few(self):
        assert "exactly two" in _ohm("V=12")

    def test_too_many(self):
        assert "exactly two" in _ohm("V=12 R=4 I=3")

    def test_zero_current_div(self):
        assert "cannot be 0" in _ohm("V=12 I=0")

    def test_bad_input(self):
        assert "exactly two" in _ohm("nonsense")


class TestRc:
    def test_bands_to_ohms(self):
        # red red brown gold = 22 x 10^1 = 220 ohm, 5%
        out = _rc("red red brown gold")
        assert "220 ohm" in out
        assert "5%" in out

    def test_value_to_bands(self):
        out = _rc("4700")
        assert "yellow violet red" in out
        assert "4.7k ohm" in out

    def test_value_with_suffix(self):
        out = _rc("4.7k")
        assert "yellow violet red" in out

    def test_five_band(self):
        out = _rc("brown black black red brown")
        assert "10.02k ohm" in out

    def test_unknown_color(self):
        assert "unknown digit color" in _rc("red zorp brown")

    def test_too_few_bands(self):
        assert "3-5 color bands" in _rc("red red")

    def test_empty(self):
        assert _rc("").startswith("usage:")


class TestBaud:
    def test_default_framing(self):
        out = _baud("1024 9600")
        assert "10 bits/byte" in out
        assert "10,240 bits" in out
        assert "1.06" in out  # ~1.067 s

    def test_explicit_framing(self):
        out = _baud("100 115200 -fmt 8N2")
        assert "11 bits/byte" in out

    def test_bad_framing(self):
        assert "bad framing" in _baud("100 9600 -fmt ZZZ")

    def test_wrong_arg_count(self):
        assert _baud("1024").startswith("usage:")

    def test_bad_number(self):
        assert "invalid number" in _baud("x y")

    def test_zero_bps(self):
        assert ">0" in _baud("100 0")


class TestFramingAndTime:
    def test_8n1(self):
        assert _parse_framing("8N1") == 10

    def test_7e1(self):
        assert _parse_framing("7E1") == 10

    def test_8n2(self):
        assert _parse_framing("8N2") == 11

    def test_bad_framing(self):
        assert _parse_framing("garbage") is None

    def test_fmt_time_units(self):
        assert "ms" in _fmt_time(0.005)
        assert "s" in _fmt_time(10)
        assert "min" in _fmt_time(300)
        assert "hr" in _fmt_time(7200)
        assert "yr" in _fmt_time(1e9)
