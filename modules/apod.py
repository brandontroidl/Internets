"""NASA Astronomy Picture of the Day — wraps api.nasa.gov.

Uses ``DEMO_KEY`` by default (rate-limited but functional).  Override
by setting ``nasa_api_key`` in the secret store::

    python -m secret_store set nasa_api_key

A real key is free at https://api.nasa.gov/.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule

log = logging.getLogger("internets.apod")

_URL = "https://api.nasa.gov/planetary/apod"
_MAX_BODY_BYTES = 32 * 1024
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _fetch_sync(key: str, ua: str) -> str:
    try:
        r = requests.get(_URL, params={"api_key": key},
                         headers={"User-Agent": ua},
                         timeout=10, stream=True)
        if r.status_code == 429:
            return "APOD rate-limited — set nasa_api_key in secret_store"
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            return "APOD response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
        title = d.get("title", "?")
        date  = d.get("date", "?")
        url   = d.get("hdurl") or d.get("url", "")
        expl  = (d.get("explanation", "") or "").replace("\n", " ")
        if len(expl) > 220:
            expl = expl[:217] + "..."
        return _strip_ctrl(
            f"\x02APOD {date}\x02 — {title} | {expl} | {url}"
        )
    except requests.RequestException as e:
        log.warning(f"apod request: {e}")
        return "APOD unavailable"
    except Exception as e:
        log.warning(f"apod parse: {e!r}")
        return "APOD response parse error"


class ApodModule(BotModule):
    """`.apod` — NASA Astronomy Picture of the Day."""

    COMMANDS: dict[str, str] = {"apod": "cmd_apod"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        # Fall back to DEMO_KEY (NASA permits it with stricter rate limits).
        self._key: str = cred(self.bot.cfg, "nasa_api_key",
                              "apod", "api_key", "DEMO_KEY") or "DEMO_KEY"

    def is_configured(self) -> bool:
        return True

    async def cmd_apod(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._key, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}apod                    NASA Astronomy Picture of the Day"]


def setup(bot: object) -> ApodModule:
    return ApodModule(bot)  # type: ignore[arg-type]
