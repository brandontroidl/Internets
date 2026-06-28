"""International Space Station tracker - wraps open-notify.org.

Two free no-key endpoints:
  - http://api.open-notify.org/iss-now.json  → current lat/lon
  - http://api.open-notify.org/astros.json   → who is in space (incl. ISS)
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.iss")

_NOW = "http://api.open-notify.org/iss-now.json"
_PEOPLE = "http://api.open-notify.org/astros.json"
_MAX_BODY_BYTES = 16 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _get_json(url: str, ua: str) -> dict | None:
    try:
        with requests.get(url, headers={"User-Agent": ua},
                          timeout=8, stream=True) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except (requests.RequestException, ValueError):
        return None


def _fetch_sync(ua: str) -> str:
    now = _get_json(_NOW, ua)
    if not now or "iss_position" not in now:
        return "ISS tracker unavailable"
    pos = now["iss_position"]
    lat = float(pos.get("latitude", 0))
    lon = float(pos.get("longitude", 0))

    people = _get_json(_PEOPLE, ua)
    crew_iss: list[str] = []
    if people and people.get("message") == "success":
        for p in people.get("people", []):
            if p.get("craft") == "ISS":
                crew_iss.append(p.get("name", "?"))
    crew_s = ", ".join(crew_iss) if crew_iss else "crew data unavailable"
    return _strip_ctrl(
        f"\x02ISS\x02 at {lat:.2f}°{'N' if lat >= 0 else 'S'}, "
        f"{lon:.2f}°{'E' if lon >= 0 else 'W'} | "
        f"crew ({len(crew_iss)}): {crew_s}"
    )


class IssModule(BotModule):
    """`.iss` - current ISS location + crew."""

    COMMANDS: dict[str, str] = {"iss": "cmd_iss"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_iss(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "iss", "ISS location + current crew")]


def setup(bot: object) -> IssModule:
    return IssModule(bot)  # type: ignore[arg-type]
