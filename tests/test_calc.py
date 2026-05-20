"""Tests for modules/calc.py — safe expression evaluator."""

import sys
sys.path.insert(0, ".")

from modules.calc import _calc


class TestCalcBasic:
    def test_addition(self):
        assert _calc("2 + 3") == "5"

    def test_multiplication(self):
        assert _calc("6 * 7") == "42"

    def test_division(self):
        assert _calc("10 / 3") == "3.3333333"

    def test_floor_division(self):
        assert _calc("10 // 3") == "3"

    def test_modulo(self):
        assert _calc("10 % 3") == "1"

    def test_power(self):
        assert _calc("2**10") == "1024"

    def test_negative(self):
        assert _calc("-5 + 3") == "-2"

    def test_parentheses(self):
        assert _calc("(2 + 3) * 4") == "20"


class TestCalcFunctions:
    def test_sqrt(self):
        assert _calc("sqrt(144)") == "12"

    def test_sin(self):
        result = float(_calc("sin(0)"))
        assert abs(result) < 1e-10

    def test_factorial(self):
        assert _calc("factorial(5)") == "120"

    def test_log(self):
        assert _calc("log(e)") == "1"


class TestCalcConstants:
    def test_pi(self):
        result = float(_calc("pi"))
        assert abs(result - 3.14159265) < 1e-6

    def test_implicit_mul(self):
        result = float(_calc("2pi"))
        assert abs(result - 6.28318530) < 1e-6


class TestCalcSafety:
    def test_division_by_zero(self):
        assert _calc("1/0") == "division by zero"

    def test_factorial_too_large(self):
        assert "too large" in _calc("factorial(999)")

    def test_exponent_too_large(self):
        assert "too large" in _calc("2**99999")

    def test_unknown_name(self):
        assert "unknown name" in _calc("os")

    def test_no_attribute_access(self):
        assert "error" in _calc("().__class__")

    def test_no_builtins(self):
        assert "error" in _calc("__import__('os')")

    def test_syntax_error(self):
        assert "error" in _calc("2 +")

    def test_empty(self):
        assert "error" in _calc("")
