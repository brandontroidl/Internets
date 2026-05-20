from __future__ import annotations

import asyncio
import logging
import requests
from .base import BotModule

log = logging.getLogger("internets.urls")


def _shorten_sync(url: str, ua: str) -> str:
    """Shorten a URL via is.gd (free, no key required)."""
    try:
        r = requests.get(
            "https://is.gd/create.php",
            params={"format": "json", "url": url},
            headers={"User-Agent": ua},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if "shorturl" in d:
            return f"\x02Short URL\x02 {d['shorturl']}"
        return d.get("errormessage", "shortening failed")
    except Exception as e:
        log.warning(f"URL shorten: {e}")
        return "shortening failed"


def _expand_sync(url: str, ua: str) -> str:
    """Expand a shortened URL by following redirects."""
    try:
        r = requests.head(
            url,
            headers={"User-Agent": ua},
            allow_redirects=True,
            timeout=10,
        )
        final = r.url
        if final == url:
            return "\x02Long URL\x02 URL does not redirect"
        return f"\x02Long URL\x02 {final}"
    except Exception as e:
        log.warning(f"URL expand: {e}")
        return "expansion failed"


class UrlsModule(BotModule):
    """URL shortener (is.gd) and expander module."""

    COMMANDS: dict[str, str] = {
        "shorten": "cmd_shorten",
        "expand": "cmd_expand",
        "unshorten": "cmd_expand",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_shorten(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Shorten a URL."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}shorten <url>")
            return
        url = arg.strip().split()[0]
        if not url.startswith(("http://", "https://")):
            self.bot.privmsg(reply_to, f"{nick}: URL must start with http:// or https://")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_shorten_sync, url, self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_expand(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Expand a shortened URL."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}expand <url>")
            return
        url = arg.strip().split()[0]
        if not url.startswith(("http://", "https://")):
            self.bot.privmsg(reply_to, f"{nick}: URL must start with http:// or https://")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_expand_sync, url, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}shorten <url>          Shorten a URL via is.gd",
            f"  {prefix}expand/.unshorten <url> Expand a shortened URL",
        ]


def setup(bot: object) -> UrlsModule:
    """Module entry point — returns a UrlsModule instance."""
    return UrlsModule(bot)  # type: ignore[arg-type]
