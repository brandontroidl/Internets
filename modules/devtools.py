"""Developer tools — pure stdlib, no network, no key.

    .jwt <token>            decode JWT header+payload (NO signature check)
    .semver <a> <b>         compare two semantic versions
    .uuid5 <ns> <name>      deterministic UUIDv5 (or inspect a single UUID)
    .tz <time> <from> <to>  convert a clock time between IANA zones
    .unix <signal|errno>    look up a Unix signal or errno by name/number
    .color <value>          parse #hex / rgb() / hsl() -> hex/rgb/hsl + name
    .cron <expr>            validate + explain a 5-field cron, next fire times

Every command is rate-limited.  All real logic lives in module-level pure
functions returning a str so they are unit-testable without a bot.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import colorsys
import datetime as _dt
import errno
import json
import signal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import BotModule, help_row, strip_ctrl

_MAX_INPUT = 400


# ── .jwt ───────────────────────────────────────────────────────────────
def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _fmt_ts(val: object) -> str:
    try:
        n = int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(val)
    try:
        dt = _dt.datetime.fromtimestamp(n, tz=_dt.timezone.utc)
    except (OSError, OverflowError, ValueError):
        return str(val)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _jwt(arg: str) -> str:
    token = arg.strip()
    if not token:
        return "usage: .jwt <token>"
    parts = token.split(".")
    if len(parts) < 2:
        return "invalid JWT — expected header.payload.signature"
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return "invalid JWT — header/payload not base64url JSON"
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return "invalid JWT — header/payload not JSON objects"
    alg = header.get("alg", "?")
    bits = [f"alg={alg}"]
    if header.get("typ"):
        bits.append(f"typ={header['typ']}")
    if str(alg).lower() == "none":
        bits.append("WARNING: alg=none (unsigned!)")
    for claim in ("iss", "sub", "aud", "jti"):
        if claim in payload:
            bits.append(f"{claim}={payload[claim]}")
    for claim in ("iat", "nbf", "exp"):
        if claim in payload:
            bits.append(f"{claim}={_fmt_ts(payload[claim])}")
    # surface a couple of remaining non-time claims for context
    seen = {"iss", "sub", "aud", "jti", "iat", "nbf", "exp"}
    extra = [k for k in payload if k not in seen]
    for k in extra[:3]:
        bits.append(f"{k}={payload[k]}")
    return " :: ".join(str(b) for b in bits)


# ── .semver ────────────────────────────────────────────────────────────
def _semver_parse(v: str) -> tuple[tuple[int, int, int], list, str]:
    s = v.strip()
    if s.startswith("v") or s.startswith("V"):
        s = s[1:]
    build = ""
    if "+" in s:
        s, build = s.split("+", 1)
    pre = ""
    if "-" in s:
        s, pre = s.split("-", 1)
    core = s.split(".")
    if len(core) != 3:
        raise ValueError("core must be major.minor.patch")
    nums = tuple(int(x) for x in core)  # raises ValueError on non-int
    if any(n < 0 for n in nums):
        raise ValueError("negative version component")
    pre_ids: list = []
    if pre:
        for ident in pre.split("."):
            if ident == "":
                raise ValueError("empty pre-release identifier")
            if ident.isdigit():
                pre_ids.append((0, int(ident)))  # numeric < alphanumeric
            else:
                pre_ids.append((1, ident))
    return nums, pre_ids, build  # type: ignore[return-value]


def _semver_cmp(a, b) -> int:
    (na, pa, _), (nb, pb, _) = a, b
    if na != nb:
        return -1 if na < nb else 1
    # a version WITH pre-release has lower precedence than one without
    if pa and not pb:
        return -1
    if pb and not pa:
        return 1
    if pa == pb:
        return 0
    return -1 if pa < pb else 1


def _semver(a: str, b: str) -> str:
    try:
        pa = _semver_parse(a)
        pb = _semver_parse(b)
    except (ValueError, TypeError):
        return "invalid semver — try 1.2.3 / 1.0.0-rc.1 / 2.0.0+build"
    c = _semver_cmp(pa, pb)
    sym = "<" if c < 0 else ("=" if c == 0 else ">")
    return f"{strip_ctrl(a.strip(), 60)} {sym} {strip_ctrl(b.strip(), 60)}"


# ── .uuid5 ─────────────────────────────────────────────────────────────
_NS = {
    "dns": uuid.NAMESPACE_DNS,
    "url": uuid.NAMESPACE_URL,
    "oid": uuid.NAMESPACE_OID,
    "x500": uuid.NAMESPACE_X500,
}

_VARIANTS = {
    "reserved for NCS compatibility": "NCS",
    "specified in RFC 4122": "RFC 4122",
    "reserved for Microsoft compatibility": "Microsoft",
    "reserved for future definition": "future",
}


def _uuid_inspect(s: str) -> str:
    try:
        u = uuid.UUID(s.strip())
    except (ValueError, AttributeError):
        return None  # type: ignore[return-value]
    ver = u.version if u.version is not None else "?"
    variant = _VARIANTS.get(u.variant, str(u.variant))
    return f"{u} :: version {ver} :: variant {variant}"


def _uuid5(ns: str, name: str | None) -> str:
    # single-arg form: if it's a UUID, inspect it
    if name is None:
        info = _uuid_inspect(ns)
        if info is not None:
            return info
        return "usage: .uuid5 <ns> <name>  (ns = dns/url/oid/x500 or a UUID)"
    key = ns.strip().lower()
    if key in _NS:
        namespace = _NS[key]
    else:
        try:
            namespace = uuid.UUID(ns.strip())
        except (ValueError, AttributeError):
            return "ns must be dns/url/oid/x500 or a UUID"
    return str(uuid.uuid5(namespace, name))


# ── .tz ────────────────────────────────────────────────────────────────
def _parse_clock(s: str, zone: ZoneInfo) -> _dt.datetime:
    s = s.strip()
    # full ISO datetime first
    try:
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        return dt
    except ValueError:
        pass
    # bare clock time HH:MM or HH:MM:SS — anchor to a fixed reference date
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = _dt.datetime.strptime(s, fmt).time()
        except ValueError:
            continue
        return _dt.datetime(2000, 1, 1, t.hour, t.minute, t.second, tzinfo=zone)
    raise ValueError("unrecognised time")


def _tz(time_s: str, from_z: str, to_z: str) -> str:
    try:
        src = ZoneInfo(from_z.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return f"unknown zone: {strip_ctrl(from_z.strip(), 40)}"
    try:
        dst = ZoneInfo(to_z.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return f"unknown zone: {strip_ctrl(to_z.strip(), 40)}"
    try:
        dt = _parse_clock(time_s, src)
    except ValueError:
        return "bad time — try 15:00 or 2026-01-02T15:00"
    out = dt.astimezone(dst)
    return (f"{dt.strftime('%Y-%m-%d %H:%M')} {from_z.strip()} = "
            f"{out.strftime('%Y-%m-%d %H:%M %Z')} ({to_z.strip()})")


# ── .unix ──────────────────────────────────────────────────────────────
def _unix(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .unix <signal|errno>"
    up = s.upper()
    # numeric: report both signal and errno meaning if any
    if s.lstrip("-").isdigit():
        n = int(s)
        parts: list[str] = []
        try:
            sig = signal.Signals(n)
            parts.append(f"signal {sig.name} ({sig.value})")
        except (ValueError, OverflowError):
            pass
        if n in errno.errorcode:
            parts.append(f"errno {errno.errorcode[n]}: {os_strerror(n)}")
        return " :: ".join(parts) if parts else f"no signal/errno numbered {n}"
    # by name: signal first
    name = up if up.startswith("SIG") else "SIG" + up
    try:
        sig = signal.Signals[name]
        return f"{sig.name} = signal {sig.value}"
    except KeyError:
        pass
    if hasattr(errno, up):
        num = getattr(errno, up)
        return f"{up} = errno {num}: {os_strerror(num)}"
    return f"unknown signal/errno: {strip_ctrl(s, 40)}"


def os_strerror(n: int) -> str:
    import os  # noqa: PLC0415
    try:
        return os.strerror(n)
    except (ValueError, OverflowError):
        return "?"


# ── .color ─────────────────────────────────────────────────────────────
_CSS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "lime": (0, 255, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "yellow": (255, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
    "silver": (192, 192, 192), "gray": (128, 128, 128), "maroon": (128, 0, 0),
    "olive": (128, 128, 0), "purple": (128, 0, 128), "teal": (0, 128, 128),
    "navy": (0, 0, 128), "orange": (255, 165, 0), "pink": (255, 192, 203),
    "brown": (165, 42, 42), "gold": (255, 215, 0), "indigo": (75, 0, 130),
    "violet": (238, 130, 238), "tan": (210, 180, 140), "beige": (245, 245, 220),
    "coral": (255, 127, 80), "salmon": (250, 128, 114), "khaki": (240, 230, 140),
    "crimson": (220, 20, 60), "turquoise": (64, 224, 208),
}


def _clamp(x: float) -> int:
    return max(0, min(255, int(round(x))))


def _parse_color(s: str) -> tuple[int, int, int]:
    s = s.strip().lower()
    if s in _CSS:
        return _CSS[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError("hex must be #rgb or #rrggbb")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    if s.startswith("rgb"):
        inner = s[s.index("(") + 1:s.index(")")]
        nums = [p.strip() for p in inner.split(",")]
        if len(nums) < 3:
            raise ValueError("rgb needs 3 components")
        return (_clamp(float(nums[0])), _clamp(float(nums[1])), _clamp(float(nums[2])))
    if s.startswith("hsl"):
        inner = s[s.index("(") + 1:s.index(")")]
        nums = [p.strip().rstrip("%") for p in inner.split(",")]
        if len(nums) < 3:
            raise ValueError("hsl needs 3 components")
        h = float(nums[0]) / 360.0
        sat = float(nums[1]) / 100.0
        lig = float(nums[2]) / 100.0
        r, g, b = colorsys.hls_to_rgb(h, lig, sat)
        return (_clamp(r * 255), _clamp(g * 255), _clamp(b * 255))
    raise ValueError("unrecognised color")


def _nearest_css(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    best = None
    best_d = None
    for name, (cr, cg, cb) in _CSS.items():
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best = name
    return best  # type: ignore[return-value]


def _color(arg: str) -> str:
    if not arg.strip():
        return "usage: .color <#hex|rgb(...)|hsl(...)|name>"
    try:
        r, g, b = _parse_color(arg)
    except (ValueError, IndexError):
        return "bad color — try #ff8800, rgb(255,136,0), hsl(32,100%,50%)"
    hx = f"#{r:02x}{g:02x}{b:02x}"
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    hsl = f"hsl({round(h * 360)},{round(s * 100)}%,{round(l * 100)}%)"
    name = _nearest_css((r, g, b))
    exact = "" if _CSS.get(name) == (r, g, b) else "~"
    return f"{hx} :: rgb({r},{g},{b}) :: {hsl} :: {exact}{name}"


# ── .cron ──────────────────────────────────────────────────────────────
_CRON_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_CRON_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_CRON_FIELDS = ["minute", "hour", "day-of-month", "month", "day-of-week"]


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("step must be positive")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start = _cron_num(a, lo, hi)
            end = _cron_num(b, lo, hi)
        else:
            start = end = _cron_num(rng, lo, hi)
        # Bound start/end to the field BEFORE materializing range(): without
        # this, '0-999999999' would build a billion-element set (event-loop
        # freeze / OOM) before any post-loop bounds check could reject it.
        if not (lo <= start <= hi and lo <= end <= hi):
            raise ValueError("value out of range")
        if start > end:
            raise ValueError("range start > end")
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError("empty field")
    return out


def _cron_num(tok: str, lo: int, hi: int) -> int:
    t = tok.strip().lower()
    if t in _CRON_NAMES:
        return _CRON_NAMES[t]
    n = int(t)
    # cron allows 7 for Sunday
    if lo == 0 and hi == 6 and n == 7:
        return 0
    return n


def _cron_explain(fields: list[str]) -> str:
    mn, hr, dom, mon, dow = fields
    bits = []
    if mn == "*" and hr == "*":
        bits.append("every minute")
    elif mn != "*" and hr != "*" and "," not in mn and "-" not in mn \
            and "/" not in mn and "," not in hr and "-" not in hr and "/" not in hr:
        bits.append(f"at {int(hr):02d}:{int(mn):02d}")
    else:
        bits.append(f"min={mn} hr={hr}")
    if dom != "*":
        bits.append(f"day-of-month {dom}")
    if mon != "*":
        bits.append(f"month {mon}")
    if dow != "*":
        bits.append(f"day-of-week {dow}")
    return ", ".join(bits)


def _cron_matches(dt: _dt.datetime, sets: list[set[int]]) -> bool:
    mins, hrs, doms, mons, dows = sets
    if dt.minute not in mins or dt.hour not in hrs or dt.month not in mons:
        return False
    # cron dow: 0=Sunday..6=Saturday; python weekday: 0=Monday..6=Sunday
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = doms != set(range(1, 32))
    dow_restricted = dows != set(range(0, 7))
    if dom_restricted and dow_restricted:
        return dt.day in doms or cron_dow in dows
    if dom_restricted:
        return dt.day in doms
    if dow_restricted:
        return cron_dow in dows
    return True


def _cron(expr: str, now: _dt.datetime) -> str:
    fields = expr.split()
    if len(fields) != 5:
        return "cron needs 5 fields: min hour dom month dow"
    try:
        sets = [_cron_field(fields[i], *_CRON_BOUNDS[i]) for i in range(5)]
    except (ValueError, IndexError):
        return "invalid cron — check field ranges/syntax"
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    explain = _cron_explain(fields)
    # Cheap impossible-date short-circuit: when day-of-week is unrestricted, a
    # fire needs a real (month, day) pair.  If none exists (e.g. "0 0 30 2 *" =
    # Feb 30), skip the full ~527k-iteration 366-day minute scan.  (With dow
    # ALSO restricted, cron uses OR semantics, so the scan is still required.)
    _mins, _hrs, _doms, _mons, _dows = sets
    if _dows == set(range(0, 7)):
        _DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                          7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
        if not any(d <= _DAYS_IN_MONTH[mon] for mon in _mons for d in _doms):
            return f"{explain} :: next no fire within 1y"
    # scan forward minute-by-minute up to ~366 days for next fire times
    cur = now.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    limit = now + _dt.timedelta(days=366)
    fires: list[str] = []
    while cur <= limit and len(fires) < 2:
        if _cron_matches(cur, sets):
            fires.append(cur.strftime("%Y-%m-%d %H:%M UTC"))
        cur += _dt.timedelta(minutes=1)
    nxt = " ; ".join(fires) if fires else "no fire within 1y"
    return f"{explain} :: next {nxt}"


class DevtoolsModule(BotModule):
    """`.jwt` / `.semver` / `.uuid5` / `.tz` / `.unix` / `.color` / `.cron`."""

    COMMANDS: dict[str, str] = {
        "jwt": "cmd_jwt",
        "semver": "cmd_semver",
        "uuid5": "cmd_uuid5",
        "tz": "cmd_tz",
        "unix": "cmd_unix",
        "color": "cmd_color",
        "cron": "cmd_cron",
    }

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_jwt(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}jwt <token>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_jwt(arg[:_MAX_INPUT])))

    async def cmd_semver(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split()
        if len(parts) != 2:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}semver <a> <b>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_semver(parts[0][:80], parts[1][:80])))

    async def cmd_uuid5(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split(maxsplit=1)
        if not parts:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}uuid5 <ns> <name>  (ns=dns/url/oid/x500 or a UUID)")
            return
        ns = parts[0][:80]
        name = parts[1][:_MAX_INPUT] if len(parts) > 1 else None
        self.bot.privmsg(reply_to, strip_ctrl(_uuid5(ns, name)))

    async def cmd_tz(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split()
        if len(parts) != 3:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}tz <time> <from> <to>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_tz(parts[0][:40], parts[1][:60], parts[2][:60])))

    async def cmd_unix(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}unix <signal|errno>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_unix(arg[:40])))

    async def cmd_color(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}color <#hex|rgb(...)|hsl(...)|name>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_color(arg[:_MAX_INPUT])))

    async def cmd_cron(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cron <expr>  e.g. */15 0 * * 1-5")
            return
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        # The next-fire scan can walk ~527k minutes; run it off the event loop
        # so a never-matching expression can't freeze the whole bot.
        result = await asyncio.to_thread(_cron, arg[:_MAX_INPUT], now)
        self.bot.privmsg(reply_to, strip_ctrl(result))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "jwt <token>", "Decode JWT header+payload (no sig check)"),
            help_row(prefix, "semver <a> <b>", "Compare two semantic versions"),
            help_row(prefix, "uuid5 <ns> <name>", "Deterministic UUIDv5 / inspect a UUID"),
            help_row(prefix, "tz <time> <from> <to>", "Convert a clock time between zones"),
            help_row(prefix, "unix <signal|errno>", "Look up a Unix signal or errno"),
            help_row(prefix, "color <value>", "hex/rgb/hsl convert + nearest CSS name"),
            help_row(prefix, "cron <expr>", "Validate/explain cron + next fire times"),
        ]


def setup(bot: object) -> DevtoolsModule:
    return DevtoolsModule(bot)  # type: ignore[arg-type]
