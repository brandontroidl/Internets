"""D&D 5e SRD lookup - wraps dnd5eapi.co.

No API key required.  Tries the spells endpoint first, falls back to
monsters.  Slugs are kebab-case lower (the API enforces this).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.dnd")

_BASE = "https://www.dnd5eapi.co/api/2014"
_MAX_BODY_BYTES = 256 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:64]


def _get(url: str, ua: str) -> dict | None:
    try:
        with requests.get(url, headers={"User-Agent": ua},
                         timeout=10, stream=True) as r:
            if r.status_code == 404:
                return None
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except (requests.RequestException, ValueError):
        return None


def _fmt_spell(d: dict) -> str:
    name = d.get("name", "?")
    level = d.get("level", 0)
    level_s = "cantrip" if level == 0 else f"L{level}"
    school = (d.get("school") or {}).get("name", "?")
    casting = d.get("casting_time", "?")
    range_s = d.get("range", "?")
    duration = d.get("duration", "?")
    desc_l = d.get("desc") or []
    desc = " ".join(desc_l)[:240]
    return _strip_ctrl(
        f"\x02{name}\x02 [{level_s} {school}] | cast: {casting} | "
        f"range: {range_s} | duration: {duration} - {desc}"
    )


def _fmt_monster(d: dict) -> str:
    name = d.get("name", "?")
    size = d.get("size", "?")
    type_s = d.get("type", "?")
    cr = d.get("challenge_rating", "?")
    hp = d.get("hit_points", "?")
    ac_field = d.get("armor_class")
    if isinstance(ac_field, list) and ac_field:
        ac = ac_field[0].get("value", "?")
    else:
        ac = ac_field if ac_field is not None else "?"
    speed_d = d.get("speed") or {}
    speeds = ", ".join(f"{k} {v}" for k, v in speed_d.items())
    return _strip_ctrl(
        f"\x02{name}\x02 [{size} {type_s}] | CR {cr} | "
        f"AC {ac} | HP {hp} | speed: {speeds or '?'}"
    )


def _fetch_sync(query: str, ua: str) -> str:
    slug = _slug(query)
    if not slug:
        return "invalid query"
    spell = _get(f"{_BASE}/spells/{slug}", ua)
    if spell:
        return _fmt_spell(spell)
    monster = _get(f"{_BASE}/monsters/{slug}", ua)
    if monster:
        return _fmt_monster(monster)
    return f"no D&D 5e SRD spell or monster matched '{_strip_ctrl(query, 32)}'"


class DndModule(BotModule):
    """`.dnd <name>` - D&D 5e SRD spell or monster lookup."""

    COMMANDS: dict[str, str] = {"dnd": "cmd_dnd"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_dnd(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}dnd <spell-or-monster>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, arg.strip(), self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "dnd <name>", "D&D 5e SRD spell or monster")]


def setup(bot: object) -> DndModule:
    return DndModule(bot)  # type: ignore[arg-type]
