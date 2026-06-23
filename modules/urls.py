from __future__ import annotations

import asyncio
import logging

import requests
from .base import BotModule, fetch_json, help_row, strip_ctrl
from ._netsafe import SSRFBlocked, safe_open, url_is_safe as _url_is_safe

log = logging.getLogger("internets.urls")

# SSRF: .shorten validates the user URL before handing it to is.gd, and
# .expand follows redirects through the shared SSRF-safe fetch in
# modules/_netsafe.py (validates every hop, pins DNS to the checked IP).
# The old in-module IP-literal pinned adapter was removed: under urllib3 2.7
# it failed TLS SNI (HTTPS handshake failure), so .expand silently broke on
# https.  _netsafe pins DNS resolution instead, which keeps SNI/Host intact.
_REQUEST_TIMEOUT = 10


def _strip_ctrl(s: str, max_len: int = 10_000) -> str:
    return strip_ctrl(s, max_len)


def _shorten_sync(url: str, ua: str) -> str:
    """Shorten a URL via is.gd (free, no key required)."""
    # Validate the user URL first so we don't ask is.gd to shorten an internal
    # address either (http://10.0.0.1/admin, the metadata service, ...).
    if not _url_is_safe(url):
        return "shortening failed"
    try:
        d = fetch_json(
            "https://is.gd/create.php",
            params={"format": "json", "url": url},
            ua=ua,
            timeout=_REQUEST_TIMEOUT,
        )
        if "shorturl" in d:
            # is.gd returns its own URL; sanitize defensively anyway.
            return f"\x02Short URL\x02 {_strip_ctrl(str(d['shorturl']))}"
        return _strip_ctrl(str(d.get("errormessage", "shortening failed")))
    except Exception as e:
        log.warning(f"URL shorten: {e}")
        return "shortening failed"


def _expand_sync(url: str, ua: str) -> str:
    """Expand a shortened URL by following redirects under the SSRF guard.

    safe_open re-resolves, re-validates, and re-pins DNS at every hop, so the
    final URL cannot be reached via a redirect to an internal address.
    """
    try:
        with safe_open("HEAD", url, ua, follow_redirects=True,
                       timeout=_REQUEST_TIMEOUT) as resp:
            # resp.url is the final hostname URL after the safe redirect walk.
            final = _strip_ctrl(resp.url)
    except SSRFBlocked:
        return "expansion failed"
    except requests.RequestException as e:
        log.warning(f"URL expand: {e}")
        return "expansion failed"
    if final == url:
        return "\x02Long URL\x02 URL does not redirect"
    return f"\x02Long URL\x02 {final}"


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
            help_row(prefix, "shorten <url>", "Shorten a URL via is.gd"),
            help_row(prefix, "expand/.unshorten <url>", "Expand a shortened URL"),
        ]


def setup(bot: object) -> UrlsModule:
    """Module entry point — returns a UrlsModule instance."""
    return UrlsModule(bot)  # type: ignore[arg-type]
