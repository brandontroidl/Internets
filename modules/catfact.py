"""Cat-fact command — wraps catfact.ninja.

No API key required.  JSON response shape:
    {"fact": "the fact text", "length": 44}
"""

from __future__ import annotations

import asyncio
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.catfact")

_URL = "https://catfact.ninja/fact"
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
                log.warning("catfact response too large")
                return "cat fact too long for IRC"
            import json
            d = json.loads(body.decode("utf-8", errors="replace"))
            text = _strip_ctrl(d.get("fact", ""))
            return text or "no fact received"
    except requests.RequestException as e:
        log.warning(f"catfact request: {e}")
        return "cat facts API unavailable"
    except Exception as e:
        log.warning(f"catfact parse: {e!r}")
        return "cat facts parse error"


class CatfactModule(BotModule):
    """`.catfact` / `.cat` — random cat fact."""

    COMMANDS: dict[str, str] = {"catfact": "cmd_catfact", "cat": "cmd_catfact"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_catfact(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "catfact/.cat", "Random cat fact")]


def setup(bot: object) -> CatfactModule:
    return CatfactModule(bot)  # type: ignore[arg-type]
