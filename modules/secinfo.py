"""Security information lookups — CVE / password-pwn / hash & crypto helpers.

All commands are KEYLESS.

    .cve <CVE-ID>      NVD lookup: CVSS base score+severity, summary, published date
    .pwn <password>    Have I Been Pwned k-anonymity breach count (PM-ONLY)
    .hashid <hash>     identify likely hash type from length+charset (offline)
    .cvss <vector>     parse a CVSS v3.1 vector, compute base score+severity (offline)
    .cipher <name>     bundled cipher reference: type, key/block size, status (offline)

Network commands route every outbound request through ``base.fetch_json``
(size-capped) except ``.pwn``, which fetches plain text via a size-capped
raw ``requests`` stream — and only the 5-char SHA-1 prefix ever leaves the
host (HIBP k-anonymity).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import requests
from .base import BotModule, ResponseTooLarge, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.secinfo")

_MAX_INPUT = 200

# Strict CVE id: CVE-YYYY-NNNN+ (4+ digit sequence).  Validated before any
# network call so junk input never reaches NVD.
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)

# NVD allows ~5 requests / 30s without a key; the JSON for a single CVE is
# modest but the description can be long, so a small bump over the default
# is plenty.
_NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_NVD_MAX_BYTES = 512 * 1024

# HIBP range endpoint returns ~500-1000 lines of "SUFFIX:COUNT" text.
_HIBP_URL = "https://api.pwnedpasswords.com/range/"
_HIBP_MAX_BYTES = 1024 * 1024


# ── .cve ──────────────────────────────────────────────────────────────
def _cve_sync(cve_id: str, ua: str) -> str:
    """Blocking NVD CVE lookup — run via asyncio.to_thread."""
    try:
        data = fetch_json(
            _NVD_URL,
            ua=ua,
            params={"cveId": cve_id},
            timeout=12,
            max_bytes=_NVD_MAX_BYTES,
            allow_404=True,
        )
        if not data:
            return f"{strip_ctrl(cve_id, 40)}: not found"
        vulns = data.get("vulnerabilities") or []
        if not vulns:
            return f"{strip_ctrl(cve_id, 40)}: not found"
        cve = vulns[0].get("cve", {})
        cid = cve.get("id", cve_id)

        # English description.
        desc = ""
        for d in cve.get("descriptions", []):
            if d.get("lang") == "en":
                desc = d.get("value", "")
                break
        desc = (desc or "no description").replace("\n", " ").strip()
        if len(desc) > 240:
            desc = desc[:237] + "..."

        published = (cve.get("published") or "")[:10] or "?"

        score, severity = _cve_score(cve.get("metrics", {}))
        sev_part = ""
        if score is not None:
            sev_part = f"CVSS {score}" + (f" ({severity})" if severity else "")
        else:
            sev_part = "CVSS n/a"

        return strip_ctrl(
            f"\x02{cid}\x02 — {sev_part} | {desc} | published {published}"
        )
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning(f"cve request: {e}")
        return "lookup failed"
    except (KeyError, ValueError, TypeError, AttributeError, IndexError) as e:
        log.warning(f"cve parse: {e!r}")
        return "lookup failed"


def _cve_score(metrics: dict) -> tuple[float | None, str]:
    """Pull a base score + severity, preferring CVSS v3.1 > v3.0 > v2."""
    for key in ("cvssMetricV31", "cvssMetricV30"):
        entries = metrics.get(key) or []
        if entries:
            cd = entries[0].get("cvssData", {})
            score = cd.get("baseScore")
            sev = cd.get("baseSeverity") or entries[0].get("baseSeverity") or ""
            if score is not None:
                return float(score), strip_ctrl(str(sev), 12).upper()
    v2 = metrics.get("cvssMetricV2") or []
    if v2:
        cd = v2[0].get("cvssData", {})
        score = cd.get("baseScore")
        sev = v2[0].get("baseSeverity") or ""
        if score is not None:
            return float(score), strip_ctrl(str(sev), 12).upper()
    return None, ""


# ── .pwn ──────────────────────────────────────────────────────────────
def _pwn_sync(password: str, ua: str) -> str:
    """HIBP k-anonymity breach count.  Only the 5-char SHA-1 prefix leaves.

    The full password is hashed locally; we send just the first 5 hex
    chars of the SHA-1, then match the returned suffix list ourselves.
    """
    try:
        # SHA-1 is MANDATED by HIBP's k-anonymity API (only the first 5 hex of
        # the digest leave); it is a protocol requirement, not a security hash,
        # so usedforsecurity=False documents that and clears the weak-hash alert.
        sha1 = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]
        with requests.get(
            _HIBP_URL + prefix,
            headers={"User-Agent": ua, "Add-Padding": "true"},
            timeout=12,
            stream=True,
        ) as r:
            r.raise_for_status()
            body = r.raw.read(_HIBP_MAX_BYTES + 1, decode_content=True)
            if len(body) > _HIBP_MAX_BYTES:
                return "lookup failed"
            text = body.decode("utf-8", errors="replace")
        count = 0
        for line in text.splitlines():
            parts = line.strip().split(":")
            if len(parts) == 2 and parts[0].upper() == suffix:
                try:
                    count = int(parts[1])
                except ValueError:
                    count = 0
                break
        if count > 0:
            return (f"\x02pwned\x02 — this password appears in "
                    f"{count:,} known breaches. Do not use it.")
        return "good news — this password was not found in any known breach."
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning(f"pwn request: {e}")
        return "lookup failed"
    except (ValueError, TypeError) as e:
        log.warning(f"pwn parse: {e!r}")
        return "lookup failed"


# ── .hashid (pure) ─────────────────────────────────────────────────────
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# length -> list of likely hex-encoded hash types
_HEX_BY_LEN: dict[int, list[str]] = {
    32: ["MD5", "NTLM", "MD4"],
    40: ["SHA-1", "RIPEMD-160"],
    56: ["SHA-224", "SHA3-224"],
    64: ["SHA-256", "SHA3-256", "BLAKE2s"],
    96: ["SHA-384", "SHA3-384"],
    128: ["SHA-512", "SHA3-512", "BLAKE2b", "Whirlpool"],
}


def _hashid(s: str) -> str:
    h = s.strip()
    if not h:
        return "usage: .hashid <hash>"
    safe = strip_ctrl(h, 60)

    # Prefixed / structured formats first.
    if h.startswith("$2a$") or h.startswith("$2b$") or h.startswith("$2y$"):
        return f"{safe} → bcrypt"
    if h.startswith("$argon2"):
        return f"{safe} → Argon2"
    if h.startswith("$6$"):
        return f"{safe} → sha512crypt (Unix)"
    if h.startswith("$5$"):
        return f"{safe} → sha256crypt (Unix)"
    if h.startswith("$1$"):
        return f"{safe} → md5crypt (Unix)"
    if h.startswith("$y$") or h.startswith("$7$"):
        return f"{safe} → yescrypt/scrypt (Unix)"
    if h.startswith("{SSHA}"):
        return f"{safe} → SSHA (LDAP)"

    if _HEX_RE.match(h):
        cands = _HEX_BY_LEN.get(len(h))
        if cands:
            return f"{safe} → {', '.join(cands)} (hex, {len(h)} chars)"
        return f"{safe} → unknown hex hash ({len(h)} chars)"

    if _B64_RE.match(h) and len(h) >= 16:
        return f"{safe} → possibly base64-encoded hash ({len(h)} chars)"

    return f"{safe} → unrecognised hash format"


# ── .cvss (pure) ────────────────────────────────────────────────────────
# CVSS v3.1 base-metric weight tables.
_CVSS_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_CVSS_AC = {"L": 0.77, "H": 0.44}
_CVSS_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}   # Scope Unchanged
_CVSS_PR_C = {"N": 0.85, "L": 0.68, "H": 0.5}    # Scope Changed
_CVSS_UI = {"N": 0.85, "R": 0.62}
_CVSS_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _cvss_roundup(x: float) -> float:
    """CVSS v3.1 spec 'roundup' — round up to one decimal place."""
    i = int(round(x * 100000))
    if i % 10000 == 0:
        return i / 100000.0
    return (i // 10000 + 1) / 10.0


def _cvss_severity(score: float) -> str:
    if score == 0.0:
        return "None"
    if score < 4.0:
        return "Low"
    if score < 7.0:
        return "Medium"
    if score < 9.0:
        return "High"
    return "Critical"


def _cvss(vector: str) -> str:
    v = vector.strip()
    if not v:
        return "usage: .cvss <CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H>"
    parts = {}
    for token in v.split("/"):
        if ":" in token:
            k, _, val = token.partition(":")
            parts[k.upper()] = val.upper()
    if parts.get("CVSS") and not parts["CVSS"].startswith("3"):
        return "only CVSS v3.x vectors supported"
    try:
        av = _CVSS_AV[parts["AV"]]
        ac = _CVSS_AC[parts["AC"]]
        ui = _CVSS_UI[parts["UI"]]
        scope = parts["S"]
        pr = (_CVSS_PR_C if scope == "C" else _CVSS_PR_U)[parts["PR"]]
        c = _CVSS_CIA[parts["C"]]
        i = _CVSS_CIA[parts["I"]]
        a = _CVSS_CIA[parts["A"]]
    except KeyError:
        return "invalid/incomplete CVSS vector (need AV/AC/PR/UI/S/C/I/A)"
    if scope not in ("U", "C"):
        return "invalid Scope (S:U or S:C)"

    iss = 1.0 - ((1 - c) * (1 - i) * (1 - a))
    if scope == "C":
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    exploitability = 8.22 * av * ac * pr * ui

    if impact <= 0:
        base = 0.0
    elif scope == "C":
        base = _cvss_roundup(min(1.08 * (impact + exploitability), 10.0))
    else:
        base = _cvss_roundup(min(impact + exploitability, 10.0))

    return f"CVSS v3.1 base {base} ({_cvss_severity(base)})"


# ── .cipher (pure) ──────────────────────────────────────────────────────
_CIPHERS: dict[str, str] = {
    "aes": "AES — symmetric block | block 128b | key 128/192/256b | status: secure",
    "aes-128": "AES-128 — symmetric block | block 128b | key 128b | status: secure",
    "aes-256": "AES-256 — symmetric block | block 128b | key 256b | status: secure",
    "des": "DES — symmetric block | block 64b | key 56b | status: broken (key too short)",
    "3des": "3DES — symmetric block | block 64b | key 112/168b | status: weak/deprecated",
    "triple-des": "3DES — symmetric block | block 64b | key 112/168b | status: weak/deprecated",
    "blowfish": "Blowfish — symmetric block | block 64b | key 32-448b | status: weak (64b block)",
    "twofish": "Twofish — symmetric block | block 128b | key 128/192/256b | status: secure",
    "rc4": "RC4 — symmetric stream | key 40-2048b | status: broken (biased keystream)",
    "chacha20": "ChaCha20 — symmetric stream | key 256b | status: secure",
    "salsa20": "Salsa20 — symmetric stream | key 128/256b | status: secure",
    "rsa": "RSA — asymmetric | key >=2048b recommended | status: secure (>=2048b)",
    "ecc": "ECC — asymmetric | key 256b ~ RSA-3072 | status: secure",
    "ecdsa": "ECDSA — asymmetric signature | key 256/384b | status: secure",
    "ed25519": "Ed25519 — asymmetric signature | key 256b | status: secure",
    "dh": "Diffie-Hellman — key exchange | >=2048b group | status: secure (>=2048b)",
    "md5": "MD5 — hash | digest 128b | status: broken (collisions)",
    "sha1": "SHA-1 — hash | digest 160b | status: broken (collisions)",
    "sha-1": "SHA-1 — hash | digest 160b | status: broken (collisions)",
    "sha256": "SHA-256 — hash | digest 256b | status: secure",
    "sha-256": "SHA-256 — hash | digest 256b | status: secure",
    "sha512": "SHA-512 — hash | digest 512b | status: secure",
    "sha3": "SHA-3 — hash | digest 224/256/384/512b | status: secure",
    "bcrypt": "bcrypt — password hash | adaptive cost | status: secure",
    "scrypt": "scrypt — password hash | memory-hard | status: secure",
    "argon2": "Argon2 — password hash | memory-hard | status: secure (recommended)",
    "pbkdf2": "PBKDF2 — password hash/KDF | configurable iters | status: acceptable",
}


def _cipher(name: str) -> str:
    n = name.strip().lower()
    if not n:
        return "usage: .cipher <name>  e.g. .cipher aes"
    info = _CIPHERS.get(n)
    if info:
        return info
    # forgiving lookup: drop separators
    alt = n.replace("-", "").replace("_", "").replace(" ", "")
    for k, v in _CIPHERS.items():
        if k.replace("-", "") == alt:
            return v
    return f"no reference for '{strip_ctrl(n, 30)}' — try aes, rsa, sha256, bcrypt, ..."


class SecinfoModule(BotModule):
    """`.cve` / `.pwn` / `.hashid` / `.cvss` / `.cipher` — security helpers."""

    COMMANDS: dict[str, str] = {
        "cve": "cmd_cve",
        "pwn": "cmd_pwn",
        "hashid": "cmd_hashid",
        "cvss": "cmd_cvss",
        "cipher": "cmd_cipher",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_cve(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        cve_id = (arg or "").strip()
        if not _CVE_RE.match(cve_id):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cve <CVE-YYYY-NNNN>  e.g. {p}cve CVE-2021-44228")
            return
        result = await asyncio.to_thread(_cve_sync, cve_id.upper(), self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_pwn(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        # PM-ONLY: in a channel reply_to is the channel, not the nick.
        if reply_to != nick:
            self.bot.notice(nick, f"{nick}: please PM me that command — never type a password in a channel")
            return
        password = arg or ""
        if not password:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}pwn <password>  (PM only; only a hash prefix is sent)")
            return
        result = await asyncio.to_thread(_pwn_sync, password, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_hashid(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}hashid <hash>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_hashid(arg[:_MAX_INPUT])))

    async def cmd_cvss(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cvss <CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_cvss(arg[:_MAX_INPUT])))

    async def cmd_cipher(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cipher <name>  e.g. {p}cipher aes")
            return
        self.bot.privmsg(reply_to, strip_ctrl(_cipher(arg[:_MAX_INPUT])))

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "cve <CVE-ID>", "NVD CVSS score, summary, date"),
            help_row(prefix, "pwn <password>", "HIBP breach count (PM-only)"),
            help_row(prefix, "hashid <hash>", "Identify likely hash type"),
            help_row(prefix, "cvss <vector>", "Compute CVSS v3.1 base score"),
            help_row(prefix, "cipher <name>", "Cipher reference (size/status)"),
        ]


def setup(bot: object) -> SecinfoModule:
    return SecinfoModule(bot)  # type: ignore[arg-type]
