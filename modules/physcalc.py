"""Physics & engineering calculators — pure stdlib, no network, no key.

    .ly <distance>          light travel time <-> distance (ly/au/km/s/min/hr)
    .sr <v>                 special relativity (Lorentz gamma) for v as frac of c
    .escape <body|m r>      escape velocity + surface gravity (builtin bodies or SI)
    .ohm <two of V,I,R,P>   Ohm-law + power solver
    .rc <bands|ohms>        resistor color code <-> value, bidirectional
    .baud <bytes> <bps>     serial transfer time for N bytes at bps [-fmt 8N1]
"""
from __future__ import annotations

import math
import re
from .base import BotModule, help_row, strip_ctrl

_MAX_INPUT = 120

# ── physical constants (SI) ──────────────────────────────────────────────
_C = 299_792_458.0           # speed of light, m/s
_G = 6.674_30e-11            # gravitational constant, m^3 kg^-1 s^-2
_AU_M = 1.495_978_707e11     # astronomical unit, m
_LY_M = 9.460_730_472_5808e15  # light-year, m
_KM_M = 1_000.0


def _fmt(x: float, sig: int = 6) -> str:
    """Compact human number: fixed for sane magnitudes, sci otherwise."""
    if x == 0:
        return "0"
    ax = abs(x)
    if ax < 1e-4 or ax >= 1e12:
        return f"{x:.{sig - 1}e}"
    s = f"{x:,.{sig}g}"
    return s


# ── .ly  light travel time <-> distance ──────────────────────────────────
# value-units in metres; time-units converted via c.
_LY_DIST_UNITS = {
    "ly": _LY_M, "lightyear": _LY_M, "lightyears": _LY_M,
    "au": _AU_M, "km": _KM_M, "m": 1.0,
}
_LY_TIME_UNITS = {  # seconds per unit
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0,
}


def _ly(arg: str) -> str:
    s = arg.strip().lower()
    m = re.fullmatch(r"\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-z]+)\s*", s)
    if not m:
        return "usage: .ly <distance>  e.g. 4.2 ly | 1 au | 384400 km | 8 min"
    try:
        val = float(m.group(1))
    except ValueError:
        return "invalid number"
    unit = m.group(2)
    if val < 0:
        return "value must be non-negative"
    if unit in _LY_DIST_UNITS:
        metres = val * _LY_DIST_UNITS[unit]
        t = metres / _C  # light travel time in seconds
        return (f"{_fmt(val)} {unit} = {_fmt(metres / _LY_M)} ly :: "
                f"{_fmt(metres / _AU_M)} au :: {_fmt(metres / _KM_M)} km :: "
                f"light: {_fmt_time(t)}")
    if unit in _LY_TIME_UNITS:
        t = val * _LY_TIME_UNITS[unit]
        metres = t * _C
        return (f"light-{unit} {_fmt(val)} = {_fmt(metres / _LY_M)} ly :: "
                f"{_fmt(metres / _AU_M)} au :: {_fmt(metres / _KM_M)} km")
    return "unknown unit — use ly/au/km/m or s/min/hr/day"


def _fmt_time(seconds: float) -> str:
    """Pick a friendly time unit for a light-travel duration."""
    if seconds < 1e-3:
        return f"{_fmt(seconds * 1e6)} µs"
    if seconds < 1.0:
        return f"{_fmt(seconds * 1e3)} ms"
    if seconds < 90.0:
        return f"{_fmt(seconds)} s"
    if seconds < 5400.0:
        return f"{_fmt(seconds / 60.0)} min"
    if seconds < 172800.0:
        return f"{_fmt(seconds / 3600.0)} hr"
    if seconds < 3.156e7 * 2:
        return f"{_fmt(seconds / 86400.0)} days"
    return f"{_fmt(seconds / 3.155_815e7)} yr"


# ── .sr  special relativity ──────────────────────────────────────────────
def _sr(arg: str) -> str:
    s = arg.strip().lower().rstrip("c").strip()
    try:
        beta = float(s)
    except ValueError:
        return "usage: .sr <v>  v as fraction of c, e.g. 0.99 or 0.99c"
    if not (0.0 <= beta < 1.0):
        if beta == 1.0:
            return "v = c is unphysical (gamma -> infinity)"
        return "v must be in [0, 1) as a fraction of c"
    gamma = 1.0 / math.sqrt(1.0 - beta * beta)
    v_ms = beta * _C
    return (f"v = {_fmt(beta)}c ({_fmt(v_ms / 1000.0)} km/s) :: "
            f"gamma {_fmt(gamma)} :: time dilation x{_fmt(gamma)} :: "
            f"length contraction x{_fmt(1.0 / gamma)}")


# ── .escape  escape velocity + surface gravity ───────────────────────────
# (mass_kg, radius_m)
_BODIES: dict[str, tuple[float, float]] = {
    "sun": (1.98892e30, 6.9634e8),
    "mercury": (3.3011e23, 2.4397e6),
    "venus": (4.8675e24, 6.0518e6),
    "earth": (5.97219e24, 6.371e6),
    "moon": (7.342e22, 1.7374e6),
    "mars": (6.4171e23, 3.3895e6),
    "jupiter": (1.8982e27, 6.9911e7),
    "saturn": (5.6834e26, 5.8232e7),
    "uranus": (8.6810e25, 2.5362e7),
    "neptune": (1.02413e26, 2.4622e7),
    "pluto": (1.303e22, 1.1883e6),
    "ceres": (9.3835e20, 4.69e5),
}


def _escape(arg: str) -> str:
    parts = arg.strip().lower().split()
    if len(parts) == 1 and parts[0] in _BODIES:
        mass, radius = _BODIES[parts[0]]
        label = parts[0]
    elif len(parts) == 2:
        try:
            mass = float(parts[0])
            radius = float(parts[1])
        except ValueError:
            return "usage: .escape <body|mass radius>  mass kg, radius m"
        label = "body"
    else:
        return ("usage: .escape <body|mass radius> :: bodies: "
                + ", ".join(sorted(_BODIES)))
    if mass <= 0 or radius <= 0:
        return "mass and radius must be positive"
    g = _G * mass / (radius * radius)              # surface gravity m/s^2
    v_esc = math.sqrt(2.0 * _G * mass / radius)    # escape velocity m/s
    return (f"{label}: escape velocity {_fmt(v_esc / 1000.0)} km/s "
            f"({_fmt(v_esc)} m/s) :: surface gravity {_fmt(g)} m/s^2 "
            f"({_fmt(g / 9.80665)} g)")


# ── .ohm  Ohm-law + power solver ─────────────────────────────────────────
def _ohm(arg: str) -> str:
    vals: dict[str, float] = {}
    tokens = re.findall(r"([virp])\s*=\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)",
                        arg.strip().lower())
    for key, num in tokens:
        try:
            vals[key] = float(num)
        except ValueError:
            return "invalid number"
    if len(vals) != 2:
        return "give exactly two of V,I,R,P — e.g. .ohm V=12 R=4"
    V = vals.get("v")
    I = vals.get("i")
    R = vals.get("r")
    P = vals.get("p")
    try:
        if V is not None and I is not None:
            if I == 0:
                return "current cannot be 0"
            R = V / I
            P = V * I
        elif V is not None and R is not None:
            if R == 0:
                return "resistance cannot be 0"
            I = V / R
            P = V * I
        elif V is not None and P is not None:
            if V == 0:
                return "voltage cannot be 0"
            I = P / V
            R = V / I
        elif I is not None and R is not None:
            V = I * R
            P = V * I
        elif I is not None and P is not None:
            if I == 0:
                return "current cannot be 0"
            V = P / I
            R = V / I
        elif R is not None and P is not None:
            if R < 0 or P < 0:
                return "R and P must be non-negative for this pair"
            I = math.sqrt(P / R) if R else 0.0
            V = I * R
        else:
            return "give two of V,I,R,P"
    except (ValueError, ZeroDivisionError):
        return "cannot solve from those values"
    return (f"V {_fmt(V)} V :: I {_fmt(I)} A :: "
            f"R {_fmt(R)} ohm :: P {_fmt(P)} W")


# ── .rc  resistor color code <-> value ───────────────────────────────────
_RC_DIGIT = {
    "black": 0, "brown": 1, "red": 2, "orange": 3, "yellow": 4,
    "green": 5, "blue": 6, "violet": 7, "purple": 7, "grey": 8,
    "gray": 8, "white": 9,
}
_RC_MULT = {  # color -> power of ten
    "black": 0, "brown": 1, "red": 2, "orange": 3, "yellow": 4,
    "green": 5, "blue": 6, "violet": 7, "purple": 7, "grey": 8,
    "gray": 8, "white": 9, "gold": -1, "silver": -2,
}
_RC_TOL = {  # color -> tolerance %
    "brown": 1.0, "red": 2.0, "green": 0.5, "blue": 0.25, "violet": 0.1,
    "grey": 0.05, "gray": 0.05, "gold": 5.0, "silver": 10.0,
}
_RC_DIGIT_REV = {0: "black", 1: "brown", 2: "red", 3: "orange", 4: "yellow",
                 5: "green", 6: "blue", 7: "violet", 8: "grey", 9: "white"}
_RC_MULT_REV = {-2: "silver", -1: "gold", 0: "black", 1: "brown", 2: "red",
                3: "orange", 4: "yellow", 5: "green", 6: "blue", 7: "violet",
                8: "grey", 9: "white"}


def _rc_ohms_fmt(ohms: float) -> str:
    if ohms >= 1e9:
        return f"{_fmt(ohms / 1e9)}G ohm"
    if ohms >= 1e6:
        return f"{_fmt(ohms / 1e6)}M ohm"
    if ohms >= 1e3:
        return f"{_fmt(ohms / 1e3)}k ohm"
    return f"{_fmt(ohms)} ohm"


def _rc(arg: str) -> str:
    s = arg.strip().lower()
    if not s:
        return "usage: .rc <bands|ohms>  e.g. red red brown gold | 4700"
    tokens = s.replace(",", " ").split()
    # numeric input -> 4-band colors
    if len(tokens) == 1 and re.fullmatch(r"\d*\.?\d+[kmg]?", tokens[0]):
        return _rc_from_value(tokens[0])
    # color bands -> ohms
    return _rc_from_bands(tokens)


def _rc_from_value(tok: str) -> str:
    mult_suffix = {"k": 1e3, "m": 1e6, "g": 1e9}
    scale = 1.0
    if tok[-1] in mult_suffix:
        scale = mult_suffix[tok[-1]]
        tok = tok[:-1]
    try:
        ohms = float(tok) * scale
    except ValueError:
        return "invalid resistance value"
    if ohms <= 0:
        return "resistance must be positive"
    # represent as two significant digits x 10^exp
    exp = int(math.floor(math.log10(ohms)))
    mantissa = ohms / (10 ** exp)
    d = int(round(mantissa * 10))  # two sig figs
    if d >= 100:
        d //= 10
        exp += 1
    d1, d2 = divmod(d, 10)
    mult = exp - 1
    if mult not in _RC_MULT_REV or d1 not in _RC_DIGIT_REV:
        return "value out of 4-band range"
    bands = [_RC_DIGIT_REV[d1], _RC_DIGIT_REV[d2], _RC_MULT_REV[mult], "gold"]
    encoded = (d1 * 10 + d2) * (10 ** mult)
    return (f"{_rc_ohms_fmt(ohms)} -> {' '.join(bands)} "
            f"(={_rc_ohms_fmt(encoded)} +/-5%)")


def _rc_from_bands(tokens: list[str]) -> str:
    if not (3 <= len(tokens) <= 5):
        return "give 3-5 color bands or a numeric value"
    # last band may be tolerance
    tol: float | None = None
    bands = tokens[:]
    if len(bands) >= 4 and bands[-1] in _RC_TOL and bands[-1] not in _RC_MULT:
        tol = _RC_TOL[bands.pop()]
    elif len(bands) >= 4 and bands[-1] in ("gold", "silver"):
        # gold/silver as final band: tolerance unless only 3 digits remain
        if len(bands) - 1 >= 3:
            tol = _RC_TOL.get(bands[-1])
            # keep as multiplier-or-tolerance decision: 4-band -> 2 digits+mult
            if len(bands) == 4:
                tol = _RC_TOL.get(bands.pop())
    # remaining: digits + one multiplier
    if len(bands) < 2:
        return "need at least two digit bands"
    *digits, mult_color = bands
    for c in digits:
        if c not in _RC_DIGIT:
            return f"unknown digit color: {strip_ctrl(c, 16)}"
    if mult_color not in _RC_MULT:
        return f"unknown multiplier color: {strip_ctrl(mult_color, 16)}"
    sig = 0
    for c in digits:
        sig = sig * 10 + _RC_DIGIT[c]
    ohms = sig * (10 ** _RC_MULT[mult_color])
    tol_str = f" +/-{_fmt(tol)}%" if tol is not None else ""
    return f"{' '.join(tokens)} = {_rc_ohms_fmt(ohms)}{tol_str}"


# ── .baud  serial transfer time ──────────────────────────────────────────
def _parse_framing(fmt: str) -> int | None:
    """8N1 -> bits per byte. Returns None if unparseable."""
    m = re.fullmatch(r"(\d)([noems])(\d(?:\.5)?|1\.5)", fmt.strip().lower())
    if not m:
        return None
    data = int(m.group(1))
    parity = 0 if m.group(2) == "n" else 1
    stop_raw = m.group(3)
    try:
        stop = float(stop_raw)
    except ValueError:
        return None
    # bits per frame: 1 start + data + parity + stop; round up fractional stop
    total = 1 + data + parity + stop
    return int(math.ceil(total))


def _baud(arg: str) -> str:
    s = arg.strip()
    fmt = "8N1"
    m = re.search(r"-fmt\s+(\S+)", s)
    if m:
        fmt = m.group(1)
        s = (s[:m.start()] + s[m.end():]).strip()
    parts = s.split()
    if len(parts) != 2:
        return "usage: .baud <bytes> <bps> [-fmt 8N1]"
    try:
        nbytes = float(parts[0])
        bps = float(parts[1])
    except ValueError:
        return "invalid number — usage: .baud <bytes> <bps> [-fmt 8N1]"
    if nbytes < 0 or bps <= 0:
        return "bytes must be >=0 and bps must be >0"
    bits_per_byte = _parse_framing(fmt)
    if bits_per_byte is None:
        return "bad framing — use e.g. 8N1, 7E1, 8N2"
    total_bits = nbytes * bits_per_byte
    seconds = total_bits / bps
    return (f"{_fmt(nbytes)} bytes @ {_fmt(bps)} bps ({fmt.upper()}, "
            f"{bits_per_byte} bits/byte) = {_fmt_time(seconds)} "
            f"({_fmt(total_bits)} bits)")


class PhyscalcModule(BotModule):
    """`.ly` / `.sr` / `.escape` / `.ohm` / `.rc` / `.baud` — physics calculators."""

    COMMANDS: dict[str, str] = {
        "ly": "cmd_ly",
        "sr": "cmd_sr",
        "escape": "cmd_escape",
        "ohm": "cmd_ohm",
        "rc": "cmd_rc",
        "baud": "cmd_baud",
    }

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_ly(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}ly <distance>  e.g. 4.2 ly | 1 au | 8 min")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_ly(arg[:_MAX_INPUT])))

    async def cmd_sr(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}sr <v>  v as fraction of c, e.g. 0.99c")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_sr(arg[:_MAX_INPUT])))

    async def cmd_escape(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}escape <body|mass radius>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_escape(arg[:_MAX_INPUT])))

    async def cmd_ohm(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}ohm <two of V,I,R,P>  e.g. V=12 R=4")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_ohm(arg[:_MAX_INPUT])))

    async def cmd_rc(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rc <bands|ohms>  e.g. red red brown gold | 4700")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_rc(arg[:_MAX_INPUT])))

    async def cmd_baud(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}baud <bytes> <bps> [-fmt 8N1]")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_baud(arg[:_MAX_INPUT])))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "ly <distance>", "Light time <-> distance (ly/au/km/min)"),
            help_row(prefix, "sr <v>", "Special relativity gamma for v=frac of c"),
            help_row(prefix, "escape <body|m r>", "Escape velocity + surface gravity"),
            help_row(prefix, "ohm <two of V,I,R,P>", "Ohm-law + power solver"),
            help_row(prefix, "rc <bands|ohms>", "Resistor color code <-> value"),
            help_row(prefix, "baud <bytes> <bps>", "Serial transfer time [-fmt 8N1]"),
        ]


def setup(bot: object) -> PhyscalcModule:
    return PhyscalcModule(bot)  # type: ignore[arg-type]
