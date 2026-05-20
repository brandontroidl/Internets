from __future__ import annotations

import asyncio
import html
import logging
import re

import requests
from .base import BotModule

log = logging.getLogger("internets.fml")

# Match FML article text from the HTML page
_FML_RE = re.compile(
    r'<a[^>]*class="[^"]*article-link[^"]*"[^>]*>.*?<p[^>]*class="[^"]*article-contents[^"]*"[^>]*>(.*?)</p>',
    re.DOTALL,
)
_FML_ID_RE = re.compile(r'/article/(\d+)')
_TAG_RE = re.compile(r'<[^>]+>')


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", html.unescape(s)).strip()


def _lookup_sync(ua: str) -> str:
    """Fetch a random FML quote — blocking, run via asyncio.to_thread."""
    try:
        r = requests.get(
            "https://www.fmylife.com/random",
            headers={
                "User-Agent": ua,
                "Accept": "text/html",
            },
            timeout=15,
        )
        r.raise_for_status()
        matches = _FML_RE.findall(r.text)
        if not matches:
            return "could not parse FML page — site layout may have changed"

        text = _strip_tags(matches[0])
        if len(text) > 400:
            text = text[:397] + "..."

        # Try to extract the quote ID from the page
        id_match = _FML_ID_RE.search(r.text)
        qid = id_match.group(1) if id_match else "?"

        return f"[fml #{qid}] {text}"
    except Exception as e:
        log.warning(f"FML lookup: {e}")
        return "fmylife.com is temporarily unavailable"


class FmlModule(BotModule):
    """FMyLife (fmylife.com) random quote module."""

    COMMANDS: dict[str, str] = {"fml": "cmd_fml"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_fml(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show a random FMyLife quote."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}fml                    Random FMyLife quote"]


def setup(bot: object) -> FmlModule:
    """Module entry point — returns a FmlModule instance."""
    return FmlModule(bot)  # type: ignore[arg-type]
