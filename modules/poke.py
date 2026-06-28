"""PokéAPI Pokémon lookup - wraps pokeapi.co.

No API key required.  Free, rate-limit-friendly.  We make one call to
``/pokemon/<name>`` and format the highlights for IRC.
"""

from __future__ import annotations

import asyncio
import json
import logging

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.poke")

_URL = "https://pokeapi.co/api/v2/pokemon"
# PokéAPI responses are huge (moves + sprites): Mewtwo ≈ 425 KB,
# Charizard ≈ 343 KB, Pikachu ≈ 274 KB.  1 MB leaves comfortable headroom.
_MAX_BODY_BYTES = 1024 * 1024


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fetch_sync(name: str, ua: str) -> str:
    try:
        # `with` releases the socket on every exit path (404, raise,
        # success) - a stream=True response left open leaks the FD.
        with requests.get(f"{_URL}/{name.lower()}",
                          headers={"User-Agent": ua},
                          timeout=10, stream=True) as r:
            if r.status_code == 404:
                return f"no Pokémon called '{_strip_ctrl(name, 32)}'"
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            return "PokéAPI response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))

        name_s = d.get("name", "?").title()
        idx = d.get("id", "?")
        types = "/".join(t["type"]["name"].title() for t in d.get("types", []))
        ht = d.get("height", 0) / 10.0   # decimetres → metres
        wt = d.get("weight", 0) / 10.0   # hectograms → kilograms
        stats = {s["stat"]["name"]: s["base_stat"] for s in d.get("stats", [])}
        bst = sum(stats.values())
        ability = (d.get("abilities") or [{}])[0].get("ability", {}).get("name", "?").replace("-", " ")
        return _strip_ctrl(
            f"\x02{name_s}\x02 #{idx} [{types}] | "
            f"\x02HP\x02 {stats.get('hp', '?')} "
            f"\x02Atk\x02 {stats.get('attack', '?')} "
            f"\x02Def\x02 {stats.get('defense', '?')} "
            f"\x02SpA\x02 {stats.get('special-attack', '?')} "
            f"\x02SpD\x02 {stats.get('special-defense', '?')} "
            f"\x02Spe\x02 {stats.get('speed', '?')} "
            f"(BST {bst}) | "
            f"\x02{ht}m {wt}kg\x02 | ability: {ability}"
        )
    except requests.RequestException as e:
        log.warning(f"pokeapi request: {e}")
        return "PokéAPI unavailable"
    except Exception as e:
        log.warning(f"pokeapi parse: {e!r}")
        return "PokéAPI response parse error"


class PokeModule(BotModule):
    """`.poke <name>` - Pokémon info (types, stats, height/weight)."""

    COMMANDS: dict[str, str] = {"poke": "cmd_poke", "pokemon": "cmd_poke"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_poke(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}poke <name-or-id>")
            return
        target = arg.strip().split()[0]
        if not all(c.isalnum() or c == "-" for c in target):
            self.bot.privmsg(reply_to, f"{nick}: name must be alphanumeric")
            return
        if target.isdigit():
            target = str(int(target))  # PokéAPI 404s on leading zeros (e.g. "06")
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, target, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "poke <name|id>", "Pokémon info from PokéAPI")]


def setup(bot: object) -> PokeModule:
    return PokeModule(bot)  # type: ignore[arg-type]
