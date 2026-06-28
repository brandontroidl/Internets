"""xkcd lookup - wraps xkcd.com's official JSON endpoint.

No API key required.  Endpoints:
  - https://xkcd.com/info.0.json         → latest comic
  - https://xkcd.com/<num>/info.0.json   → specific comic by number
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.xkcd")

# Bandit B311 false-positive - picking which xkcd to show isn't a security
# decision, but SystemRandom keeps the scan clean without per-line nosec.
_rng = random.SystemRandom()

_LATEST = "https://xkcd.com/info.0.json"
_BYNUM  = "https://xkcd.com/{n}/info.0.json"
_MAX_BODY_BYTES = 64 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _get_json(url: str, ua: str) -> dict | None:
    try:
        with requests.get(url, headers={"User-Agent": ua},
                         timeout=8, stream=True) as r:
            if r.status_code == 404:
                return None
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except (requests.RequestException, ValueError):
        return None


def _fetch_sync(num: int | None, ua: str) -> str:
    if num is None:
        # Random: fetch latest first to discover max num, then random
        latest = _get_json(_LATEST, ua)
        if not latest:
            return "xkcd unavailable"
        max_n = int(latest.get("num", 1))
        # xkcd 404 doesn't exist as a comic - skip it.
        choice = _rng.randint(1, max_n)
        if choice == 404:
            choice = 405
        d = _get_json(_BYNUM.format(n=choice), ua)
        if not d:
            return "xkcd random fetch failed"
    elif num == 404:
        return "xkcd #404 doesn't exist (the comic skipped that number on purpose)"
    else:
        d = _get_json(_BYNUM.format(n=num), ua)
        if not d:
            return f"xkcd #{num} not found"
    title = d.get("title", "?")
    alt   = d.get("alt", "")
    n     = d.get("num", "?")
    return _strip_ctrl(
        f"\x02xkcd #{n}\x02 - {title} | alt: {alt} | https://xkcd.com/{n}/"
    )


class XkcdModule(BotModule):
    """`.xkcd [num]` - xkcd comic title + alt text + link."""

    COMMANDS: dict[str, str] = {"xkcd": "cmd_xkcd"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_xkcd(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        num: int | None = None
        if arg and arg.strip():
            token = arg.strip().split()[0]
            if not token.isdigit():
                self.bot.privmsg(reply_to, f"{nick}: xkcd <number>")
                return
            num = int(token)
            if num <= 0 or num > 100000:
                self.bot.privmsg(reply_to, f"{nick}: out-of-range comic number")
                return
        text = await asyncio.to_thread(_fetch_sync, num, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "xkcd [num]", "xkcd comic - random or specific")]


def setup(bot: object) -> XkcdModule:
    return XkcdModule(bot)  # type: ignore[arg-type]
