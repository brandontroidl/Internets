import re
import logging
import requests
from .base import BotModule

log = logging.getLogger("internets.ud")

_IDX_RE = re.compile(r"^(.+?)\s*/(\d+)$")


def _lookup(term, index, user_agent):
    try:
        r    = requests.get(
            "https://api.urbandictionary.com/v0/define",
            params={"term": term},
            headers={"User-Agent": user_agent},
            timeout=10,
        )
        defs = r.json().get("list", [])
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
    COMMANDS = {"u": "cmd_ud", "urbandictionary": "cmd_ud"}

    def on_load(self):
        self._ua = self.bot.cfg["weather"]["user_agent"]

    def cmd_ud(self, nick, reply_to, arg):
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}u <word> [/N]  e.g. {p}u yolo /2")
            return
        m    = _IDX_RE.match(arg.strip())
        term = m.group(1).strip() if m else arg.strip()
        idx  = int(m.group(2))    if m else 1
        self.bot.privmsg(reply_to, _lookup(term, idx, self._ua))

    def help_lines(self, prefix):
        return [f"  {prefix}u/.urbandictionary <word> [/N]   Urban Dictionary  e.g. {prefix}u yolo /2"]


def setup(bot):
    return UDModule(bot)
