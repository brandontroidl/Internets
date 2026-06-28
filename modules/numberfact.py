"""Number facts - local math + Wikipedia REST for trivia/date/year.

numbersapi.com was sold off in 2025 (now 301-redirects to a publisher
domain that 404s), so this module replaces it with a hybrid:

* ``math``   - computed locally (no network)
* ``trivia`` - Wikipedia article ``<n>_(number)`` summary
* ``date``   - Wikipedia ``feed/onthisday/events/MM/DD``
* ``year``   - Wikipedia ``page/summary/<year>`` summary

``random`` is accepted in place of a number for any type.  When the
remote summary is just the boilerplate "N is the natural number
following..." sentence (no actual trivia) or the page 404s, we fall
back to the local ``math_fact()`` so the user still gets something.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math as _math
import random
import re

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.numberfact")

# Use the OS-level cryptographic PRNG even though this module's randomness
# is non-security-sensitive (picking which fun fact / event to display).
# Bandit's B311 query flags the plain ``random`` module across the codebase;
# routing through SystemRandom avoids per-line ``# nosec`` annotations at
# no perceptible cost.
_rng = random.SystemRandom()

_WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
_WIKI_ONTHISDAY = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{mm}/{dd}"
_TYPES = {"trivia", "math", "date", "year"}
# Upper bound on a user-supplied number.  math_fact() runs O(√n) trial
# division (primality, factorization, divisor count); without a ceiling
# a 19-digit input burns ~90 s of CPU on a to_thread worker - a trivial
# DoS.  10^12 keeps every path's √n ≤ 10^6 iterations (sub-millisecond).
_MAX_ABS_N = 10 ** 12
# Wikipedia's on-this-day endpoint returns large blobs - May 20 alone is
# ~1.5 MB.  Cap generously; trivia/year summaries are tiny so a single
# global ceiling is fine.
_MAX_BODY_BYTES = 4 * 1024 * 1024
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
# Wikipedia boilerplate we want to ignore: "12 (twelve) is the natural
# number following 11 and preceding 13."  If that's *all* there is in the
# extract, there's nothing interesting to share - fall back to math_fact.
_BOILERPLATE_RE = re.compile(
    r"^\d+\s*\([^)]+\)\s+is\s+the\s+natural\s+number\s+following\s+\d+\s+and\s+preceding\s+\d+\.\s*$"
)
_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


# ---------------------------------------------------------------------------
# Pure local math facts
# ---------------------------------------------------------------------------

def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0:
        return False
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def _is_perfect_square(n: int) -> bool:
    if n < 0:
        return False
    r = _math.isqrt(n)
    return r * r == n


def _is_perfect_cube(n: int) -> bool:
    if n == 0:
        return True
    m = abs(n)
    # binary search for cube root
    lo, hi = 0, m
    while lo <= hi:
        mid = (lo + hi) // 2
        c = mid * mid * mid
        if c == m:
            return True
        if c < m:
            lo = mid + 1
        else:
            hi = mid - 1
    return False


def _cube_root(n: int) -> int:
    if n == 0:
        return 0
    sign = 1 if n > 0 else -1
    m = abs(n)
    lo, hi = 0, m
    while lo <= hi:
        mid = (lo + hi) // 2
        c = mid * mid * mid
        if c == m:
            return sign * mid
        if c < m:
            lo = mid + 1
        else:
            hi = mid - 1
    return sign * hi  # not exact; only called when _is_perfect_cube true


def _is_fibonacci(n: int) -> bool:
    if n < 0:
        return False
    # n is fib iff 5n^2 + 4 or 5n^2 - 4 is a perfect square
    return _is_perfect_square(5 * n * n + 4) or _is_perfect_square(5 * n * n - 4)


def _factorial_k(n: int) -> int | None:
    """If n == k! for some k >= 0, return k.  Else None."""
    if n < 1:
        return None
    if n == 1:
        return 1  # 1 == 1! (also 0! but pick 1 for nicer phrasing)
    k = 2
    acc = 2
    while acc < n:
        k += 1
        acc *= k
    return k if acc == n else None


def _is_triangular(n: int) -> bool:
    if n < 0:
        return False
    # n = k(k+1)/2 → 8n+1 must be a perfect square
    return _is_perfect_square(8 * n + 1)


def _is_palindrome(n: int) -> bool:
    s = str(abs(n))
    return len(s) > 1 and s == s[::-1]


def _is_power_of(n: int, base: int) -> bool:
    if n < 1 or base < 2:
        return False
    while n > 1:
        if n % base != 0:
            return False
        n //= base
    return n == 1


def _power_exponent(n: int, base: int) -> int:
    e = 0
    while n > 1:
        n //= base
        e += 1
    return e


def _prime_factorization(n: int) -> str:
    if n < 2:
        return ""
    factors: list[tuple[int, int]] = []
    m = n
    p = 2
    while p * p <= m:
        if m % p == 0:
            c = 0
            while m % p == 0:
                m //= p
                c += 1
            factors.append((p, c))
        p += 1 if p == 2 else 2
    if m > 1:
        factors.append((m, 1))
    return " × ".join(f"{p}^{c}" if c > 1 else str(p) for p, c in factors)


def _divisor_count(n: int) -> int:
    if n < 1:
        return 0
    count = 0
    i = 1
    while i * i <= n:
        if n % i == 0:
            count += 2 if i * i != n else 1
        i += 1
    return count


def math_fact(n: int) -> str:
    """Return one interesting math fact about ``n`` as a sentence ending with '.'."""
    # Special cases first - they're the most distinctive
    if n == 0:
        return "0 is the additive identity and the only integer that is neither positive nor negative."
    if n == 1:
        return "1 is the multiplicative identity and the only positive integer that is neither prime nor composite."
    if n == -1:
        return "-1 is the only integer whose multiplicative inverse is itself (other than 1)."
    if n < 0:
        # Negatives don't fit most of the predicates below - fall back to a
        # short sentence rooted in |n|'s properties.
        pos = abs(n)
        bits: list[str] = []
        if _is_prime(pos):
            bits.append(f"the negation of the prime {pos}")
        elif _is_perfect_square(pos):
            r = _math.isqrt(pos)
            bits.append(f"the negation of {r}² = {pos}")
        else:
            bits.append(f"a negative integer with |n| = {pos}")
        return f"{n} is {bits[0]}."

    # Identify every applicable property, then pick the most distinctive one.
    # Order matters: rarer / more interesting facts win.  A few targeted ties
    # are broken by magnitude (e.g. high powers of two beat their "happens
    # to also be a square" framing - 1024 reads better as 2^10 than as 32²).
    is_prime = _is_prime(n)
    is_square = n >= 4 and _is_perfect_square(n)
    is_cube = n >= 8 and _is_perfect_cube(n)
    is_fib = n >= 2 and _is_fibonacci(n)
    fact_k = _factorial_k(n) if n >= 2 else None
    is_tri = n >= 3 and _is_triangular(n)
    is_pal = _is_palindrome(n)
    is_pow2 = n >= 2 and _is_power_of(n, 2)
    is_pow10 = n >= 10 and _is_power_of(n, 10)

    # Priority: factorial (k>=3) > cube > small-prime (<13) > fibonacci >
    # prime > palindrome > (high power-of-2 > square > low power-of-2) >
    # triangular > power-of-10.  Skip k!=1,2 since 1 and 2 are more
    # interesting as identity/prime than as 1!/2!.  Fibonacci is rarer than
    # primality once n is past the small range, so it wins for n >= 13;
    # below that, "prime" reads better (2, 3, 5).
    if fact_k is not None and fact_k >= 3:
        return f"{n} is a factorial: {fact_k}! = {n}."
    if is_cube:
        r = _cube_root(n)
        return f"{n} is a perfect cube: {r}³ = {n}."
    if is_prime and n < 13:
        return f"{n} is a prime number."
    if is_fib:
        return f"{n} is a Fibonacci number."
    if is_prime:
        return f"{n} is a prime number."
    if is_pal:
        return f"{n} is a palindrome in base 10."
    # Powers of two from 2^6 (=64) and up read more clearly as powers; small
    # ones (4, 16, 32) read better as squares when they happen to be both.
    if is_pow2 and _power_exponent(n, 2) >= 6:
        e = _power_exponent(n, 2)
        return f"{n} is a power of two: 2^{e} = {n}."
    if is_square:
        r = _math.isqrt(n)
        return f"{n} is a perfect square: {r}² = {n}."
    if is_pow2:
        e = _power_exponent(n, 2)
        return f"{n} is a power of two: 2^{e} = {n}."
    if is_tri:
        kt = (_math.isqrt(8 * n + 1) - 1) // 2
        return f"{n} is the {kt}th triangular number."
    if is_pow10:
        e = _power_exponent(n, 10)
        return f"{n} is a power of ten: 10^{e} = {n}."

    # Fallback - composite, nothing fancy.  Show a short factorization, plus
    # divisor count for tiny extra colour.
    pf = _prime_factorization(n)
    dc = _divisor_count(n)
    if pf:
        return f"{n} = {pf} (it has {dc} divisors)."
    return f"{n} is an integer with {dc} divisors."


# ---------------------------------------------------------------------------
# Wikipedia REST helpers (blocking - call via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _read_capped(r: requests.Response) -> bytes | None:
    body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
    if len(body) > _MAX_BODY_BYTES:
        return None
    return body


def _truncate(s: str, n: int = 300) -> str:
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _fetch_trivia_sync(n: int, ua: str) -> str:
    """Wikipedia summary for ``<n>_(number)``; fall back to math_fact()."""
    try:
        # `with` releases the socket on every exit path (the stream=True
        # response would otherwise leak the connection / FD).
        with requests.get(
            _WIKI_SUMMARY.format(slug=f"{n}_(number)"),
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10, stream=True,
        ) as r:
            if r.status_code == 404:
                return math_fact(n)
            r.raise_for_status()
            body = _read_capped(r)
        if body is None:
            return "Wikipedia response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
        extract = (d.get("extract") or "").strip()
        if not extract or _BOILERPLATE_RE.match(extract):
            return math_fact(n)
        return f"{n}: {_truncate(extract, 300)}"
    except requests.RequestException as e:
        log.warning(f"numberfact wiki trivia: {e}")
        return "Wikipedia unavailable"
    except Exception as e:
        log.warning(f"numberfact wiki trivia parse: {e!r}")
        return "Wikipedia response parse error"


def _fetch_year_sync(year: int, ua: str) -> str:
    """Wikipedia summary for ``<year>``; fall back to math_fact() on 404."""
    try:
        with requests.get(
            _WIKI_SUMMARY.format(slug=str(year)),
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10, stream=True,
        ) as r:
            if r.status_code == 404:
                return math_fact(year)
            r.raise_for_status()
            body = _read_capped(r)
        if body is None:
            return "Wikipedia response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
        extract = (d.get("extract") or "").strip()
        if not extract:
            return math_fact(year)
        return f"{year}: {_truncate(extract, 300)}"
    except requests.RequestException as e:
        log.warning(f"numberfact wiki year: {e}")
        return "Wikipedia unavailable"
    except Exception as e:
        log.warning(f"numberfact wiki year parse: {e!r}")
        return "Wikipedia response parse error"


def _fetch_date_sync(mm: int, dd: int, ua: str) -> str:
    """Random event from Wikipedia's on-this-day endpoint."""
    try:
        with requests.get(
            _WIKI_ONTHISDAY.format(mm=f"{mm:02d}", dd=f"{dd:02d}"),
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10, stream=True,
        ) as r:
            r.raise_for_status()
            body = _read_capped(r)
        if body is None:
            return "Wikipedia response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
        events = d.get("events") or []
        if not events:
            return f"no recorded events for {_MONTHS[mm - 1]} {dd}"
        ev = _rng.choice(events)
        year = ev.get("year", "?")
        text = (ev.get("text") or "").strip().rstrip(".")
        if not text:
            return f"no recorded events for {_MONTHS[mm - 1]} {dd}"
        return f"On {_MONTHS[mm - 1]} {dd}: in {year}, {_truncate(text, 280)}."
    except requests.RequestException as e:
        log.warning(f"numberfact wiki date: {e}")
        return "Wikipedia unavailable"
    except Exception as e:
        log.warning(f"numberfact wiki date parse: {e!r}")
        return "Wikipedia response parse error"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> tuple[int, int] | None:
    """Parse MM/DD; return (mm, dd) or None if invalid."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if not m:
        return None
    mm = int(m.group(1))
    dd = int(m.group(2))
    if not (1 <= mm <= 12):
        return None
    if not (1 <= dd <= _DAYS_IN_MONTH[mm - 1]):
        return None
    return mm, dd


def _random_date() -> tuple[int, int]:
    mm = _rng.randint(1, 12)
    dd = _rng.randint(1, _DAYS_IN_MONTH[mm - 1])
    return mm, dd


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------

class NumberfactModule(BotModule):
    """`.numberfact <n|random|MM/DD> [type]` - number trivia/math/date/year."""

    COMMANDS: dict[str, str] = {"numberfact": "cmd_numberfact", "nf": "cmd_numberfact"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_numberfact(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return

        q_raw = "random"
        t = "trivia"
        if arg and arg.strip():
            parts = arg.strip().split()
            cand = parts[0]
            cand_l = cand.lower()
            if cand_l == "random" or cand.lstrip("-").isdigit() or "/" in cand:
                q_raw = cand_l if cand_l == "random" else cand
            else:
                self.bot.privmsg(
                    reply_to,
                    f"{nick}: numberfact <n|random|MM/DD> [trivia|math|date|year]",
                )
                return
            if len(parts) > 1:
                t = parts[1].lower()
                if t not in _TYPES:
                    self.bot.privmsg(
                        reply_to,
                        f"{nick}: type must be {'|'.join(sorted(_TYPES))}",
                    )
                    return

        # MM/DD forces date type
        if "/" in q_raw:
            t = "date"

        # ---- date ----
        if t == "date":
            if q_raw == "random":
                mm, dd = _random_date()
            else:
                parsed = _parse_date(q_raw)
                if parsed is None:
                    self.bot.privmsg(reply_to, f"{nick}: date must be MM/DD (e.g. 05/20)")
                    return
                mm, dd = parsed
            text = await asyncio.to_thread(_fetch_date_sync, mm, dd, self._ua)
            self.bot.privmsg(reply_to, _strip_ctrl(text))
            return

        # ---- year ----
        if t == "year":
            if q_raw == "random":
                year = _rng.randint(1500, 2026)
            else:
                try:
                    year = int(q_raw)
                except ValueError:
                    self.bot.privmsg(reply_to, f"{nick}: year must be an integer")
                    return
            if abs(year) > _MAX_ABS_N:
                self.bot.privmsg(reply_to, f"{nick}: year too large (max {_MAX_ABS_N:,})")
                return
            text = await asyncio.to_thread(_fetch_year_sync, year, self._ua)
            self.bot.privmsg(reply_to, _strip_ctrl(text))
            return

        # ---- math ----
        if t == "math":
            if q_raw == "random":
                n = _rng.randint(1, 2000)
            else:
                try:
                    n = int(q_raw)
                except ValueError:
                    self.bot.privmsg(reply_to, f"{nick}: n must be an integer")
                    return
                if abs(n) > _MAX_ABS_N:
                    self.bot.privmsg(reply_to, f"{nick}: n too large (max {_MAX_ABS_N:,})")
                    return
            self.bot.privmsg(reply_to, _strip_ctrl(math_fact(n)))
            return

        # ---- trivia (default) ----
        if q_raw == "random":
            n = _rng.randint(1, 2000)
        else:
            try:
                n = int(q_raw)
            except ValueError:
                self.bot.privmsg(reply_to, f"{nick}: n must be an integer")
                return
            # trivia falls back to math_fact(n) on a boilerplate/404 Wikipedia
            # result, so the same O(√n) ceiling applies here.
            if abs(n) > _MAX_ABS_N:
                self.bot.privmsg(reply_to, f"{nick}: n too large (max {_MAX_ABS_N:,})")
                return
        text = await asyncio.to_thread(_fetch_trivia_sync, n, self._ua)
        self.bot.privmsg(reply_to, _strip_ctrl(text))

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "numberfact/.nf [n] [type]", "Number trivia (type: trivia/math/date/year)")]


def setup(bot: object) -> NumberfactModule:
    return NumberfactModule(bot)  # type: ignore[arg-type]
