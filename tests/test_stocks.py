"""Tests for the stocks module helper functions."""

from modules.stocks import _fmt_change, _fmt_number


def test_fmt_change_positive():
    result = _fmt_change(2.50, 1.25)
    assert "+2.50" in result
    assert "+1.25%" in result


def test_fmt_change_negative():
    result = _fmt_change(-3.10, -2.00)
    assert "-3.10" in result
    assert "-2.00%" in result


def test_fmt_number_billions():
    assert _fmt_number(1_500_000_000) == "1.50B"


def test_fmt_number_millions():
    assert _fmt_number(42_000_000) == "42.00M"


def test_fmt_number_thousands():
    assert _fmt_number(8_500) == "8.50K"


def test_fmt_number_small():
    assert _fmt_number(123.45) == "123.45"
