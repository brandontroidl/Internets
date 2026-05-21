from __future__ import annotations

import asyncio
import re
import logging
import requests
from .base import BotModule, fetch_json

log = logging.getLogger("internets.ud")

_IDX_RE = re.compile(r"^(.+?)\s*/(\d+)$")


def _lookup_sync(term: str, index: int, user_agent: str) -> str:
    """Blocking HTTP call — run via asyncio.to_thread."""
    try:
        data = fetch_json(
            "https://api.urbandictionary.com/v0/define",
            params={"term": term},
            ua=user_agent,
            timeout=10,
        )
        defs = data.get("list", [])
        if not defs:
            return f"No results for '{term}'"
        total = len(defs)
        idx   = max(1, min(index, total)) - 1
        defn  = defs[idx]["definition"].replace("\r", "").replace("\n", " ").strip()
        if len(defn) > 400:
            defn = defn[:397] + "..."
        return f"[{idx+1}/{total}] {defn}"
    except Exception as e:
        log.warning(f"UD lookup: {e}")
        return "lookup failed"


class UDModule(BotModule):
    """Urban Dictionary lookup module with result pagination."""
    COMMANDS: dict[str, str] = {"u": "cmd_ud", "urbandictionary": "cmd_ud"}

    def on_load(self) -> None:
        """Load user agent — secret_store overrides config."""
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_ud(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a term on Urban Dictionary."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}u <word> [/N]  e.g. {p}u yolo /2")
            return
        m    = _IDX_RE.match(arg.strip())
        term = m.group(1).strip() if m else arg.strip()
        idx  = int(m.group(2))    if m else 1
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, term, idx, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        """Return Urban Dictionary help text."""
        return [f"  {prefix}u/.urbandictionary <word> [/N]   Urban Dictionary  e.g. {prefix}u yolo /2"]


def setup(bot: object) -> UDModule:
    """Module entry point — returns a UDModule instance."""
    return UDModule(bot)  # type: ignore[arg-type]
