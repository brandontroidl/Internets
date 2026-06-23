from __future__ import annotations

import asyncio
import json
import re
import logging
import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.translate")

# Language codes from Google Translate are always 2-letter or 2/3-letter
# with optional "-region" (zh-CN, pt-BR, etc.).  Be conservative on input:
# accept only the simple 2-letter form here — the legacy gtx endpoint
# rejects anything else anyway, and accepting "-" makes URL escaping
# trickier than it needs to be.
_LANG_RE = re.compile(r"^[a-z]{2}$")

# Bound the per-call text length.  IRC delivers ~400 bytes per line, so any
# query longer than this is almost certainly junk and is also a way to
# inflate the cost of the request to the unofficial endpoint.
_MAX_QUERY_CHARS = 500

# Cap upstream response.  A normal short translation is well under 4 KB;
# 256 KB is generous headroom and keeps a hostile MITM (or a Google A/B
# experiment that ships an HTML interstitial) from blowing up memory.
_MAX_BODY_BYTES = 256 * 1024

def _strip_ctrl(s: object, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _translate_sync(src: str | None, tgt: str, text: str) -> str:
    """Blocking HTTP call — run via asyncio.to_thread."""
    # Validate language codes one more time inside the worker so a future
    # caller that bypasses the handler-level regex can't smuggle in
    # arbitrary path/query characters via sl/tl.
    if src is not None and not _LANG_RE.match(src):
        return "translation failed"
    if not _LANG_RE.match(tgt):
        return "translation failed"
    if not text or len(text) > _MAX_QUERY_CHARS:
        return "translation failed"

    try:
        # ``requests`` will percent-encode params for us so ``text`` is safe
        # to pass through verbatim — but we still need to cap response size.
        with requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": src or "auto", "tl": tgt, "dt": "t", "q": text},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
            stream=True,
        ) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                log.warning("translate response exceeded size cap")
                return "translation failed"
            data = json.loads(body.decode("utf-8", errors="replace"))

            # The response shape is loose JSON arrays — be defensive about every
            # index and never assume any element is a string.
            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                return "translation failed"
            chunks = []
            for part in data[0]:
                if isinstance(part, list) and part and isinstance(part[0], str):
                    chunks.append(part[0])
            translated = "".join(chunks)
            detected_raw = data[2] if len(data) > 2 and isinstance(data[2], str) else (src or "auto")
            # Re-validate detected language against the same conservative regex
            # before splicing — otherwise upstream could return "xx\r\nQUIT" and
            # bypass our PRIVMSG framing.
            detected = detected_raw if _LANG_RE.match(detected_raw or "") else "??"
            translated = _strip_ctrl(translated, 400)
            return f"[t] [{detected}→{tgt}] {translated}" if translated else "empty result"
    except Exception as e:
        log.warning(f"Translate: {e}")
        return "translation failed"


class TranslateModule(BotModule):
    """Translation module using Google Translate."""
    COMMANDS: dict[str, str] = {"t": "cmd_translate", "translate": "cmd_translate"}

    async def cmd_translate(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Translate text between languages."""
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
        # Reject empty / overlong text early so we don't waste a request.
        if not text.strip():
            self.bot.privmsg(reply_to, f"{nick}: nothing to translate")
            return
        if len(text) > _MAX_QUERY_CHARS:
            self.bot.privmsg(reply_to, f"{nick}: input too long (max {_MAX_QUERY_CHARS} chars)")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(_translate_sync, src, tgt, text)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        """Return translation help text."""
        return [help_row(prefix, "t/.translate [src] <tgt> <text>", f"Translate  e.g. {prefix}t en es Hello")]


def setup(bot: object) -> TranslateModule:
    """Module entry point — returns a TranslateModule instance."""
    return TranslateModule(bot)  # type: ignore[arg-type]
