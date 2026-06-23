"""Chuck-Norris-jokes command — wraps api.chucknorris.io.

No API key required.  JSON response shape:
    {"categories":[...], "id":"...", "value":"the joke text", ...}
"""

from __future__ import annotations

import asyncio
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.chuck")

_URL = "https://api.chucknorris.io/jokes/random"
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
                log.warning("chuck response too large")
                return "joke too long for IRC"
            import json
            d = json.loads(body.decode("utf-8", errors="replace"))
            joke = _strip_ctrl(d.get("value", ""))
            return joke or "no joke received"
    except requests.RequestException as e:
        log.warning(f"chuck request: {e}")
        return "Chuck Norris API unavailable"
    except Exception as e:
        log.warning(f"chuck parse: {e!r}")
        return "Chuck Norris response parse error"


class ChuckModule(BotModule):
    """`.chuck` — random Chuck Norris joke."""

    COMMANDS: dict[str, str] = {"chuck": "cmd_chuck"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_chuck(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        joke = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, joke)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "chuck", "Random Chuck Norris joke")]


def setup(bot: object) -> ChuckModule:
    return ChuckModule(bot)  # type: ignore[arg-type]
