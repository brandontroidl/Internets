"""TheCocktailDB cocktail-recipe lookup.

No API key required (we use the public test key ``1`` which is unlimited
for low-volume use).  Endpoint: ``search.php?s=<name>``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.cocktail")

_URL = "https://www.thecocktaildb.com/api/json/v1/1/search.php"
_MAX_BODY_BYTES = 256 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fetch_sync(name: str, ua: str) -> str:
    try:
        with requests.get(_URL, params={"s": name},
                         headers={"User-Agent": ua},
                         timeout=10, stream=True) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                return "TheCocktailDB response too large"
            d = json.loads(body.decode("utf-8", errors="replace"))
            drinks = d.get("drinks") or []
            if not drinks:
                return f"no cocktail matched '{_strip_ctrl(name, 60)}'"
            c = drinks[0]
            nm = c.get("strDrink", "?")
            glass = c.get("strGlass", "?")
            cat = c.get("strCategory", "?")
            instr = (c.get("strInstructions", "") or "").replace("\n", " ")
            if len(instr) > 200:
                instr = instr[:197] + "..."
            ingredients = []
            for i in range(1, 16):
                ing = (c.get(f"strIngredient{i}") or "").strip()
                qty = (c.get(f"strMeasure{i}") or "").strip()
                if ing:
                    ingredients.append(f"{qty} {ing}".strip())
            ing_s = ", ".join(ingredients) if ingredients else "(no ingredient list)"
            return _strip_ctrl(
                f"\x02{nm}\x02 ({cat}, {glass}) | {ing_s} | {instr}"
            )
    except requests.RequestException as e:
        log.warning(f"cocktail request: {e}")
        return "TheCocktailDB unavailable"
    except Exception as e:
        log.warning(f"cocktail parse: {e!r}")
        return "TheCocktailDB response parse error"


class CocktailModule(BotModule):
    """`.cocktail <name>` — cocktail recipe lookup via TheCocktailDB."""

    COMMANDS: dict[str, str] = {"cocktail": "cmd_cocktail", "drink": "cmd_cocktail"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_cocktail(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cocktail <name>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, arg.strip(), self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "cocktail <name>", "Cocktail recipe via TheCocktailDB")]


def setup(bot: object) -> CocktailModule:
    return CocktailModule(bot)  # type: ignore[arg-type]
