"""TheMealDB recipe lookup.

No API key required (public test key ``1``).  Endpoint:
``search.php?s=<name>``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.recipe")

_URL = "https://www.themealdb.com/api/json/v1/1/search.php"
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
                return "TheMealDB response too large"
            d = json.loads(body.decode("utf-8", errors="replace"))
            meals = d.get("meals") or []
            if not meals:
                return f"no recipe matched '{_strip_ctrl(name, 60)}'"
            m = meals[0]
            nm = m.get("strMeal", "?")
            cat = m.get("strCategory", "?")
            area = m.get("strArea", "?")
            ingredients = []
            for i in range(1, 21):
                ing = (m.get(f"strIngredient{i}") or "").strip()
                qty = (m.get(f"strMeasure{i}") or "").strip()
                if ing:
                    ingredients.append(f"{qty} {ing}".strip())
            ing_s = ", ".join(ingredients[:12])
            if len(ingredients) > 12:
                ing_s += f", + {len(ingredients) - 12} more"
            link = m.get("strSource") or m.get("strYoutube") or ""
            return _strip_ctrl(
                f"\x02{nm}\x02 ({area} {cat}) | {ing_s} | {link}"
            )
    except requests.RequestException as e:
        log.warning(f"recipe request: {e}")
        return "TheMealDB unavailable"
    except Exception as e:
        log.warning(f"recipe parse: {e!r}")
        return "TheMealDB response parse error"


class RecipeModule(BotModule):
    """`.recipe <name>` — recipe lookup via TheMealDB."""

    COMMANDS: dict[str, str] = {"recipe": "cmd_recipe", "meal": "cmd_recipe"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_recipe(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}recipe <name>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, arg.strip(), self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "recipe/.meal <name>", "Recipe lookup via TheMealDB")]


def setup(bot: object) -> RecipeModule:
    return RecipeModule(bot)  # type: ignore[arg-type]
