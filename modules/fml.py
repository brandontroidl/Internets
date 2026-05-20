from __future__ import annotations

import asyncio
import html
import logging
import re

import requests
from .base import BotModule

log = logging.getLogger("internets.fml")

# Match an FML article's BODY anchor on the random page.  The site
# moved to Tailwind in 2024-2025 and now renders each article with TWO
# links to the same /article/<slug>_<id>.html URL:
#
#   1. A bare category-title anchor like
#      ``<a href="/article/..._<id>.html">Magic underwear</a>``
#      (no class attribute, short curated tag-line)
#   2. The body anchor:
#      ``<a href=".._<id>.html" class="block text-blue-500 dark:text-white my-4 [spicy-hidden]">
#         Today, I ... FML
#       </a>``
#
# Anchoring the regex on the ``block text-blue-500`` class signature
# (always present on the body link, absent from the title link) ensures
# we capture the full quote, not the category tag-line.
_FML_ARTICLE = re.compile(
    r'<a\s+href="/article/[^"]*?_(\d+)\.html"\s+'
    r'class="[^"]*block text-blue-500[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE   = re.compile(r'<[^>]+>')
_WS_RE    = re.compile(r'\s+')


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", html.unescape(s))).strip()


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
        matches = _FML_ARTICLE.findall(r.text)
        if not matches:
            return "could not parse FML page — site layout may have changed"

        qid, raw_text = matches[0]
        text = _strip_tags(raw_text)
        if len(text) > 400:
            text = text[:397] + "..."

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
