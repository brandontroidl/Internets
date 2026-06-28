"""Encoding / text / generator utilities - pure stdlib, no network, no key.

All commands are rate-limited and pure-compute.  Real logic lives in the
module-level ``_*`` helpers (each returns one ``str``) so they unit-test
without a bot; the ``cmd_*`` wrappers only gate, arg-check and privmsg.

    .unicode <char|U+XXXX|name>  codepoint / name / category / UTF-8 / block
    .hash <algo> <text>          md5/sha1/sha256/sha512/blake2b digest
    .crc <text>                  CRC32 + Adler-32 of UTF-8 input
    .b32 <text>                  Base32 encode, or decode if valid base32
    .slug <text>                 slugify (fold accents, hyphenate)
    .ulid                        generate a ULID (Crockford base32)
    .ascii [dec|hex|char]        dec/hex/oct/char/control-name for a byte
    .ds <value> <unit>           data-size convert (decimal + binary)
    .defang <url>                defang / refang URLs, IPs, emails (auto)
    .entropy <password>          estimate password entropy + strength
    .pw [len] [-s]               strong random password / passphrase
    .lorem [words]               N words of lorem ipsum
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
import secrets
import time
import unicodedata
import zlib
from .base import BotModule, help_row, strip_ctrl

_MAX_INPUT = 400

# ── .unicode ──────────────────────────────────────────────────────────
# Unicode block ranges (start, end, name).  unicodedata has no block API
# before 3.14's `unicodedata.UCD` extras, so carry a small useful subset.
_BLOCKS: tuple[tuple[int, int, str], ...] = (
    (0x0000, 0x007F, "Basic Latin"),
    (0x0080, 0x00FF, "Latin-1 Supplement"),
    (0x0100, 0x017F, "Latin Extended-A"),
    (0x0180, 0x024F, "Latin Extended-B"),
    (0x0250, 0x02AF, "IPA Extensions"),
    (0x0370, 0x03FF, "Greek and Coptic"),
    (0x0400, 0x04FF, "Cyrillic"),
    (0x0590, 0x05FF, "Hebrew"),
    (0x0600, 0x06FF, "Arabic"),
    (0x0900, 0x097F, "Devanagari"),
    (0x0E00, 0x0E7F, "Thai"),
    (0x1100, 0x11FF, "Hangul Jamo"),
    (0x2000, 0x206F, "General Punctuation"),
    (0x2070, 0x209F, "Superscripts and Subscripts"),
    (0x20A0, 0x20CF, "Currency Symbols"),
    (0x2100, 0x214F, "Letterlike Symbols"),
    (0x2190, 0x21FF, "Arrows"),
    (0x2200, 0x22FF, "Mathematical Operators"),
    (0x2300, 0x23FF, "Miscellaneous Technical"),
    (0x2500, 0x257F, "Box Drawing"),
    (0x2600, 0x26FF, "Miscellaneous Symbols"),
    (0x2700, 0x27BF, "Dingbats"),
    (0x3040, 0x309F, "Hiragana"),
    (0x30A0, 0x30FF, "Katakana"),
    (0x3400, 0x4DBF, "CJK Unified Ideographs Ext A"),
    (0x4E00, 0x9FFF, "CJK Unified Ideographs"),
    (0xAC00, 0xD7AF, "Hangul Syllables"),
    (0xE000, 0xF8FF, "Private Use Area"),
    (0xFB00, 0xFB4F, "Alphabetic Presentation Forms"),
    (0xFE00, 0xFE0F, "Variation Selectors"),
    (0x1F300, 0x1F5FF, "Misc Symbols and Pictographs"),
    (0x1F600, 0x1F64F, "Emoticons"),
    (0x1F680, 0x1F6FF, "Transport and Map Symbols"),
    (0x1F900, 0x1F9FF, "Supplemental Symbols and Pictographs"),
    (0x20000, 0x2A6DF, "CJK Unified Ideographs Ext B"),
)


def _block_of(cp: int) -> str:
    for start, end, name in _BLOCKS:
        if start <= cp <= end:
            return name
    return "Unknown / Unassigned block"


def _unicode(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .unicode <char|U+XXXX|name>"
    cp: int | None = None
    # U+XXXX or bare hex codepoint
    m = re.fullmatch(r"(?:[uU]\+|0[xX])?([0-9a-fA-F]{1,6})", s)
    if len(s) == 1:
        cp = ord(s)
    elif s.upper().startswith("U+") or (m and (s[:2].lower() in ("u+", "0x"))):
        try:
            cp = int(m.group(1), 16)  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            cp = None
    if cp is None and m and len(s) <= 6:
        # bare hex (e.g. "1F600"); only when it actually looks like hex digits
        try:
            cp = int(s, 16)
        except ValueError:
            cp = None
    if cp is None:
        # try by name (lookup is case-insensitive on the official name)
        try:
            cp = ord(unicodedata.lookup(s.upper()))
        except KeyError:
            return f"no character named '{strip_ctrl(s, 40)}' (try a char, U+XXXX, or name)"
    if not (0 <= cp <= 0x10FFFF):
        return "codepoint out of range (U+0000–U+10FFFF)"
    ch = chr(cp)
    name = unicodedata.name(ch, "(no name)")
    cat = unicodedata.category(ch)
    try:
        utf8 = ch.encode("utf-8").hex(" ").upper()
    except UnicodeEncodeError:
        utf8 = "(unencodable surrogate)"
    shown = ch if cat[0] not in ("C", "Z") or ch == " " else "·"
    return (f"U+{cp:04X} '{shown}' :: {name} :: cat {cat} :: "
            f"UTF-8 {utf8} :: {_block_of(cp)}")


# ── .hash ─────────────────────────────────────────────────────────────
_HASH_ALGOS: dict[str, str] = {
    "md5": "md5", "sha1": "sha1", "sha256": "sha256",
    "sha512": "sha512", "blake2b": "blake2b",
}


def _hash(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .hash <algo> <text>  (md5/sha1/sha256/sha512/blake2b)"
    parts = s.split(None, 1)
    first = parts[0].lower()
    if first in _HASH_ALGOS:
        algo = first
        text = parts[1] if len(parts) > 1 else ""
    else:
        algo = "sha256"
        text = s
    if not text:
        return "usage: .hash <algo> <text>  (md5/sha1/sha256/sha512/blake2b)"
    try:
        digest = hashlib.new(_HASH_ALGOS[algo], text.encode("utf-8")).hexdigest()
    except (ValueError, TypeError):
        return f"unknown algo '{strip_ctrl(algo, 20)}'"
    return f"{algo}: {digest}"


# ── .crc ──────────────────────────────────────────────────────────────
def _crc(arg: str) -> str:
    if not arg.strip():
        return "usage: .crc <text>"
    raw = arg.encode("utf-8")
    c = zlib.crc32(raw) & 0xFFFFFFFF
    a = zlib.adler32(raw) & 0xFFFFFFFF
    return f"CRC32 {c:08x} :: Adler-32 {a:08x}"


# ── .b32 ──────────────────────────────────────────────────────────────
_B32_RE = re.compile(r"^[A-Z2-7]+=*$")


def _b32(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .b32 <text>"
    candidate = s.upper()
    # Valid base32: alphabet only, length multiple of 8 (with padding).
    if _B32_RE.fullmatch(candidate) and len(candidate) % 8 == 0:
        try:
            raw = base64.b32decode(candidate)
        except (binascii.Error, ValueError):
            raw = None
        if raw is not None:
            # Split the utf-8 decode out: UnicodeDecodeError subclasses
            # ValueError, so catching it in the b32decode try would wrongly
            # fall through to re-encoding valid-but-binary base32.
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return "decoded base32 is binary or invalid utf-8"
    encoded = base64.b32encode(s.encode("utf-8")).decode("ascii")
    return encoded


# ── .slug ─────────────────────────────────────────────────────────────
def _slug(arg: str) -> str:
    if not arg.strip():
        return "usage: .slug <text>"
    # NFKD then drop combining marks to fold accents to ASCII.
    folded = unicodedata.normalize("NFKD", arg)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = hyphenated.strip("-")
    return slug or "(empty)"


# ── .ulid ─────────────────────────────────────────────────────────────
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32_crockford(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _ulid() -> str:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = secrets.randbits(80)
    return _b32_crockford(ms, 10) + _b32_crockford(rand, 16)


# ── .ascii ────────────────────────────────────────────────────────────
_CTRL_NAMES: dict[int, str] = {
    0: "NUL", 1: "SOH", 2: "STX", 3: "ETX", 4: "EOT", 5: "ENQ", 6: "ACK",
    7: "BEL", 8: "BS", 9: "TAB", 10: "LF", 11: "VT", 12: "FF", 13: "CR",
    14: "SO", 15: "SI", 16: "DLE", 17: "DC1", 18: "DC2", 19: "DC3",
    20: "DC4", 21: "NAK", 22: "SYN", 23: "ETB", 24: "CAN", 25: "EM",
    26: "SUB", 27: "ESC", 28: "FS", 29: "GS", 30: "RS", 31: "US",
    32: "SPACE", 127: "DEL",
}


def _ascii(arg: str) -> str:
    s = arg.strip()
    if not s:
        return ("ASCII printable range: 32 (SPACE) – 126 (~). "
                "usage: .ascii <char|dec|hex>")
    n: int | None = None
    if len(s) == 1 and ord(s) < 256:
        n = ord(s)
    elif s.lower().startswith("0x"):
        try:
            n = int(s, 16)
        except ValueError:
            n = None
    elif s.isdigit():
        n = int(s)
    elif re.fullmatch(r"[0-9a-fA-F]{1,2}", s):
        n = int(s, 16)
    if n is None or not (0 <= n <= 255):
        return "give a single char, decimal 0-255, or hex (0x41 / 41)"
    ctrl = _CTRL_NAMES.get(n)
    if ctrl:
        glyph = ctrl
    elif n < 256:
        glyph = repr(chr(n))
    else:
        glyph = "?"
    extra = f" [{ctrl}]" if ctrl else ""
    return f"dec {n} :: hex {n:02X} :: oct {n:o} :: char {glyph}{extra}"


# ── .ds (data size) ───────────────────────────────────────────────────
_DS_UNITS: dict[str, tuple[int, int]] = {
    # name -> (decimal exponent of 1000, binary exponent of 1024)
    "b": (0, 0), "byte": (0, 0), "bytes": (0, 0),
    "kb": (1, 0), "kib": (0, 1),
    "mb": (2, 0), "mib": (0, 2),
    "gb": (3, 0), "gib": (0, 3),
    "tb": (4, 0), "tib": (0, 4),
    "pb": (5, 0), "pib": (0, 5),
}


def _fmt_size(n: float) -> str:
    return f"{n:.4g}"


def _ds(arg: str) -> str:
    s = arg.strip()
    parts = s.split()
    # accept "1.5GB" or "1.5 GB"
    if len(parts) == 1:
        m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-zA-Z]+)", parts[0])
        if not m:
            return "usage: .ds <value> <unit>  e.g. .ds 1.5 GB"
        parts = [m.group(1), m.group(2)]
    if len(parts) != 2:
        return "usage: .ds <value> <unit>  e.g. .ds 1.5 GB"
    try:
        value = float(parts[0])
    except ValueError:
        return "value must be a number - e.g. .ds 1.5 GB"
    unit = parts[1].lower()
    if unit not in _DS_UNITS:
        return f"unknown unit '{strip_ctrl(parts[1], 12)}' (B/KB/MB/GB/TB/PB or KiB/MiB/...)"
    dec_exp, bin_exp = _DS_UNITS[unit]
    total_bytes = value * (1000 ** dec_exp) * (1024 ** bin_exp)
    out = [f"{value:g} {parts[1]} = {total_bytes:,.0f} bytes"]
    for name, exp in (("KB", 1), ("MB", 2), ("GB", 3), ("TB", 4)):
        out.append(f"{_fmt_size(total_bytes / 1000 ** exp)} {name}")
    bins = []
    for name, exp in (("KiB", 1), ("MiB", 2), ("GiB", 3), ("TiB", 4)):
        bins.append(f"{_fmt_size(total_bytes / 1024 ** exp)} {name}")
    return " :: ".join([out[0]] + ["dec " + ", ".join(out[1:]), "bin " + ", ".join(bins)])


# ── .defang ───────────────────────────────────────────────────────────
def _is_defanged(s: str) -> bool:
    return any(t in s for t in ("hxxp", "[.]", "[:]", "[@]", "(.)", "[dot]"))


def _defang(arg: str) -> str:
    s = arg.strip()
    if not s:
        return "usage: .defang <url|ip|email>"
    if _is_defanged(s):
        out = (s.replace("hxxps", "https").replace("hxxp", "http")
               .replace("[.]", ".").replace("(.)", ".").replace("[dot]", ".")
               .replace("[:]", ":").replace("[@]", "@"))
        return "refanged: " + out
    out = (s.replace("https", "hxxps").replace("http", "hxxp")
           .replace("://", "[:]//").replace(".", "[.]").replace("@", "[@]"))
    return "defanged: " + out


# ── .entropy ──────────────────────────────────────────────────────────
def _entropy(arg: str) -> str:
    pw = arg
    if not pw:
        return "usage: .entropy <password>"
    pool = 0
    if re.search(r"[a-z]", pw):
        pool += 26
    if re.search(r"[A-Z]", pw):
        pool += 26
    if re.search(r"[0-9]", pw):
        pool += 10
    if re.search(r"[^a-zA-Z0-9]", pw):
        pool += 33  # rough printable-symbol count
    if pool == 0:
        return "no usable characters"
    bits = len(pw) * math.log2(pool)
    if bits < 28:
        label = "very weak"
    elif bits < 36:
        label = "weak"
    elif bits < 60:
        label = "reasonable"
    elif bits < 128:
        label = "strong"
    else:
        label = "very strong"
    # rough crack time at 1e10 guesses/sec, half the keyspace on average
    guesses = 2 ** bits / 2
    seconds = guesses / 1e10
    crack = _human_time(seconds)
    return (f"len {len(pw)} :: pool {pool} :: ~{bits:.1f} bits :: {label} :: "
            f"~{crack} at 10B/s")


def _human_time(seconds: float) -> str:
    if seconds < 1:
        return "instant"
    units = (("y", 31_557_600), ("d", 86_400), ("h", 3_600),
             ("m", 60), ("s", 1))
    for name, size in units:
        if seconds >= size:
            v = seconds / size
            if v > 1e9:
                return f"{v:.2g}{name}"
            return f"{v:,.0f}{name}"
    return "instant"


# ── .pw ───────────────────────────────────────────────────────────────
_PW_ALPHABET = ("abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789!@#$%^&*-_=+")
_DICE_WORDS: tuple[str, ...] = (
    "apple", "river", "stone", "tiger", "cloud", "amber", "maple", "comet",
    "ember", "frost", "grove", "harbor", "ivory", "jewel", "knoll", "lunar",
    "meadow", "nectar", "orbit", "pebble", "quartz", "raven", "sable", "thorn",
    "umber", "vapor", "willow", "xenon", "yarrow", "zephyr", "cedar", "dusk",
    "flint", "glade", "hazel", "indigo", "juniper", "kelp", "lichen", "moss",
)


def _pw(arg: str) -> str:
    s = (arg or "").strip()
    passphrase = "-s" in s.split()
    length = 16
    for tok in s.split():
        if tok.isdigit():
            length = int(tok)
            break
    if passphrase:
        count = max(3, min(length if length != 16 else 5, 10))
        words = [secrets.choice(_DICE_WORDS) for _ in range(count)]
        # capitalise one word and append a digit for a little extra entropy
        idx = secrets.randbelow(count)
        words[idx] = words[idx].capitalize()
        return "-".join(words) + str(secrets.randbelow(100))
    length = max(8, min(length, 64))
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


# ── .lorem ────────────────────────────────────────────────────────────
_LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua ut enim ad minim "
    "veniam quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea "
    "commodo consequat duis aute irure dolor in reprehenderit in voluptate "
    "velit esse cillum dolore eu fugiat nulla pariatur excepteur sint "
    "occaecat cupidatat non proident sunt in culpa qui officia deserunt "
    "mollit anim id est laborum"
).split()


def _lorem(arg: str) -> str:
    s = (arg or "").strip()
    n = 20
    if s:
        if not s.isdigit():
            return "usage: .lorem [words]  (count, default 20)"
        n = int(s)
    n = max(1, min(n, 60))
    words = (_LOREM * ((n // len(_LOREM)) + 1))[:n]
    text = " ".join(words)
    return text[0].upper() + text[1:] + "."


class EncodeModule(BotModule):
    """`.unicode` / `.hash` / `.crc` / `.b32` / `.slug` / `.ulid` / `.ascii`
    / `.ds` / `.defang` / `.entropy` / `.pw` / `.lorem` - offline codecs."""

    COMMANDS: dict[str, str] = {
        "unicode": "cmd_unicode",
        "hash": "cmd_hash",
        "crc": "cmd_crc",
        "b32": "cmd_b32",
        "slug": "cmd_slug",
        "ulid": "cmd_ulid",
        "ascii": "cmd_ascii",
        "ds": "cmd_ds",
        "defang": "cmd_defang",
        "entropy": "cmd_entropy",
        "pw": "cmd_pw",
        "lorem": "cmd_lorem",
    }

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return False
        return True

    async def cmd_unicode(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}unicode <char|U+XXXX|name>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_unicode(arg[:_MAX_INPUT])))

    async def cmd_hash(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}hash <algo> <text>  (md5/sha1/sha256/sha512/blake2b)")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_hash(arg[:_MAX_INPUT])))

    async def cmd_crc(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}crc <text>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_crc(arg[:_MAX_INPUT])))

    async def cmd_b32(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}b32 <text>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_b32(arg[:_MAX_INPUT])))

    async def cmd_slug(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}slug <text>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_slug(arg[:_MAX_INPUT])))

    async def cmd_ulid(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        self.bot.privmsg(reply_to, strip_ctrl(_ulid()))

    async def cmd_ascii(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        self.bot.privmsg(reply_to, strip_ctrl(_ascii((arg or "")[:_MAX_INPUT])))

    async def cmd_ds(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}ds <value> <unit>  e.g. {p}ds 1.5 GB")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_ds(arg[:_MAX_INPUT])))

    async def cmd_defang(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}defang <url|ip|email>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_defang(arg[:_MAX_INPUT])))

    async def cmd_entropy(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or arg.strip() == "!":
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}entropy <password>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_entropy(arg[:_MAX_INPUT])))

    async def cmd_pw(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        self.bot.privmsg(reply_to, strip_ctrl(_pw((arg or "")[:_MAX_INPUT])))

    async def cmd_lorem(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        self.bot.privmsg(reply_to, strip_ctrl(_lorem((arg or "")[:_MAX_INPUT])))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "unicode <char|U+hex|name>", "Codepoint/name/category/UTF-8/block"),
            help_row(prefix, "hash <algo> <text>", "md5/sha1/sha256/sha512/blake2b digest"),
            help_row(prefix, "crc <text>", "CRC32 + Adler-32"),
            help_row(prefix, "b32 <text>", "Base32 encode/decode (auto)"),
            help_row(prefix, "slug <text>", "Slugify text"),
            help_row(prefix, "ulid", "Generate a ULID"),
            help_row(prefix, "ascii [dec|hex|char]", "ASCII dec/hex/oct/char/name"),
            help_row(prefix, "ds <value> <unit>", "Data-size convert (dec + bin)"),
            help_row(prefix, "defang <url>", "Defang/refang URL/IP/email (auto)"),
            help_row(prefix, "entropy <password>", "Estimate password entropy"),
            help_row(prefix, "pw [len] [-s]", "Random password / passphrase"),
            help_row(prefix, "lorem [words]", "Lorem ipsum text"),
        ]


def setup(bot: object) -> EncodeModule:
    return EncodeModule(bot)  # type: ignore[arg-type]
