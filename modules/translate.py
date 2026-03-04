from __future__ import annotations

import asyncio
import re
import logging
import requests
from .base import BotModule

log = logging.getLogger("internets.translate")

_LANG_RE = re.compile(r"^[a-z]{2}$")


def _translate_sync(src: str | None, tgt: str, text: str) -> str:
    """Blocking HTTP call — run via asyncio.to_thread."""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": src or "auto", "tl": tgt, "dt": "t", "q": text},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        data       = r.json()
        translated = "".join(part[0] for part in data[0] if part[0])
        detected   = data[2] if len(data) > 2 and data[2] else (src or "auto")
        return f"[t] [{detected}→{tgt}] {translated}" if translated else "empty result"
    except Exception as e:
        log.warning(f"Translate: {e}")
        return "translation failed"


class TranslateModule(BotModule):
    COMMANDS: dict[str, str] = {"t": "cmd_translate", "translate": "cmd_translate"}

    async def cmd_translate(self, nick: str, reply_to: str, arg: str | None) -> None:
        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg:
            self.bot.privmsg(reply_to, f"{nick}: {p}t [src] <tgt> <text>  e.g. {p}t en es Hello")
            return
        parts = arg.strip().split(None, 2)
        if len(parts) >= 3 and _LANG_RE.match(parts[0]) and _LANG_RE.match(parts[1]):
            src, tgt, text = parts[0], parts[1], parts[2]
        elif len(parts) >= 2 and _LANG_RE.match(parts[0]):
            src, tgt, text = None, parts[0], " ".join(parts[1:])
        else:
            self.bot.privmsg(reply_to, f"{nick}: {p}t [src] <tgt> <text>")
            return
        result = await asyncio.to_thread(_translate_sync, src, tgt, text)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}t/.translate [src] <tgt> <text>   Translate  e.g. {prefix}t en es Hello"]


def setup(bot: object) -> TranslateModule:
    return TranslateModule(bot)  # type: ignore[arg-type]
