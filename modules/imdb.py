from __future__ import annotations

import asyncio
import logging
from .base import BotModule, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.imdb")


def _lookup_sync(title: str, key: str, ua: str) -> str:
    """Blocking OMDb lookup — run via asyncio.to_thread."""
    try:
        d = fetch_json(
            "https://www.omdbapi.com/",
            params={"t": title, "plot": "short", "r": "json", "apikey": key},
            ua=ua,
            timeout=10,
        )
        if d.get("Response") != "True":
            return f"nothing found for '{strip_ctrl(title)}'"
        rating = strip_ctrl(d.get("imdbRating", "N/A"))
        votes = strip_ctrl(d.get("imdbVotes", "N/A"))
        return (
            f"\x02{strip_ctrl(d['Title'])}\x02 [{strip_ctrl(d.get('Year', '?'))}] "
            f"Rated {strip_ctrl(d.get('Rated', 'N/A'))} | "
            f"\x02Rating\x02 {rating}/10, {votes} votes | "
            f"\x02Genre\x02 {strip_ctrl(d.get('Genre', 'N/A'))} | "
            f"\x02Director\x02 {strip_ctrl(d.get('Director', 'N/A'))} | "
            f"\x02Actors\x02 {strip_ctrl(d.get('Actors', 'N/A'))} | "
            f"\x02Runtime\x02 {strip_ctrl(d.get('Runtime', 'N/A'))} | "
            f"\x02Plot\x02 {strip_ctrl(d.get('Plot', 'N/A'))} | "
            f"https://www.imdb.com/title/{strip_ctrl(d.get('imdbID', ''))}/"
        )
    except Exception as e:
        log.warning(f"OMDb lookup: {e}")
        return "lookup failed"


class ImdbModule(BotModule):
    """Movie/TV lookup via OMDb API (omdbapi.com)."""

    COMMANDS: dict[str, str] = {"imdb": "cmd_imdb"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._key: str = cred(self.bot.cfg, "omdb_key", "imdb", "omdb_key")
        if not self._key:
            log.warning("imdb: omdb_key not set (secret_store or [imdb]) — .imdb will not work")

    def is_configured(self) -> bool:
        return bool(self._key)

    async def cmd_imdb(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a movie or TV show on IMDB."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}imdb <title>  e.g. {p}imdb The Matrix")
            return
        if not self._key:
            self.bot.privmsg(reply_to, "OMDb API key not configured — see [imdb] in config.ini")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, arg.strip(), self._key, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "imdb <title>", f"Movie/TV lookup  e.g. {prefix}imdb The Matrix")]


def setup(bot: object) -> ImdbModule:
    """Module entry point — returns an ImdbModule instance."""
    return ImdbModule(bot)  # type: ignore[arg-type]
