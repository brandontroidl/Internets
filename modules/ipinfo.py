from __future__ import annotations

import asyncio
import logging
import requests
from .base import BotModule

log = logging.getLogger("internets.ipinfo")


def _lookup_sync(target: str, ua: str) -> str:
    """Blocking IP geolocation lookup — run via asyncio.to_thread.

    Uses ip-api.com (free, no key, 45 requests/min).
    """
    try:
        r = requests.get(
            f"http://ip-api.com/json/{target}",
            params={"fields": "status,message,query,country,countryCode,"
                    "regionName,city,zip,lat,lon,timezone,isp,org"},
            headers={"User-Agent": ua},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("status") == "fail":
            return f"{d.get('message', 'lookup failed')} for '{target}'"

        ip_addr = d.get("query", target)
        city = d.get("city", "N/A")
        region = d.get("regionName", "N/A")
        country = d.get("country", "N/A")
        cc = d.get("countryCode", "")
        tz = d.get("timezone", "N/A")
        isp = d.get("isp", "")
        lat = d.get("lat")
        lon = d.get("lon")

        parts = [
            f"\x02IP/Host\x02 {target} ({ip_addr})",
            f"\x02Location\x02 {city}, {region}, {country} [{cc}]",
            f"\x02Timezone\x02 {tz}",
        ]
        if isp:
            parts.append(f"\x02ISP\x02 {isp}")
        if lat and lon:
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
        result = await asyncio.to_thread(_lookup_sync, arg.strip().split()[0], self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}ipinfo <ip/host>       IP geolocation  e.g. {prefix}ipinfo 8.8.8.8"]


def setup(bot: object) -> IpinfoModule:
    """Module entry point — returns an IpinfoModule instance."""
    return IpinfoModule(bot)  # type: ignore[arg-type]
