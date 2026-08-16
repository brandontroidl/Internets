"""`.isprime` and `.factor` must not be able to hang the bot.

Both reach `_pollard_rho()` for a composite that survives trial division.  That
loop had no iteration bound and both handlers ran it synchronously on the event
loop, so one pasted semiprime stalled every user's commands, not just the
caller's.  The 60 s command timeout does not help: `asyncio.wait_for` cannot
interrupt a synchronous call already running on the loop.

These tests pin the bound and the offload separately, because either one alone
still leaves a way to hurt the bot: an unbounded loop in a worker thread burns
a pool slot indefinitely, and a bounded loop on the event loop still blocks it
for the length of the bound.
"""

from __future__ import annotations

import inspect
import sys

import pytest

_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import modules.mathx as mathx  # noqa: E402

sys.argv = _SAVED_ARGV

# Two 60-digit primes.  Their product survives the 2^20 trial-division cap, so
# it is the shape that used to fall into the unbounded loop.
_P = 671998030559713968361666935769
_Q = 282174488599599500573849980909


class TestPollardRhoIsBounded:
    def test_gives_up_instead_of_spinning(self):
        """A hard semiprime must terminate, not run forever."""
        with pytest.raises(mathx.FactorizationLimit):
            mathx._pollard_rho(_P * _Q, max_iterations=200)

    def test_still_factors_something_easy(self):
        """The bound must not break ordinary factoring."""
        n = 8051  # 83 * 97
        d = mathx._pollard_rho(n)
        assert n % d == 0 and 1 < d < n

    def test_default_bound_exists(self):
        sig = inspect.signature(mathx._pollard_rho)
        assert "max_iterations" in sig.parameters
        assert sig.parameters["max_iterations"].default > 0


class TestHandlersReportRatherThanHang:
    def test_isprime_reports_the_limit(self):
        out = mathx._isprime(str(_P * _Q))
        assert "composite" in out.lower() or "could not" in out.lower()
        assert str(_P) not in out  # it must not have actually factored it

    def test_factor_refuses_input_it_could_not_bound(self):
        """`.factor` caps input length, so it never reaches the hard case.

        That cap, not the iteration budget, is what bounds `.factor`. Recorded
        here because the two commands are bounded by different mechanisms and
        a reader would otherwise assume the budget covers both: `.isprime`
        accepts far longer input and needs the budget.
        """
        out = mathx._factor(str(_P * _Q))
        assert "too big" in out.lower()

    def test_factor_within_its_cap_still_terminates(self):
        """The largest input `.factor` accepts must finish inside the budget."""
        out = mathx._factor("9223372036854775783")  # 19-digit prime
        assert "prime" in out.lower()

    def test_small_numbers_still_work(self):
        assert "prime" in mathx._isprime("97")
        assert "3" in mathx._factor("12")


class TestHandlersOffloadToAThread:
    """A bounded loop on the event loop still blocks it for the bound."""

    @pytest.mark.parametrize("name", ["cmd_isprime", "cmd_factor"])
    def test_handler_uses_to_thread(self, name):
        src = inspect.getsource(getattr(mathx.MathxModule, name))
        assert "to_thread" in src, (
            f"{name} runs CPU-bound factoring on the event loop; "
            f"cmd_bignum in the same file shows the correct pattern")
