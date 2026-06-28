"""Magic: the Gathering card lookup - wraps Scryfall.

No API key required.  Scryfall asks for a descriptive User-Agent and a
short delay between calls; we comply naturally via the channel rate
limiter.  Uses the ``/cards/named?fuzzy=`` endpoint for forgiving
name matching.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.mtg")

_URL = "https://api.scryfall.com/cards/named"
_MAX_BODY_BYTES = 256 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fetch_sync(name: str, ua: str) -> str:
    try:
        with requests.get(_URL, params={"fuzzy": name},
                          headers={"User-Agent": ua, "Accept": "application/json"},
                          timeout=10, stream=True) as r:
            if r.status_code == 404:
                return f"no card matched '{_strip_ctrl(name, 60)}'"
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return "Scryfall response too large"
            d = json.loads(body.decode("utf-8", errors="replace"))

            nm = d.get("name", "?")
            cost = d.get("mana_cost", "")
            type_line = d.get("type_line", "?")
            oracle = (d.get("oracle_text", "") or "").replace("\n", " | ")
            pwr = d.get("power")
            tgh = d.get("toughness")
            loy = d.get("loyalty")
            set_n = (d.get("set_name") or "?")
            rarity = (d.get("rarity") or "?").title()
            pt = ""
            if pwr is not None and tgh is not None:
                pt = f" {pwr}/{tgh}"
            elif loy is not None:
                pt = f" [{loy}]"
            text = _strip_ctrl(
                f"\x02{nm}\x02 {cost} | {type_line}{pt} | {oracle} | "
                f"{set_n} ({rarity})"
            )
            return text
    except requests.RequestException as e:
        log.warning(f"scryfall request: {e}")
        return "Scryfall unavailable"
    except Exception as e:
        log.warning(f"scryfall parse: {e!r}")
        return "Scryfall response parse error"


class MtgModule(BotModule):
    """`.mtg <card>` - Magic: the Gathering card lookup via Scryfall."""

    COMMANDS: dict[str, str] = {"mtg": "cmd_mtg"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_mtg(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}mtg <card name>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, arg.strip(), self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "mtg <card>", "Magic: the Gathering card via Scryfall")]


def setup(bot: object) -> MtgModule:
    return MtgModule(bot)  # type: ignore[arg-type]
