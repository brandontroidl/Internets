"""Numbers API trivia/math/date facts — wraps numbersapi.com.

No API key required.  Endpoint:
  http://numbersapi.com/<n>/<type>?json

Where type ∈ {trivia, math, date, year}.  ``random`` is also accepted
in place of a number for any type.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule

log = logging.getLogger("internets.numberfact")

_URL = "http://numbersapi.com/{q}/{t}"
_TYPES = {"trivia", "math", "date", "year"}
_MAX_BODY_BYTES = 16 * 1024
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _fetch_sync(q: str, t: str, ua: str) -> str:
    try:
        r = requests.get(_URL.format(q=q, t=t), params={"json": ""},
                         headers={"User-Agent": ua},
                         timeout=8, stream=True)
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            return "Numbers API response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
        return _strip_ctrl(d.get("text", "no fact available"))
    except requests.RequestException as e:
        log.warning(f"numberfact request: {e}")
        return "Numbers API unavailable"
    except Exception as e:
        log.warning(f"numberfact parse: {e!r}")
        return "Numbers API response parse error"


class NumberfactModule(BotModule):
    """`.numberfact <n> [type]` — trivia/math/date/year fact about a number."""

    COMMANDS: dict[str, str] = {"numberfact": "cmd_numberfact", "nf": "cmd_numberfact"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_numberfact(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        q = "random"
        t = "trivia"
        if arg and arg.strip():
            parts = arg.strip().split()
            cand = parts[0].lower()
            if cand == "random" or cand.lstrip("-").isdigit() or "/" in cand:
                q = cand
            else:
                self.bot.privmsg(reply_to, f"{nick}: numberfact <n|random|MM/DD> [trivia|math|date|year]")
                return
            if len(parts) > 1:
                t = parts[1].lower()
                if t not in _TYPES:
                    self.bot.privmsg(reply_to, f"{nick}: type must be {'|'.join(sorted(_TYPES))}")
                    return
        text = await asyncio.to_thread(_fetch_sync, q, t, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}numberfact [n] [type]   Number trivia (type: trivia/math/date/year)"]


def setup(bot: object) -> NumberfactModule:
    return NumberfactModule(bot)  # type: ignore[arg-type]
