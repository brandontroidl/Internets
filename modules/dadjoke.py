"""Dad-joke command — wraps icanhazdadjoke.com.

No API key required.  The endpoint returns JSON when the request carries
an ``Accept: application/json`` header; otherwise it serves HTML.  We
always set the JSON Accept header.

Response shape:  ``{"id": "...", "joke": "...", "status": 200}``
"""

from __future__ import annotations

import asyncio
import logging

import requests
from .base import BotModule

log = logging.getLogger("internets.dadjoke")

_URL = "https://icanhazdadjoke.com/"
_MAX_BODY_BYTES = 16 * 1024
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _fetch_sync(ua: str) -> str:
    try:
        r = requests.get(
            _URL,
            headers={"Accept": "application/json", "User-Agent": ua},
            timeout=8, stream=True,
        )
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("dadjoke response too large (%d bytes)", len(body))
            return "dad joke too long for IRC"
        import json
        d = json.loads(body.decode("utf-8", errors="replace"))
        joke = _strip_ctrl(d.get("joke", ""))
        return joke or "no joke received"
    except requests.RequestException as e:
        log.warning(f"dadjoke request: {e}")
        return "dad joke unavailable"
    except Exception as e:
        log.warning(f"dadjoke parse: {e!r}")
        return "dad joke parse error"


class DadjokeModule(BotModule):
    """`.dadjoke` / `.joke` — random dad joke from icanhazdadjoke.com."""

    COMMANDS: dict[str, str] = {"dadjoke": "cmd_dadjoke", "joke": "cmd_dadjoke"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_dadjoke(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        joke = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, joke)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}dadjoke / .joke         Random dad joke (icanhazdadjoke.com)"]


def setup(bot: object) -> DadjokeModule:
    return DadjokeModule(bot)  # type: ignore[arg-type]
