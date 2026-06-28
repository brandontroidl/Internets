from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.ipinfo")

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
# ip-api.com's path takes the IP or hostname directly in the URL.  If we
# pass it untrusted IRC input we can be pushed into path traversal
# (``8.8.8.8/../../some/endpoint``) or scheme injection via leading whitespace.
# Restrict the target to a conservative character class — letters, digits,
# dot, colon (for IPv6), and hyphen — then cap the length.  Anything else
# is rejected before it ever hits the wire.

_TARGET_RE = re.compile(r"^[A-Za-z0-9.:\-]{1,253}$")

def _strip_ctrl(s: object, max_len: int = 200) -> str:
    """Coerce to str, drop IRC control bytes, cap length."""
    return strip_ctrl(s, max_len)


# Cap the JSON payload we read from ip-api.  Their normal response is
# ~400 bytes; allow generous headroom but stop a hostile/misbehaving
# server from streaming us a multi-GB response.
_MAX_BODY_BYTES = 32 * 1024


def _lookup_sync(target: str, ua: str) -> str:
    """Blocking IP geolocation lookup — run via asyncio.to_thread.

    Uses ip-api.com over HTTPS (their free tier supports it on the
    ``pro.ip-api.com`` host with a key, but the plain ip-api.com endpoint
    is HTTP-only on the free tier; we still URL-escape the target and
    cap the response so an upstream MITM can't drive us into surprising
    behaviour).
    """
    # Validate before any URL formatting — never trust the IRC-supplied
    # target enough to interpolate it raw.
    if not _TARGET_RE.match(target):
        return "invalid target"

    try:
        # quote() the target defensively even after the regex check —
        # belt-and-braces against future regex relaxation.
        with requests.get(
            f"http://ip-api.com/json/{quote(target, safe='')}",
            params={"fields": "status,message,query,country,countryCode,"
                    "regionName,city,zip,lat,lon,timezone,isp,org"},
            headers={"User-Agent": ua},
            timeout=10,
            stream=True,
        ) as r:
            r.raise_for_status()
            # Bounded read: never accept more than _MAX_BODY_BYTES from upstream.
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                log.warning("ip-api response exceeded size cap")
                return "lookup failed"
            import json
            d = json.loads(body.decode("utf-8", errors="replace"))
            if d.get("status") == "fail":
                return f"{_strip_ctrl(d.get('message', 'lookup failed'))} for '{_strip_ctrl(target, 64)}'"

            # All fields below are upstream-controlled and reach an IRC line,
            # so every one is funnelled through _strip_ctrl to drop CR/LF and
            # IRC formatting bytes an attacker could embed via DNS/PTR poisoning.
            ip_addr = _strip_ctrl(d.get("query", target), 64)
            city    = _strip_ctrl(d.get("city", "N/A"), 64)
            region  = _strip_ctrl(d.get("regionName", "N/A"), 64)
            country = _strip_ctrl(d.get("country", "N/A"), 64)
            cc      = _strip_ctrl(d.get("countryCode", ""), 8)
            tz      = _strip_ctrl(d.get("timezone", "N/A"), 64)
            isp     = _strip_ctrl(d.get("isp", ""), 96)
            lat     = d.get("lat")
            lon     = d.get("lon")

            parts = [
                f"\x02IP/Host\x02 {_strip_ctrl(target, 64)} ({ip_addr})",
                f"\x02Location\x02 {city}, {region}, {country} [{cc}]",
                f"\x02Timezone\x02 {tz}",
            ]
            if isp:
                parts.append(f"\x02ISP\x02 {isp}")
            # Only emit the coords link if both are numeric — never interpolate
            # raw strings into a URL we hand back to clients.
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                parts.append(f"https://maps.google.com/maps?q={lat},{lon}")

            return " | ".join(parts)
    except Exception as e:
        log.warning(f"IP lookup: {e}")
        return "lookup failed"


class IpinfoModule(BotModule):
    """IP/hostname geolocation lookup module (ip-api.com)."""

    COMMANDS: dict[str, str] = {"ipinfo": "cmd_ipinfo"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_ipinfo(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up geolocation info for an IP address or hostname."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}ipinfo <ip/host>  e.g. {p}ipinfo 8.8.8.8")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        target = arg.strip().split()[0]
        # Pre-validate in the handler too so we can give a helpful error
        # without burning a request.
        if not _TARGET_RE.match(target):
            self.bot.privmsg(reply_to, f"{nick}: invalid IP/host")
            return
        result = await asyncio.to_thread(_lookup_sync, target, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "ipinfo <ip/host>", f"IP geolocation  e.g. {prefix}ipinfo 8.8.8.8")]


def setup(bot: object) -> IpinfoModule:
    """Module entry point — returns an IpinfoModule instance."""
    return IpinfoModule(bot)  # type: ignore[arg-type]
