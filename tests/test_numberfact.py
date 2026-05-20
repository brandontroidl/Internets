"""Tests for modules/numberfact.py — pure math_fact(n) function.

The function may randomly choose among applicable facts, so tests for
numbers with multiple applicable facts retry a handful of times and
pass if ANY iteration contains the expected substring. This keeps the
suite deterministic regardless of the implementation's RNG.
"""

import pytest

from modules.numberfact import math_fact


# Number of attempts when a number has multiple applicable facts and the
# implementation may randomly pick among them. Large enough to make the
# probability of a false negative negligible.
ATTEMPTS = 40


def _any_call_contains(n, *substrings):
    """Call math_fact(n) up to ATTEMPTS times; return True if any result
    contains ANY of the given substrings (case-insensitive)."""
    needles = [s.lower() for s in substrings]
    for _ in range(ATTEMPTS):
        out = math_fact(n).lower()
        if any(s in out for s in needles):
            return True
    return False


# --- Universal invariants -------------------------------------------------

@pytest.mark.parametrize("n", [-5, -1, 0, 1, 2, 7, 100, 1000, 9999])
def test_returns_string(n):
    assert isinstance(math_fact(n), str)


@pytest.mark.parametrize("n", [-5, -1, 0, 1, 2, 7, 100, 1000, 9999])
def test_non_empty(n):
    assert len(math_fact(n)) > 0


@pytest.mark.parametrize("n", [-5, -1, 0, 1, 2, 7, 100, 1000, 9999])
def test_ends_with_period(n):
    assert math_fact(n).endswith(".")


# --- Property-specific facts ---------------------------------------------

def test_seven_is_prime():
    assert _any_call_contains(7, "prime")


def test_thirteen_is_prime_or_fibonacci():
    # 13 is both prime and a Fibonacci number.
    assert _any_call_contains(13, "prime", "fibonacci")


def test_sixteen_is_square():
    assert _any_call_contains(16, "square")


def test_eightyone_is_square():
    assert _any_call_contains(81, "square")


def test_twentyseven_is_cube():
    assert _any_call_contains(27, "cube")


def test_eight_is_fibonacci_or_cube():
    # 8 is both a Fibonacci number and a perfect cube (2^3).
    assert _any_call_contains(8, "fibonacci", "cube")


def test_eightynine_is_fibonacci_or_prime():
    # 89 is both a Fibonacci number and a prime.
    assert _any_call_contains(89, "fibonacci", "prime")


def test_onetwenty_is_factorial():
    # 120 == 5!
    assert _any_call_contains(120, "factorial")


def test_sevenhundredtwenty_is_factorial():
    # 720 == 6!
    assert _any_call_contains(720, "factorial")


def test_onetwentyone_is_palindrome_or_square():
    # 121 is both a palindrome and 11^2.
    assert _any_call_contains(121, "palindrome", "square")


def test_1024_is_power():
    # 1024 == 2^10
    assert _any_call_contains(1024, "power")


def test_1000_is_power_or_cube():
    # 1000 == 10^3 (also a perfect cube).
    assert _any_call_contains(1000, "power", "cube")


# --- Edge cases ----------------------------------------------------------

@pytest.mark.parametrize("n", [0, 1, -1, -100])
def test_edge_case_non_empty(n):
    result = math_fact(n)
    assert isinstance(result, str)
    assert len(result) > 0
    assert result.endswith(".")
