"""DNS / RDAP utilities — keyless, over HTTPS.

    .dns <host> [type]   A/AAAA/MX/TXT/NS/CNAME (default A) via Cloudflare DoH
    .rdns <ip>           reverse PTR lookup (in-addr.arpa / ip6.arpa)
    .caa <domain>        CAA records (+ SPF/DMARC if easy)
    .whois <domain>      RDAP domain lookup (registrar / events / NS / status)
    .asn <ip>            RDAP IP lookup (network / AS, best-effort)

All outbound HTTP goes through base.fetch_json (size-capped).  No API key
is required; every upstream-derived string is run through strip_ctrl.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re

from .base import BotModule, ResponseTooLarge, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.dnsutils")

# Conservative hostname / domain charset — letters, digits, dot, hyphen,
# underscore (for _dmarc / _domainkey style names).  Reject anything else
# before it ever hits the wire.
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")

# DNS record types we accept for `.dns`.  numeric codes only used internally.
_TYPES = {"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "PTR", "SRV", "CAA"}

# RCODE → human label (Cloudflare DoH returns "Status").
_RCODE = {
    0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
    4: "NOTIMP", 5: "REFUSED",
}

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_DOH_HEADERS = {"Accept": "application/dns-json"}
_MAX_LINE = 380


# ── DoH query ──────────────────────────────────────────────────────────
def _doh(name: str, rrtype: str, ua: str) -> dict | None:
    """One Cloudflare DNS-over-HTTPS query → parsed JSON dict (or None)."""
    return fetch_json(
        _DOH_URL,
        ua=ua,
        params={"name": name, "type": rrtype},
        headers=_DOH_HEADERS,
        timeout=10,
    )


def _answers(data: dict | None, want_type: str | None = None) -> list[str]:
    """Pull the "Answer" array's data fields, optionally filtered by type name."""
    if not isinstance(data, dict):
        return []
    want_code = _TYPE_CODES.get(want_type) if want_type else None
    out: list[str] = []
    for ans in data.get("Answer", []) or []:
        if not isinstance(ans, dict):
            continue
        if want_code is not None and ans.get("type") != want_code:
            continue
        d = ans.get("data")
        if d is not None:
            out.append(strip_ctrl(str(d), 200))
    return out


# DNS rrtype → numeric code, for filtering Answer entries by type.
_TYPE_CODES = {
    "A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15,
    "TXT": 16, "AAAA": 28, "SRV": 33, "CAA": 257,
}


def _join(parts: list[str], sep: str = ", ") -> str:
    """Join answer fields and cap to a single IRC line."""
    s = sep.join(parts)
    return s if len(s) <= _MAX_LINE else s[:_MAX_LINE - 3] + "..."


# ── .dns ────────────────────────────────────────────────────────────────
def _dns_sync(host: str, rrtype: str, ua: str) -> str:
    host = host.strip()
    rrtype = (rrtype or "A").strip().upper()
    if not _HOST_RE.match(host):
        return "invalid host"
    if rrtype not in _TYPES:
        return f"unknown type '{strip_ctrl(rrtype, 16)}' — try A/AAAA/MX/TXT/NS/CNAME"
    try:
        data = _doh(host, rrtype, ua)
        status = data.get("Status") if isinstance(data, dict) else None
        ans = _answers(data, rrtype)
        if not ans:
            rc = _RCODE.get(status, "")
            tail = f" ({rc})" if rc and rc != "NOERROR" else ""
            return f"no {rrtype} records for {strip_ctrl(host, 80)}{tail}"
        return f"\x02{strip_ctrl(host, 80)}\x02 {rrtype}: {_join(ans)}"
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f".dns {host} {rrtype}: {e}")
        return "lookup failed"
    except Exception as e:  # requests.RequestException etc.
        log.warning(f".dns {host} {rrtype}: {e}")
        return "lookup failed"


# ── .rdns ─────────────────────────────────────────────────────────────────
def _reverse_name(ip: str) -> str | None:
    """Build the in-addr.arpa / ip6.arpa PTR name for an IP, or None."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    return addr.reverse_pointer


def _rdns_sync(ip: str, ua: str) -> str:
    rev = _reverse_name(ip)
    if rev is None:
        return "invalid IP"
    try:
        data = _doh(rev, "PTR", ua)
        ans = _answers(data, "PTR")
        if not ans:
            return f"no PTR record for {strip_ctrl(ip.strip(), 80)}"
        return f"\x02{strip_ctrl(ip.strip(), 80)}\x02 PTR: {_join(ans)}"
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f".rdns {ip}: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f".rdns {ip}: {e}")
        return "lookup failed"


# ── .caa ──────────────────────────────────────────────────────────────────
def _spf_dmarc(host: str, ua: str) -> list[str]:
    """Best-effort SPF (TXT @apex) + DMARC (_dmarc TXT) one-liners."""
    out: list[str] = []
    try:
        txt = _answers(_doh(host, "TXT", ua), "TXT")
        spf = next((t for t in txt if "v=spf1" in t.lower()), None)
        if spf:
            out.append(f"SPF: {spf}")
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        log.warning(f".caa spf {host}: {e}")
    try:
        dm = _answers(_doh(f"_dmarc.{host}", "TXT", ua), "TXT")
        rec = next((t for t in dm if "v=dmarc1" in t.lower()), None)
        if rec:
            out.append(f"DMARC: {rec}")
    except Exception as e:  # noqa: BLE001
        log.warning(f".caa dmarc {host}: {e}")
    return out


def _caa_sync(domain: str, ua: str) -> str:
    domain = domain.strip()
    if not _HOST_RE.match(domain):
        return "invalid domain"
    try:
        caa = _answers(_doh(domain, "CAA", ua), "CAA")
        parts: list[str] = []
        if caa:
            parts.append("CAA: " + "; ".join(caa))
        else:
            parts.append("CAA: none (any CA may issue)")
        parts.extend(_spf_dmarc(domain, ua))
        return f"\x02{strip_ctrl(domain, 80)}\x02 " + _join(parts, " | ")
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f".caa {domain}: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f".caa {domain}: {e}")
        return "lookup failed"


# ── .whois (RDAP domain) ──────────────────────────────────────────────────
def _rdap_registrar(entities: list) -> str:
    """Extract registrar name from an RDAP entities array."""
    if not isinstance(entities, list):
        return ""
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles") or []
        if "registrar" not in roles:
            continue
        # vcardArray: ["vcard", [ ["fn", {}, "text", "Name"], ... ]]
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list):
            for field in vcard[1]:
                if (isinstance(field, list) and len(field) >= 4
                        and field[0] == "fn"):
                    return str(field[3])
        h = ent.get("handle")
        if h:
            return str(h)
    return ""


def _rdap_event(events: list, action: str) -> str:
    """Return the eventDate for a given eventAction (registration/expiration)."""
    if not isinstance(events, list):
        return ""
    for ev in events:
        if isinstance(ev, dict) and ev.get("eventAction") == action:
            return str(ev.get("eventDate", ""))[:10]
    return ""


def _rdap_nameservers(ns: list) -> list[str]:
    out: list[str] = []
    if isinstance(ns, list):
        for n in ns:
            if isinstance(n, dict) and n.get("ldhName"):
                out.append(str(n["ldhName"]).lower())
    return out


def _whois_sync(domain: str, ua: str) -> str:
    domain = domain.strip().lower()
    if not _HOST_RE.match(domain):
        return "invalid domain"
    try:
        # RDAP payloads can be large (full nameserver/entity graphs).
        data = fetch_json(
            f"https://rdap.org/domain/{domain}",
            ua=ua,
            timeout=12,
            allow_404=True,
            max_bytes=512 * 1024,
        )
        if data is None or not isinstance(data, dict):
            return f"no RDAP record for {strip_ctrl(domain, 80)}"
        registrar = strip_ctrl(_rdap_registrar(data.get("entities", [])), 80)
        reg = _rdap_event(data.get("events", []), "registration")
        exp = _rdap_event(data.get("events", []), "expiration")
        ns = _rdap_nameservers(data.get("nameservers", []))
        status = data.get("status") or []
        status_s = ", ".join(strip_ctrl(str(s), 40) for s in status[:3]) \
            if isinstance(status, list) else ""

        parts = [f"\x02{strip_ctrl(domain, 80)}\x02"]
        if registrar:
            parts.append(f"registrar {registrar}")
        if reg:
            parts.append(f"created {strip_ctrl(reg, 10)}")
        if exp:
            parts.append(f"expires {strip_ctrl(exp, 10)}")
        if ns:
            parts.append("ns " + " ".join(strip_ctrl(n, 80) for n in ns[:4]))
        if status_s:
            parts.append(f"status {status_s}")
        if len(parts) == 1:
            return f"RDAP record for {strip_ctrl(domain, 80)} (no detail fields)"
        return _join(parts, " | ")
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f".whois {domain}: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f".whois {domain}: {e}")
        return "lookup failed"


# ── .asn (RDAP IP) ────────────────────────────────────────────────────────
def _asn_sync(target: str, ua: str) -> str:
    target = target.strip()
    # Accept a bare IP; for ASn input, RDAP autnum needs the number only.
    m = re.match(r"^(?:as)?(\d{1,10})$", target, re.IGNORECASE)
    if m:
        path = f"autnum/{m.group(1)}"
        label = f"AS{m.group(1)}"
    else:
        try:
            ipaddress.ip_address(target)
        except ValueError:
            return "give an IP address or ASn (e.g. .asn 8.8.8.8 / .asn AS15169)"
        path = f"ip/{target}"
        label = target
    try:
        data = fetch_json(
            f"https://rdap.org/{path}",
            ua=ua,
            timeout=12,
            allow_404=True,
            max_bytes=512 * 1024,
        )
        if data is None or not isinstance(data, dict):
            return f"no RDAP record for {strip_ctrl(label, 80)}"
        name = strip_ctrl(data.get("name", ""), 80)
        handle = strip_ctrl(data.get("handle", ""), 40)
        country = strip_ctrl(data.get("country", ""), 8)
        start = strip_ctrl(data.get("startAddress", ""), 64)
        end = strip_ctrl(data.get("endAddress", ""), 64)
        rdap_type = strip_ctrl(data.get("type", ""), 40)

        parts = [f"\x02{strip_ctrl(label, 80)}\x02"]
        if name:
            parts.append(name)
        if handle and handle != name:
            parts.append(f"({handle})")
        if start and end:
            parts.append(f"{start}–{end}")
        if rdap_type:
            parts.append(rdap_type)
        if country:
            parts.append(country)
        if len(parts) == 1:
            return f"RDAP record for {strip_ctrl(label, 80)} (no detail fields)"
        return _join(parts, " | ")
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f".asn {label}: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f".asn {label}: {e}")
        return "lookup failed"


class DnsutilsModule(BotModule):
    """`.dns` / `.rdns` / `.caa` / `.whois` / `.asn` — DNS & RDAP lookups."""

    COMMANDS: dict[str, str] = {
        "dns": "cmd_dns",
        "rdns": "cmd_rdns",
        "caa": "cmd_caa",
        "whois": "cmd_whois",
        "asn": "cmd_asn",
    }

    def is_configured(self) -> bool:
        return True

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_dns(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        parts = (arg or "").split()
        if not parts:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}dns <host> [type]  e.g. {p}dns example.com MX")
            return
        host = parts[0]
        rrtype = parts[1] if len(parts) > 1 else "A"
        result = await asyncio.to_thread(_dns_sync, host, rrtype, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_rdns(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rdns <ip>  e.g. {p}rdns 8.8.8.8")
            return
        result = await asyncio.to_thread(_rdns_sync, arg.split()[0], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_caa(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}caa <domain>  e.g. {p}caa example.com")
            return
        result = await asyncio.to_thread(_caa_sync, arg.split()[0], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_whois(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}whois <domain>  e.g. {p}whois example.com")
            return
        result = await asyncio.to_thread(_whois_sync, arg.split()[0], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_asn(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}asn <ip|ASn>  e.g. {p}asn 8.8.8.8")
            return
        result = await asyncio.to_thread(_asn_sync, arg.split()[0], self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "dns <host> [type]", "DNS lookup (A/AAAA/MX/TXT/NS/CNAME)"),
            help_row(prefix, "rdns <ip>", "Reverse PTR lookup"),
            help_row(prefix, "caa <domain>", "CAA records (+ SPF/DMARC)"),
            help_row(prefix, "whois <domain>", "RDAP domain registration info"),
            help_row(prefix, "asn <ip|ASn>", "RDAP network / AS info"),
        ]


def setup(bot: object) -> DnsutilsModule:
    """Module entry point — returns a DnsutilsModule instance."""
    return DnsutilsModule(bot)  # type: ignore[arg-type]
