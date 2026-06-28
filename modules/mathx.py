"""Math toolbox — pure stdlib, no network, no key.

    .isprime <n>            deterministic Miller-Rabin primality (big ints)
    .factor <n>             prime factorization (trial division + Pollard rho)
    .gcd <a> <b> [..]       GCD and LCM of two or more integers
    .base <n> <from> <to>   convert an integer between bases 2..36
    .stats <numbers>        count/mean/median/stdev/min/max/sum of a list
    .roman <n|numeral>      Arabic <-> Roman (1..3999), auto-detect
    .pct <expr>             "20% of 150" | "50 to 75" | "30 of 120"
    .bignum <expr>          exact "n!" | "fib(n)" | "2^n" (big-result summary)
    .const <name>           physical constant value + SI unit
"""
from __future__ import annotations

import asyncio
import math
import random
import re
import statistics
import sys
from .base import BotModule, help_row, strip_ctrl

_MAX_INPUT = 200
_MAX_ISPRIME_DIGITS = 100
_MAX_FACTOR_DIGITS = 19
_MAX_STATS_NUMS = 1000
_BIG_DIGIT_THRESHOLD = 100


# ── helpers ───────────────────────────────────────────────────────────

def _fmt_int(n: int) -> str:
    """Comma-group an int, but only when short enough to stay one tidy line."""
    s = str(n)
    return f"{n:,}" if len(s) <= 30 else s


def _is_probable_prime(n: int) -> bool:
    """Deterministic Miller-Rabin.

    The fixed witness set {2,3,5,7,11,13,17,19,23,29,31,37} is proven
    deterministic for all n < 3.3e24; well above any 100-digit input we
    might be asked, the same bases give a vanishingly small error and in
    practice act as a strong probable-prime test.
    """
    if n < 2:
        return False
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for p in small:
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for a in small:
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _smallest_factor(n: int) -> int:
    """Smallest prime factor of composite n via small-prime + trial division."""
    if n % 2 == 0:
        return 2
    i = 3
    # Bounded: only used after Miller-Rabin says composite, and we cap the
    # walk so a stubborn semiprime can't spin forever — fall back to rho.
    limit = 1 << 20
    while i <= limit:
        if n % i == 0:
            return i
        i += 2
    return _pollard_rho(n)


def _next_prime(n: int) -> int:
    cand = n + 1
    if cand <= 2:
        return 2
    if cand % 2 == 0:
        cand += 1
    while not _is_probable_prime(cand):
        cand += 2
    return cand


def _pollard_rho(n: int) -> int:
    if n % 2 == 0:
        return 2
    while True:
        x = random.randrange(2, n)
        y = x
        c = random.randrange(1, n)
        d = 1
        while d == 1:
            x = (x * x + c) % n
            y = (y * y + c) % n
            y = (y * y + c) % n
            d = math.gcd(abs(x - y), n)
        if d != n:
            return d


def _prime_factors(n: int) -> dict[int, int]:
    """Return {prime: exponent} for n >= 2 (trial division + Pollard rho)."""
    factors: dict[int, int] = {}

    def add(p: int) -> None:
        factors[p] = factors.get(p, 0) + 1

    # peel small primes first
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        while n % p == 0:
            add(p)
            n //= p

    stack = [n] if n > 1 else []
    while stack:
        m = stack.pop()
        if m == 1:
            continue
        if _is_probable_prime(m):
            add(m)
            continue
        d = _pollard_rho(m)
        stack.append(d)
        stack.append(m // d)
    return factors


# ── pure command functions (each returns a single str) ────────────────

def _isprime(arg: str) -> str:
    s = arg.strip().lstrip("+")
    if not re.fullmatch(r"\d+", s):
        return "usage: .isprime <n>  (non-negative integer)"
    if len(s) > _MAX_ISPRIME_DIGITS:
        return f"too big — cap is {_MAX_ISPRIME_DIGITS} digits"
    n = int(s)
    if n < 2:
        return f"{_fmt_int(n)} is not prime (primes are >= 2)"
    if _is_probable_prime(n):
        return f"{_fmt_int(n)} is prime :: next prime {_fmt_int(_next_prime(n))}"
    f = _smallest_factor(n)
    return (f"{_fmt_int(n)} is composite :: smallest factor {_fmt_int(f)} :: "
            f"next prime {_fmt_int(_next_prime(n))}")


def _factor(arg: str) -> str:
    s = arg.strip().lstrip("+")
    if not re.fullmatch(r"\d+", s):
        return "usage: .factor <n>  (integer >= 2)"
    if len(s) > _MAX_FACTOR_DIGITS:
        return f"too big — cap is {_MAX_FACTOR_DIGITS} digits"
    n = int(s)
    if n < 2:
        return "nothing to factor — give an integer >= 2"
    if _is_probable_prime(n):
        return f"{_fmt_int(n)} is prime"
    factors = _prime_factors(n)
    parts = []
    for p in sorted(factors):
        e = factors[p]
        parts.append(f"{p}^{e}" if e > 1 else str(p))
    return f"{n} = " + " x ".join(parts)


def _gcd(arg: str) -> str:
    toks = re.split(r"[\s,]+", arg.strip())
    toks = [t for t in toks if t]
    if len(toks) < 2:
        return "usage: .gcd <a> <b> [..]  (two or more integers)"
    if len(toks) > _MAX_STATS_NUMS:
        return f"too many numbers — cap is {_MAX_STATS_NUMS}"
    nums: list[int] = []
    for t in toks:
        try:
            nums.append(abs(int(t)))
        except ValueError:
            return f"not an integer: {strip_ctrl(t, 20)}"
    g = math.gcd(*nums)
    try:
        lcm = math.lcm(*nums)
    except ValueError:
        lcm = 0
    return f"gcd = {_fmt_int(g)} :: lcm = {_fmt_int(lcm)}"


def _base(arg: str) -> str:
    toks = arg.split()
    if len(toks) != 3:
        return "usage: .base <n> <from> <to>  (bases 2..36)"
    num_s, from_s, to_s = toks
    try:
        from_b = int(from_s)
        to_b = int(to_s)
    except ValueError:
        return "bases must be integers 2..36"
    if not (2 <= from_b <= 36 and 2 <= to_b <= 36):
        return "bases must be in 2..36"
    neg = num_s.startswith("-")
    body = num_s[1:] if neg or num_s.startswith("+") else num_s
    if not body:
        return "no number given"
    try:
        value = int(body, from_b)
    except ValueError:
        return f"'{strip_ctrl(num_s, 40)}' is not a valid base-{from_b} number"
    if neg:
        value = -value
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    v = abs(value)
    if v == 0:
        out = "0"
    else:
        chars = []
        while v:
            chars.append(digits[v % to_b])
            v //= to_b
        out = "".join(reversed(chars))
    if value < 0:
        out = "-" + out
    return f"{strip_ctrl(num_s, 60)} (base {from_b}) = {out} (base {to_b})"


def _stats(arg: str) -> str:
    toks = re.split(r"[\s,]+", arg.strip())
    toks = [t for t in toks if t]
    if not toks:
        return "usage: .stats <n1 n2 ...>  (whitespace or comma separated)"
    if len(toks) > _MAX_STATS_NUMS:
        return f"too many numbers — cap is {_MAX_STATS_NUMS}"
    nums: list[float] = []
    for t in toks:
        try:
            nums.append(float(t))
        except ValueError:
            return f"not a number: {strip_ctrl(t, 20)}"

    def f(x: float) -> str:
        # show ints without trailing .0, floats to 6 sig figs
        if x == int(x) and abs(x) < 1e15:
            return str(int(x))
        return f"{x:.6g}"

    n = len(nums)
    mean = statistics.fmean(nums)
    median = statistics.median(nums)
    sd = statistics.stdev(nums) if n > 1 else 0.0
    return (f"n={n} :: mean={f(mean)} :: median={f(median)} :: "
            f"stdev={f(sd)} :: min={f(min(nums))} :: max={f(max(nums))} :: "
            f"sum={f(sum(nums))}")


_ROMAN_PAIRS = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_RE = re.compile(r"^[IVXLCDM]+$", re.IGNORECASE)


def _to_roman(n: int) -> str:
    out = []
    for val, sym in _ROMAN_PAIRS:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _from_roman(s: str) -> int | None:
    s = s.upper()
    total = 0
    prev = 0
    for ch in reversed(s):
        v = _ROMAN_VALUES[ch]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


def _roman(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .roman <1..3999 | numeral>"
    if re.fullmatch(r"\d+", s):
        n = int(s)
        if not (1 <= n <= 3999):
            return "Roman numerals cover 1..3999"
        return f"{n} = {_to_roman(n)}"
    if _ROMAN_RE.match(s):
        n = _from_roman(s)
        if n is None or not (1 <= n <= 3999):
            return f"'{strip_ctrl(s, 30)}' is not a valid Roman numeral"
        # round-trip check guards against malformed input like "IIII"/"VV"
        if _to_roman(n) != s.upper():
            return f"'{strip_ctrl(s, 30)}' is not a well-formed Roman numeral"
        return f"{s.upper()} = {n}"
    return f"'{strip_ctrl(s, 30)}' is neither an integer nor a Roman numeral"


def _num(s: str) -> float:
    return float(s)


def _pct(arg: str) -> str:
    s = arg.strip()
    low = s.lower()
    # form 1: "X% of Y"
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*%\s*of\s*(-?\d+(?:\.\d+)?)\s*", low)
    if m:
        p, base = _num(m.group(1)), _num(m.group(2))
        r = p / 100.0 * base
        return f"{m.group(1)}% of {m.group(2)} = {_pct_fmt(r)}"
    # form 2: "X to Y" -> percent change
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*to\s*(-?\d+(?:\.\d+)?)\s*", low)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if a == 0:
            return "percent change from 0 is undefined"
        change = (b - a) / abs(a) * 100.0
        arrow = "increase" if change >= 0 else "decrease"
        return f"{m.group(1)} to {m.group(2)} = {_pct_fmt(change)}% {arrow}"
    # form 3: "X of Y" -> what percent
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*of\s*(-?\d+(?:\.\d+)?)\s*", low)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        if b == 0:
            return "cannot take a percentage of 0"
        r = a / b * 100.0
        return f"{m.group(1)} is {_pct_fmt(r)}% of {m.group(2)}"
    return ("usage: .pct <expr> — '20% of 150' | '50 to 75' | '30 of 120'")


def _pct_fmt(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.4g}"


def _bignum(arg: str) -> str:
    s = arg.strip().lower().replace(" ", "")
    if not s:
        return "usage: .bignum <expr> — 'n!' | 'fib(n)' | '2^n'"
    # factorial: n!
    m = re.fullmatch(r"(\d+)!", s)
    if m:
        n = int(m.group(1))
        if n > 100000:
            return "factorial argument too large (cap 100000)"
        return _bignum_report(f"{n}!", math.factorial(n))
    # fibonacci: fib(n)
    m = re.fullmatch(r"fib\((\d+)\)", s)
    if m:
        n = int(m.group(1))
        if n > 500000:
            return "fib argument too large (cap 500000)"
        return _bignum_report(f"fib({n})", _fib(n))
    # power: a^b  (also a**b)
    m = re.fullmatch(r"(\d+)(?:\^|\*\*)(\d+)", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        # bound the result size: estimate digits = b*log10(a)
        if a > 1 and b * math.log10(a) > 1_000_000:
            return "power result too large (over ~1M digits)"
        return _bignum_report(f"{a}^{b}", a ** b)
    return "usage: .bignum <expr> — 'n!' | 'fib(n)' | '2^n'"


def _fib(n: int) -> int:
    # fast-doubling iterative
    def fd(k: int) -> tuple[int, int]:
        if k == 0:
            return (0, 1)
        a, b = fd(k >> 1)
        c = a * ((b << 1) - a)
        d = a * a + b * b
        if k & 1:
            return (d, c + d)
        return (c, d)
    return fd(n)[0]


def _bignum_report(label: str, value: int) -> str:
    # Python caps int->str at sys.get_int_max_str_digits() (default 4300) as a
    # DoS guard, but .bignum results are intentionally huge (factorial(100000)
    # is ~456k digits), so str(value) raises ValueError across most of the
    # capped range.  Raise the limit just for this controlled conversion (the
    # input caps already bound the size to ~1M digits), then restore it.
    prev = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(2_000_000)
        s = str(value)
    finally:
        sys.set_int_max_str_digits(prev)
    if len(s) <= _BIG_DIGIT_THRESHOLD:
        return f"{label} = {_fmt_int(value)}"
    return (f"{label} = {len(s)} digits :: starts {s[:20]} ... ends {s[-20:]}")


_CONSTANTS: dict[str, tuple[float, str, str]] = {
    # name: (value, unit, description)
    "c":        (299792458.0, "m/s", "speed of light"),
    "h":        (6.62607015e-34, "J*s", "Planck constant"),
    "hbar":     (1.054571817e-34, "J*s", "reduced Planck constant"),
    "g":        (6.67430e-11, "m^3 kg^-1 s^-2", "gravitational constant"),  # big G
    "e":        (1.602176634e-19, "C", "elementary charge"),
    "k":        (1.380649e-23, "J/K", "Boltzmann constant"),
    "n_a":      (6.02214076e23, "1/mol", "Avogadro constant"),
    "r":        (8.314462618, "J/(mol*K)", "molar gas constant"),
    "epsilon0": (8.8541878128e-12, "F/m", "vacuum permittivity"),
    "mu0":      (1.25663706212e-6, "N/A^2", "vacuum permeability"),
    "m_e":      (9.1093837015e-31, "kg", "electron mass"),
    "m_p":      (1.67262192369e-27, "kg", "proton mass"),
    "g_n":      (9.80665, "m/s^2", "standard gravity"),
    "atm":      (101325.0, "Pa", "standard atmosphere"),
    "sigma":    (5.670374419e-8, "W m^-2 K^-4", "Stefan-Boltzmann constant"),
    "f":        (96485.33212, "C/mol", "Faraday constant"),
}
# friendly aliases so common names resolve
_CONST_ALIASES = {
    "speed_of_light": "c", "lightspeed": "c",
    "planck": "h", "planck_constant": "h",
    "reduced_planck": "hbar",
    "big_g": "g", "gravitational_constant": "g", "newton_g": "g",
    "charge": "e", "elementary_charge": "e",
    "boltzmann": "k", "k_b": "k", "kb": "k",
    "avogadro": "n_a", "na": "n_a", "n_avogadro": "n_a",
    "gas_constant": "r", "molar_gas": "r",
    "vacuum_permittivity": "epsilon0", "eps0": "epsilon0", "epsilon_0": "epsilon0",
    "vacuum_permeability": "mu0", "mu_0": "mu0",
    "electron_mass": "m_e", "me": "m_e",
    "proton_mass": "m_p", "mp": "m_p",
    "gravity": "g_n", "little_g": "g_n", "g0": "g_n",
    "atmosphere": "atm", "stp": "atm",
    "stefan_boltzmann": "sigma",
    "faraday": "f",
}


def _const(arg: str) -> str:
    key = arg.strip().lower()
    if not key:
        avail = ", ".join(sorted(_CONSTANTS))
        return f"usage: .const <name> — known: {avail}"[:380]
    key = _CONST_ALIASES.get(key, key)
    entry = _CONSTANTS.get(key)
    if entry is None:
        return (f"unknown constant '{strip_ctrl(arg.strip(), 30)}' — try one of: "
                + ", ".join(sorted(_CONSTANTS)))[:380]
    value, unit, desc = entry
    return f"{desc} = {value:g} {unit}"


# ── module ────────────────────────────────────────────────────────────

class MathxModule(BotModule):
    """`.isprime` / `.factor` / `.gcd` / `.base` / `.stats` / `.roman` /
    `.pct` / `.bignum` / `.const` — offline math toolbox."""

    COMMANDS: dict[str, str] = {
        "isprime": "cmd_isprime",
        "factor": "cmd_factor",
        "gcd": "cmd_gcd",
        "base": "cmd_base",
        "stats": "cmd_stats",
        "roman": "cmd_roman",
        "pct": "cmd_pct",
        "bignum": "cmd_bignum",
        "const": "cmd_const",
    }

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_isprime(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}isprime <n>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_isprime(arg[:_MAX_INPUT])))

    async def cmd_factor(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}factor <n>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_factor(arg[:_MAX_INPUT])))

    async def cmd_gcd(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}gcd <a> <b> [..]")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_gcd(arg[:_MAX_INPUT])))

    async def cmd_base(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}base <n> <from> <to>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_base(arg[:_MAX_INPUT])))

    async def cmd_stats(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}stats <n1 n2 ...>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_stats(arg[:_MAX_INPUT])))

    async def cmd_roman(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}roman <1..3999 | numeral>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_roman(arg[:_MAX_INPUT])))

    async def cmd_pct(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}pct <expr>  e.g. 20% of 150")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_pct(arg[:_MAX_INPUT])))

    async def cmd_bignum(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}bignum <expr>  e.g. 50! fib(100) 2^256")
            return
        # Big-int math (factorial/fib/power up to ~1M digits) is heavy CPU; run
        # it off the event loop so a user can't freeze the whole bot per call.
        result = await asyncio.to_thread(_bignum, arg[:_MAX_INPUT])
        self.bot.privmsg(reply_to, strip_ctrl(result))

    async def cmd_const(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}const <name>  e.g. c, h, G, k")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_const(arg[:_MAX_INPUT])))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "isprime <n>", "Primality test + next prime"),
            help_row(prefix, "factor <n>", "Prime factorization"),
            help_row(prefix, "gcd <a> <b> [..]", "GCD and LCM"),
            help_row(prefix, "base <n> <from> <to>", "Convert between bases 2..36"),
            help_row(prefix, "stats <n1 n2 ...>", "Mean/median/stdev/min/max/sum"),
            help_row(prefix, "roman <n|numeral>", "Arabic <-> Roman (1..3999)"),
            help_row(prefix, "pct <expr>", "20% of 150 | 50 to 75 | 30 of 120"),
            help_row(prefix, "bignum <expr>", "Exact n! / fib(n) / 2^n"),
            help_row(prefix, "const <name>", "Physical constant value + unit"),
        ]


def setup(bot: object) -> MathxModule:
    return MathxModule(bot)  # type: ignore[arg-type]
