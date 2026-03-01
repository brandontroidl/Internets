"""
Translation module — Google Translate gtx endpoint (no key needed).
Commands: .t, .translate
"""

import re
import requests
import logging
from .base import BotModule

log = logging.getLogger("internets.translate")


def translate_text(src_lang, tgt_lang: str, text: str) -> str:
    sl = src_lang if src_lang else "auto"
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client":"gtx","sl":sl,"tl":tgt_lang,"dt":"t","q":text},
            headers={"User-Agent":"Mozilla/5.0"},
            timeout=10,
        )
        data       = r.json()
        translated = "".join(part[0] for part in data[0] if part[0])
        detected   = data[2] if len(data) > 2 and data[2] else sl
        if not translated:
            return "Translation returned empty result."
        return f"[t] [from {detected}] -> {translated}"
    except Exception as e:
        log.warning(f"Translate error: {e}")
        return "Translation failed."


class TranslateModule(BotModule):
    COMMANDS = {
        "t":         "cmd_translate",
        "translate": "cmd_translate",
    }

    def on_load(self):
        log.info("TranslateModule loaded")

    def cmd_translate(self, nick, reply_to, arg):
        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg:
            self.bot.privmsg(reply_to,
                f"{nick}: usage: {p}t [src] <tgt> <text>  e.g. {p}t en es Hello")
            return
        parts   = arg.strip().split(None, 2)
        lang_re = re.compile(r"^[a-z]{2}$")
        if len(parts) >= 3 and lang_re.match(parts[0]) and lang_re.match(parts[1]):
            src, tgt, text = parts[0], parts[1], parts[2]
        elif len(parts) >= 2 and lang_re.match(parts[0]):
            src, tgt, text = None, parts[0], " ".join(parts[1:])
        else:
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}t [src] <tgt> <text>")
            return
        self.bot.privmsg(reply_to, translate_text(src, tgt, text))

    def help_lines(self, prefix):
        return [
            f"  {prefix}t   [src] <tgt> <text>        Translate  e.g. {prefix}t en es Hello",
            f"  {prefix}translate [src] <tgt> <text>  Alias for {prefix}t",
        ]


def setup(bot):
    return TranslateModule(bot)
