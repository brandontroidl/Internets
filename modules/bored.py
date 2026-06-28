"""Bored API activity suggester - wraps bored-api.appbrewery.com.

No API key required.  The original boredapi.com domain went offline in
2024; appbrewery host a verbatim mirror of the same dataset.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.bored")

_URL = "https://bored-api.appbrewery.com/random"
_MAX_BODY_BYTES = 16 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fetch_sync(ua: str) -> str:
    try:
        with requests.get(_URL, headers={"User-Agent": ua},
                         timeout=8, stream=True) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return "Bored API response too large"
            d = json.loads(body.decode("utf-8", errors="replace"))
            activity = d.get("activity", "?")
            kind = d.get("type", "?")
            participants = d.get("participants", "?")
            return _strip_ctrl(
                f"\x02bored?\x02 try: {activity} | type: {kind} | participants: {participants}"
            )
    except requests.RequestException as e:
        log.warning(f"bored request: {e}")
        return "Bored API unavailable"
    except Exception as e:
        log.warning(f"bored parse: {e!r}")
        return "Bored API response parse error"


class BoredModule(BotModule):
    """`.bored` - random activity suggestion."""

    COMMANDS: dict[str, str] = {"bored": "cmd_bored"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_bored(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "bored", "Random activity suggestion")]


def setup(bot: object) -> BoredModule:
    return BoredModule(bot)  # type: ignore[arg-type]
