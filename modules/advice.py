"""Advice-slip command — wraps api.adviceslip.com.

No API key required.  Response shape (the upstream serves
``Content-Type: text/html`` but the body is JSON — we parse it as JSON
explicitly rather than trusting the header):
    {"slip": {"id": 56, "advice": "the advice text"}}
"""

from __future__ import annotations

import asyncio
import logging

import requests
from .base import BotModule

log = logging.getLogger("internets.advice")

_URL = "https://api.adviceslip.com/advice"
_MAX_BODY_BYTES = 16 * 1024
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _fetch_sync(ua: str) -> str:
    try:
        r = requests.get(_URL, headers={"User-Agent": ua},
                         timeout=8, stream=True)
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("advice response too large")
            return "advice too long for IRC"
        import json
        d = json.loads(body.decode("utf-8", errors="replace"))
        advice = _strip_ctrl(d.get("slip", {}).get("advice", ""))
        return advice or "no advice received"
    except requests.RequestException as e:
        log.warning(f"advice request: {e}")
        return "advice slip API unavailable"
    except Exception as e:
        log.warning(f"advice parse: {e!r}")
        return "advice slip parse error"


class AdviceModule(BotModule):
    """`.advice` — random piece of advice (fortune-cookie style)."""

    COMMANDS: dict[str, str] = {"advice": "cmd_advice"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_advice(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}advice                 Random piece of advice"]


def setup(bot: object) -> AdviceModule:
    return AdviceModule(bot)  # type: ignore[arg-type]
