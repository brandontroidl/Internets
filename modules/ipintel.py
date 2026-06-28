"""IP reputation aggregator — .ip / .rep — keyless multi-source threat intel.

    .ip <ip|host>    one-line reputation across DNSBLs, SANS ISC / DShield,
                     GreyNoise, the Tor exit list, and AbuseIPDB (if a key
                     is configured)

Every source is keyless except AbuseIPDB, which uses the optional
``abuseipdb_key`` secret (the command degrades gracefully without it).

Safety model: the target is resolved to ONE public IP through the shared
SSRF-safe resolver (``_netsafe.resolve_safe_ip``) — private / loopback /
link-local / reserved / unresolvable targets are refused before any request
goes out, and an internal IP can never be leaked to a third party.  The
validated IP only ever appears as a query parameter / path segment against
FIXED, trusted endpoints (never a user-controlled URL), so there is no SSRF
surface here the way there is for ``probe`` / ``scinews``.  Every upstream
string is run through ``strip_ctrl``; every outbound body is size-capped.

DNSBL lookups go over Cloudflare DNS-over-HTTPS (a large public resolver),
so Spamhaus ZEN — which deliberately refuses public resolvers — is NOT in
the default zone set; the zones here all answer public-resolver queries.
A returned A record in 127.0.0.0/8 (excluding the 127.255.255.0/24
"query refused" sentinel) counts as a listing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import threading
import time
from urllib.parse import quote

import requests

from .base import BotModule, ResponseTooLarge, fetch_json, help_row, strip_ctrl
from ._netsafe import resolve_safe_ip

log = logging.getLogger("internets.ipintel")

# Conservative charset for the raw IRC token before it reaches the resolver:
# letters, digits, dot, colon (IPv6), hyphen, underscore.  Anything else is
# rejected without a request.  resolve_safe_ip does the real IP validation.
_TARGET_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")

# DNSBL zones queried over Cloudflare DoH.  Only zones that answer queries
# from large public resolvers belong here (Spamhaus ZEN refuses them and
# would always read as "clean", which is worse than absent).
_DNSBL_ZONES: tuple[tuple[str, str], ...] = (
    ("dnsbl.dronebl.org",      "DroneBL"),
    ("bl.spamcop.net",         "SpamCop"),
    ("psbl.surriel.com",       "PSBL"),
    ("dnsbl-1.uceprotect.net", "UCEPROTECT"),
    ("all.s5h.net",            "s5h"),
    ("truncate.gbudb.net",     "GBUdb"),
)
# A DNSBL "listed" answer lives in 127.0.0.0/8; 127.255.255.0/24 is the
# public-resolver / error sentinel several zones return — never a listing.
_DNSBL_LISTED_NET = ipaddress.ip_network("127.0.0.0/8")
_DNSBL_SENTINEL_NET = ipaddress.ip_network("127.255.255.0/24")

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_DOH_HEADERS = {"Accept": "application/dns-json"}

_ISC_URL = "https://isc.sans.edu/api/ip/"
_GN_URL = "https://api.greynoise.io/v3/community/"
_ABUSE_URL = "https://api.abuseipdb.com/api/v2/check"
_TOR_URL = "https://check.torproject.org/torbulkexitlist"

_ISC_MAX = 64 * 1024
_GN_MAX = 16 * 1024
_ABUSE_MAX = 32 * 1024
_TOR_MAX = 4 * 1024 * 1024
_TOR_TTL = 3600          # seconds; the bulk exit list changes slowly
# The bot's sender splits PRIVMSG bodies at ~400 bytes, so keep one .ip reply
# to a single message (realistic lines are far shorter).  Every untrusted
# field is strip_ctrl'd individually, so the assembled line is sanitized only
# for transport bytes (\r\n\x00) at the end — it must NOT be run through
# strip_ctrl again or that would delete the intentional \x02 emphasis codes.
_MAX_LINE = 400
_TRANSPORT_RE = re.compile(r"[\r\n\x00]")

# Caught around every outbound helper so a single dead source never breaks
# the whole reply.  json.JSONDecodeError is a ValueError subclass.
_NET_ERRORS = (requests.RequestException, ResponseTooLarge,
               ValueError, TypeError, KeyError)


# ── DNSBL (over Cloudflare DoH) ────────────────────────────────────────
def _dnsbl_name(ip: str, zone: str) -> str | None:
    """Reversed-octet DNSBL query name for an IPv4 address, or None.

    The default zones are IPv4-only, so IPv6 targets return None and are
    reported as ``DNSBL n/a`` rather than silently "clean".
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return ".".join(reversed(ip.split("."))) + "." + zone


def _dnsbl_one(ip: str, zone: str, ua: str) -> int:
    """Query one DNSBL zone.  1 = listed, 0 = not listed, -1 = unknown/error."""
    name = _dnsbl_name(ip, zone)
    if name is None:
        return -1
    try:
        data = fetch_json(
            _DOH_URL, ua=ua,
            params={"name": name, "type": "A"},
            headers=_DOH_HEADERS, timeout=6, max_bytes=16 * 1024,
        )
        if not isinstance(data, dict):
            return -1
        if data.get("Status") == 3:          # NXDOMAIN -> not listed
            return 0
        for ans in data.get("Answer", []) or []:
            if not isinstance(ans, dict) or ans.get("type") != 1:
                continue
            try:
                a = ipaddress.ip_address(str(ans.get("data", "")))
            except ValueError:
                continue
            if a in _DNSBL_LISTED_NET and a not in _DNSBL_SENTINEL_NET:
                return 1
        return 0
    except _NET_ERRORS as e:
        log.warning("dnsbl %s: %s", zone, e)
        return -1
    except Exception as e:  # noqa: BLE001 — one dead source must not break the reply
        log.warning("dnsbl %s (unexpected): %s", zone, e)
        return -1


# ── SANS ISC / DShield ─────────────────────────────────────────────────
def _dshield_sync(ip: str, ua: str) -> dict | None:
    """SANS ISC / DShield IP summary dict (the ``ip`` object), or None."""
    try:
        data = fetch_json(
            _ISC_URL + quote(ip, safe=":") + "?json",
            ua=ua, timeout=8, max_bytes=_ISC_MAX, allow_404=True,
        )
        if not isinstance(data, dict):
            return None
        info = data.get("ip")
        return info if isinstance(info, dict) else None
    except _NET_ERRORS as e:
        log.warning("dshield: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — one dead source must not break the reply
        log.warning("dshield (unexpected): %s", e)
        return None


# ── GreyNoise community ────────────────────────────────────────────────
def _greynoise_sync(ip: str, ua: str) -> dict | None:
    """GreyNoise community record, ``{'classification': 'unseen'}`` for an
    un-observed IP (HTTP 404), or None on error / rate-limit."""
    try:
        data = fetch_json(
            _GN_URL + quote(ip, safe=":"),
            ua=ua, timeout=8, max_bytes=_GN_MAX, allow_404=True,
        )
        if data is None:                      # 404 -> not observed
            return {"classification": "unseen"}
        return data if isinstance(data, dict) else None
    except _NET_ERRORS as e:
        log.warning("greynoise: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — one dead source must not break the reply
        log.warning("greynoise (unexpected): %s", e)
        return None


# ── AbuseIPDB (keyed, optional) ────────────────────────────────────────
def _abuseipdb_sync(ip: str, ua: str, key: str) -> dict | None:
    """AbuseIPDB v2 check ``data`` object, or None (incl. when no key)."""
    if not key:
        return None
    try:
        data = fetch_json(
            _ABUSE_URL, ua=ua,
            params={"ipAddress": ip, "maxAgeInDays": "90"},
            headers={"Key": key, "Accept": "application/json"},
            timeout=8, max_bytes=_ABUSE_MAX,
        )
        if not isinstance(data, dict):
            return None
        d = data.get("data")
        return d if isinstance(d, dict) else None
    except _NET_ERRORS as e:
        log.warning("abuseipdb: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — one dead source must not break the reply
        log.warning("abuseipdb (unexpected): %s", e)
        return None


# ── Tor exit list (cached) ─────────────────────────────────────────────
_tor_lock = threading.Lock()
_tor_cache: dict[str, object] = {"ts": 0.0, "set": frozenset()}


def _tor_fetch(ua: str) -> frozenset[str]:
    """Download + parse the Tor bulk exit list into a set of IP strings."""
    with requests.get(_TOR_URL, headers={"User-Agent": ua},
                      timeout=10, stream=True) as r:
        r.raise_for_status()
        body = r.raw.read(_TOR_MAX + 1, decode_content=True)
        if len(body) > _TOR_MAX:
            raise ResponseTooLarge("tor exit list exceeded size cap")
    out: set[str] = set()
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return frozenset(out)


def _tor_is_exit(ip: str, ua: str) -> int:
    """1 = Tor exit, 0 = not, -1 = unknown.  Caches the list for _TOR_TTL."""
    try:
        now = time.monotonic()
        with _tor_lock:
            cached = _tor_cache["set"]
            fresh = bool(cached) and (now - float(_tor_cache["ts"])) < _TOR_TTL
        if not fresh:
            cached = _tor_fetch(ua)
            with _tor_lock:
                _tor_cache["ts"] = now
                _tor_cache["set"] = cached
        return 1 if ip in cached else 0       # type: ignore[operator]
    except (requests.RequestException, ResponseTooLarge, ValueError) as e:
        log.warning("tor: %s", e)
        return -1
    except Exception as e:  # noqa: BLE001 — one dead source must not break the reply
        log.warning("tor (unexpected): %s", e)
        return -1


# ── formatting (pure, unit-tested) ─────────────────────────────────────
def _coerce_int(v: object) -> int | None:
    try:
        return int(v)            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _verdict(listed_n: int, tor: int, gn_class: str | None,
             abuse_score: int | None, ds_count: int | None) -> str:
    gn = (gn_class or "").lower()
    if (listed_n >= 2 or tor == 1 or gn == "malicious"
            or (abuse_score is not None and abuse_score >= 50)):
        return "malicious"
    if (listed_n == 1
            or (abuse_score is not None and 25 <= abuse_score < 50)
            or (ds_count is not None and ds_count >= 10)):
        return "suspicious"
    return "clean"


def _format(ip: str, r: dict) -> str:
    """Assemble the one-line reply from collected source results (pure)."""
    segs: list[str] = []

    listed = list(r.get("dnsbl_listed") or [])
    checked = int(r.get("dnsbl_checked", 0) or 0)
    if not r.get("ipv4", True):
        segs.append("DNSBL n/a (IPv6)")
    elif checked == 0:
        segs.append("DNSBL unknown")
    elif listed:
        segs.append(f"DNSBL \x02{len(listed)}/{checked}\x02: "
                    + ", ".join(strip_ctrl(x, 20) for x in listed[:6]))
    else:
        segs.append(f"DNSBL clean (0/{checked})")

    ds = r.get("dshield")
    ds_count = None
    if isinstance(ds, dict):
        ds_count = _coerce_int(ds.get("count"))
        if ds_count:
            seg = f"DShield {ds_count} rpts"
            cc = strip_ctrl(str(ds.get("ascountry") or ""), 4)
            if cc:
                seg += f" [{cc}]"
            segs.append(seg)
        else:
            segs.append("DShield none")

    gn = r.get("greynoise")
    gn_class = None
    if isinstance(gn, dict):
        gn_class = strip_ctrl(str(gn.get("classification") or "unknown"), 16)
        if gn_class == "unseen":
            segs.append("GreyNoise unseen")
        else:
            name = strip_ctrl(str(gn.get("name") or ""), 28)
            extra = f" ({name})" if name and name.lower() not in ("", "unknown") else ""
            segs.append(f"GreyNoise {gn_class}{extra}")

    tor = int(r.get("tor", -1))
    if tor == 1:
        segs.append("\x02Tor exit\x02")
    elif tor == 0:
        segs.append("Tor no")

    ab = r.get("abuse")
    abuse_score = None
    if isinstance(ab, dict):
        abuse_score = _coerce_int(ab.get("abuseConfidenceScore"))
        if abuse_score is not None:
            seg = f"AbuseIPDB \x02{abuse_score}%\x02"
            rep = _coerce_int(ab.get("totalReports"))
            if rep:
                seg += f" ({rep} rpts)"
            segs.append(seg)

    verdict = _verdict(len(listed), tor, gn_class, abuse_score, ds_count)
    line = f"\x02{strip_ctrl(ip, 64)}\x02 [{verdict}] | " + " | ".join(segs)
    # Every untrusted segment above is already strip_ctrl'd; strip only
    # transport bytes here so the intentional \x02 emphasis survives.
    return _TRANSPORT_RE.sub("", line)[:_MAX_LINE]


class IpintelModule(BotModule):
    """`.ip` / `.rep` — multi-source IP reputation aggregator."""

    COMMANDS: dict[str, str] = {"ip": "cmd_ip", "rep": "cmd_ip"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._abuse_key: str = cred(self.bot.cfg, "abuseipdb_key",
                                    "ipintel", "abuseipdb_key", "")

    def is_configured(self) -> bool:
        # Fully usable keyless; AbuseIPDB just enriches when a key is set.
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def _aggregate(self, ip: str) -> str:
        ua = self._ua
        ipv4 = ":" not in ip
        zones = list(_DNSBL_ZONES) if ipv4 else []

        # All sources run concurrently in worker threads.  gather with
        # return_exceptions=True is the backstop: even if a helper raised an
        # unforeseen error type (e.g. a raw urllib3 read error), it degrades
        # to that source's sentinel rather than aborting the whole reply or
        # leaking the other still-running tasks.
        coros = [asyncio.to_thread(_dnsbl_one, ip, z, ua) for z, _ in zones]
        coros.append(asyncio.to_thread(_dshield_sync, ip, ua))
        coros.append(asyncio.to_thread(_greynoise_sync, ip, ua))
        coros.append(asyncio.to_thread(_tor_is_exit, ip, ua))
        has_abuse = bool(self._abuse_key)
        if has_abuse:
            coros.append(
                asyncio.to_thread(_abuseipdb_sync, ip, ua, self._abuse_key))

        res = await asyncio.gather(*coros, return_exceptions=True)

        n = len(zones)
        listed: list[str] = []
        checked = 0
        for (_zone, label), r in zip(zones, res[:n]):
            v = r if isinstance(r, int) else -1
            if v == -1:
                continue
            checked += 1
            if v == 1:
                listed.append(label)

        def _val(x: object, default: object) -> object:
            return default if isinstance(x, BaseException) else x

        ds = _val(res[n], None)
        gn = _val(res[n + 1], None)
        tor = res[n + 2]
        tor = tor if isinstance(tor, int) else -1
        ab = _val(res[n + 3], None) if has_abuse else None

        return _format(ip, {
            "ipv4": ipv4,
            "dnsbl_listed": listed,
            "dnsbl_checked": checked,
            "dshield": ds if isinstance(ds, dict) else None,
            "greynoise": gn if isinstance(gn, dict) else None,
            "tor": tor,
            "abuse": ab if isinstance(ab, dict) else None,
        })

    async def cmd_ip(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(
                reply_to, f"{nick}: {p}ip <ip|host>  e.g. {p}ip 185.220.101.1")
            return
        target = arg.strip().split()[0]
        if not _TARGET_RE.match(target):
            self.bot.privmsg(reply_to, f"{nick}: invalid IP/host")
            return
        # resolve_safe_ip is blocking (DNS) and enforces public-only.
        ip = await asyncio.to_thread(resolve_safe_ip, target)
        if ip is None:
            self.bot.privmsg(
                reply_to,
                f"{nick}: refusing non-public or unresolvable target "
                f"'{strip_ctrl(target, 64)}'")
            return
        line = await self._aggregate(ip)
        self.bot.privmsg(reply_to, line)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "ip <ip|host>",
                     "IP reputation: DNSBL/DShield/GreyNoise/Tor/AbuseIPDB"),
        ]


def setup(bot: object) -> IpintelModule:
    """Module entry point — returns an IpintelModule instance."""
    return IpintelModule(bot)  # type: ignore[arg-type]
