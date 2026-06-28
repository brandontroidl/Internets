from __future__ import annotations

import asyncio
import re
import logging
from .base import BotModule, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.dictionary")

_IDX_RE = re.compile(r"^(.+?)\s*/(\d+)$")


def _lookup_sync(word: str, index: int, ua: str) -> str:
    """Blocking dictionary lookup - run via asyncio.to_thread.

    Uses the Free Dictionary API (dictionaryapi.dev) - no key required.
    """
    try:
        entries = fetch_json(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}",
            ua=ua,
            timeout=10,
            allow_404=True,
        )
        if entries is None or not entries or not isinstance(entries, list):
            return f"no definition found for '{word}'"

        # Flatten all definitions across all meanings
        defs: list[tuple[str, str]] = []  # (part_of_speech, definition)
        for entry in entries:
            for meaning in entry.get("meanings", []):
                pos = meaning.get("partOfSpeech", "")
                for d in meaning.get("definitions", []):
                    text = d.get("definition", "")
                    if text:
                        defs.append((pos, text))

        if not defs:
            return f"no definition found for '{word}'"

        total = len(defs)
        idx = max(1, min(index, total)) - 1
        pos, defn = defs[idx]

        if len(defn) > 400:
            defn = defn[:397] + "..."

        pos_str = f" ({strip_ctrl(pos)})" if pos else ""
        return f"[{idx + 1}/{total}] \x02{strip_ctrl(word)}\x02{pos_str} - {strip_ctrl(defn)}"
    except Exception as e:
        log.warning(f"Dictionary lookup: {e}")
        return "lookup failed"


class DictionaryModule(BotModule):
    """English dictionary definition module (Free Dictionary API)."""

    COMMANDS: dict[str, str] = {"dict": "cmd_dict", "dictionary": "cmd_dict"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    async def cmd_dict(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up an English word definition."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(
                reply_to,
                f"{nick}: {p}dict <word> [/N]  e.g. {p}dict ephemeral /2",
            )
            return
        m = _IDX_RE.match(arg.strip())
        word = m.group(1).strip() if m else arg.strip()
        idx = int(m.group(2)) if m else 1
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        result = await asyncio.to_thread(_lookup_sync, word, idx, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "dict/.dictionary <word> [/N]", f"Dictionary definition  e.g. {prefix}dict ephemeral /2")
        ]


def setup(bot: object) -> DictionaryModule:
    """Module entry point - returns a DictionaryModule instance."""
    return DictionaryModule(bot)  # type: ignore[arg-type]
