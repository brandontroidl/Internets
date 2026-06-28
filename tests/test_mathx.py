"""Tests for modules/mathx.py — pure-compute math toolbox."""

import sys
sys.path.insert(0, ".")

from modules.mathx import (
    _isprime, _factor, _gcd, _base, _stats, _roman, _pct, _bignum, _const,
    _is_probable_prime, _prime_factors, _to_roman, _from_roman, _fib,
)


class TestIsPrime:
    def test_small_prime(self):
        assert "is prime" in _isprime("7")
        assert "next prime 11" in _isprime("7")

    def test_composite(self):
        out = _isprime("15")
        assert "composite" in out
        assert "smallest factor 3" in out
        assert "next prime 17" in out

    def test_two(self):
        assert _isprime("2").startswith("2 is prime")

    def test_one_not_prime(self):
        assert "not prime" in _isprime("1")

    def test_zero(self):
        assert "not prime" in _isprime("0")

    def test_large_prime(self):
        # 2**61 - 1 is a Mersenne prime
        assert "is prime" in _isprime(str(2 ** 61 - 1))

    def test_large_composite(self):
        # product of two primes
        out = _isprime(str(1000003 * 1000033))
        assert "composite" in out

    def test_bad_input(self):
        assert _isprime("abc").startswith("usage:")
        assert _isprime("-5").startswith("usage:")

    def test_too_big(self):
        assert "cap is" in _isprime("9" * 101)

    def test_helper_direct(self):
        assert _is_probable_prime(97) is True
        assert _is_probable_prime(91) is False  # 7 * 13
        assert _is_probable_prime(1) is False


class TestFactor:
    def test_5040(self):
        assert _factor("5040") == "5040 = 2^4 x 3^2 x 5 x 7"

    def test_prime_input(self):
        assert _factor("13") == "13 is prime"

    def test_power_of_two(self):
        assert _factor("1024") == "1024 = 2^10"

    def test_semiprime(self):
        assert _factor("15") == "15 = 3 x 5"

    def test_too_small(self):
        assert "integer >= 2" in _factor("1")

    def test_bad_input(self):
        assert _factor("xyz").startswith("usage:")

    def test_too_big(self):
        assert "cap is" in _factor("9" * 20)

    def test_factors_helper(self):
        assert _prime_factors(360) == {2: 3, 3: 2, 5: 1}

    def test_large_19_digit(self):
        # 19-digit number factorable via rho
        out = _factor(str(999999999999999989))
        assert "=" in out or "prime" in out


class TestGcd:
    def test_two(self):
        assert _gcd("12 18") == "gcd = 6 :: lcm = 36"

    def test_three(self):
        assert _gcd("12 18 24") == "gcd = 6 :: lcm = 72"

    def test_comma_separated(self):
        assert _gcd("8, 12") == "gcd = 4 :: lcm = 24"

    def test_coprime(self):
        assert _gcd("9 28") == "gcd = 1 :: lcm = 252"

    def test_negatives(self):
        assert _gcd("-12 18") == "gcd = 6 :: lcm = 36"

    def test_single_fails(self):
        assert _gcd("5").startswith("usage:")

    def test_bad_input(self):
        assert "not an integer" in _gcd("5 foo")


class TestBase:
    def test_dec_to_hex(self):
        assert _base("255 10 16") == "255 (base 10) = ff (base 16)"

    def test_hex_to_dec(self):
        assert _base("ff 16 10") == "ff (base 10) = 255 (base 10)" or \
            _base("ff 16 10").endswith("= 255 (base 10)")

    def test_bin_to_dec(self):
        assert _base("1010 2 10").endswith("= 10 (base 10)")

    def test_dec_to_bin(self):
        assert _base("10 10 2").endswith("= 1010 (base 2)")

    def test_base36(self):
        assert _base("z 36 10").endswith("= 35 (base 10)")

    def test_zero(self):
        assert _base("0 10 2").endswith("= 0 (base 2)")

    def test_negative(self):
        assert _base("-255 10 16").endswith("= -ff (base 16)")

    def test_bad_base(self):
        assert "2..36" in _base("10 1 16")
        assert "2..36" in _base("10 10 40")

    def test_invalid_digit(self):
        assert "not a valid" in _base("9 2 10")

    def test_wrong_arg_count(self):
        assert _base("10 16").startswith("usage:")


class TestStats:
    def test_basic(self):
        out = _stats("1 2 3 4 5")
        assert "n=5" in out
        assert "mean=3" in out
        assert "median=3" in out
        assert "min=1" in out
        assert "max=5" in out
        assert "sum=15" in out

    def test_comma(self):
        out = _stats("10, 20, 30")
        assert "n=3" in out
        assert "mean=20" in out

    def test_single(self):
        out = _stats("42")
        assert "n=1" in out
        assert "stdev=0" in out

    def test_floats(self):
        out = _stats("1.5 2.5")
        assert "mean=2" in out

    def test_bad_input(self):
        assert "not a number" in _stats("1 two 3")

    def test_empty(self):
        assert _stats("").startswith("usage:")

    def test_too_many(self):
        assert "too many" in _stats(" ".join(["1"] * 1001))


class TestRoman:
    def test_to_roman(self):
        assert _roman("2024") == "2024 = MMXXIV"

    def test_from_roman(self):
        assert _roman("MMXXIV") == "MMXXIV = 2024"

    def test_subtractive(self):
        assert _to_roman(4) == "IV"
        assert _to_roman(9) == "IX"
        assert _to_roman(40) == "XL"

    def test_one(self):
        assert _roman("1") == "1 = I"

    def test_max(self):
        assert _roman("3999") == "3999 = MMMCMXCIX"

    def test_out_of_range(self):
        assert "1..3999" in _roman("4000")
        assert "1..3999" in _roman("0")

    def test_malformed_numeral(self):
        assert "not a well-formed" in _roman("IIII")

    def test_invalid(self):
        assert "neither" in _roman("hello")

    def test_roundtrip_helper(self):
        for n in (1, 14, 49, 99, 444, 3888):
            assert _from_roman(_to_roman(n)) == n


class TestPct:
    def test_percent_of(self):
        assert _pct("20% of 150") == "20% of 150 = 30"

    def test_change_increase(self):
        out = _pct("50 to 75")
        assert "50" in out
        assert "increase" in out

    def test_change_decrease(self):
        out = _pct("100 to 75")
        assert "decrease" in out

    def test_what_percent(self):
        assert _pct("30 of 120") == "30 is 25% of 120"

    def test_div_zero(self):
        assert "undefined" in _pct("0 to 50")
        assert "cannot take" in _pct("5 of 0")

    def test_bad_input(self):
        assert _pct("nonsense").startswith("usage:")


class TestBignum:
    def test_factorial(self):
        assert _bignum("5!") == "5! = 120"

    def test_factorial_big(self):
        out = _bignum("100!")
        assert "digits" in out
        assert "starts" in out

    def test_fib(self):
        assert _bignum("fib(10)") == "fib(10) = 55"

    def test_fib_big(self):
        out = _bignum("fib(1000)")
        assert "digits" in out

    def test_power(self):
        assert _bignum("2^10") == "2^10 = 1,024"

    def test_power_big(self):
        out = _bignum("2^1000")
        assert "digits" in out

    def test_starstar(self):
        assert _bignum("3**4") == "3^4 = 81"

    def test_bad_input(self):
        assert _bignum("blah").startswith("usage:")

    def test_fib_helper(self):
        assert _fib(0) == 0
        assert _fib(1) == 1
        assert _fib(10) == 55
        assert _fib(20) == 6765


class TestConst:
    def test_c(self):
        out = _const("c")
        assert "speed of light" in out
        assert "m/s" in out

    def test_planck(self):
        assert "Planck" in _const("h")

    def test_big_g(self):
        assert "gravitational" in _const("G")

    def test_alias(self):
        assert "Boltzmann" in _const("boltzmann")
        assert "Avogadro" in _const("avogadro")

    def test_case_insensitive(self):
        assert "electron" in _const("M_E")

    def test_unknown(self):
        assert "unknown constant" in _const("zzz")

    def test_empty_lists(self):
        assert _const("").startswith("usage:")
