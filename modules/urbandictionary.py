"""
Urban Dictionary module.
Commands: .u, .urbandictionary
"""

import re
import requests
import logging
from .base import BotModule

log = logging.getLogger("internets.ud")


def urban_lookup(term: str, index: int, user_agent: str) -> str:
    try:
        r = requests.get(
            "https://api.urbandictionary.com/v0/define",
            params={"term": term},
            headers={"User-Agent": user_agent},
            timeout=10,
        )
        defs = r.json().get("list", [])
        if not defs:
            return f"No Urban Dictionary results for '{term}'."
        total = len(defs)
        idx   = max(1, min(index, total)) - 1
        defn  = defs[idx]["definition"].replace("\r","").replace("\n"," ").strip()
        if len(defn) > 400:
            defn = defn[:397] + "..."
        return f"[{idx+1}/{total}] {defn}"
    except Exception as e:
        log.warning(f"UD error: {e}")
        return "Urban Dictionary lookup failed."


class UDModule(BotModule):
    COMMANDS = {
        "u":               "cmd_ud",
        "urbandictionary": "cmd_ud",
    }

    def on_load(self):
        self.user_agent = self.bot.cfg["weather"]["user_agent"]
        log.info("UDModule loaded")

    def cmd_ud(self, nick, reply_to, arg):
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}u <word> [/N]  e.g. {p}u jason /4")
            return
        m = re.match(r"^(.+?)\s*/(\d+)$", arg.strip())
        term, idx = (m.group(1).strip(), int(m.group(2))) if m else (arg.strip(), 1)
        self.bot.privmsg(reply_to, urban_lookup(term, idx, self.user_agent))

    def help_lines(self, prefix):
        return [
            f"  {prefix}u   <word> [/N]               Urban Dictionary  e.g. {prefix}u jason /2",
            f"  {prefix}urbandictionary <word> [/N]   Alias for {prefix}u",
        ]


def setup(bot):
    return UDModule(bot)
