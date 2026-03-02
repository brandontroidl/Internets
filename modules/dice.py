"""
Dice roller module — roll XdN+M style dice.
Commands: .d
"""

import re
import random
import logging
from .base import BotModule

log = logging.getLogger("internets.dice")


def roll_dice(expr: str) -> str:
    expr = expr.strip().lower().replace(" ", "")
    m = re.match(r"^(?:(\d+)d)?(\d+)([+-]\d+)?$", expr)
    if not m:
        return "Invalid dice format. Use: N  or  XdN  or  XdN+M"
    count_s, sides_s, mod_s = m.groups()
    count = int(count_s) if count_s else 1
    sides = int(sides_s)
    mod   = int(mod_s) if mod_s else 0
    if count < 1 or count > 100:   return "Dice count must be 1-100."
    if sides < 2 or sides > 10000: return "Sides must be 2-10000."
    rolls   = [random.randint(1, sides) for _ in range(count)]
    total   = sum(rolls) + mod
    maximum = sides * count + mod
    minimum = count + mod
    pct     = round((total - minimum) / max(maximum - minimum, 1) * 100)
    return f":: Total {total} / {maximum} [{pct}%] :: Results {rolls} ::"


class DiceModule(BotModule):
    COMMANDS = {"d": "cmd_dice"}

    def on_load(self):
        log.info("DiceModule loaded")

    def cmd_dice(self, nick, reply_to, arg):
        if self.bot.flood_limited(nick): return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}d [X]dN[+/-M]  e.g. {p}d 3d6+2")
            return
        self.bot.privmsg(reply_to, roll_dice(arg))

    def help_lines(self, prefix):
        return [
            f"  {prefix}d [X]dN[+/-M]   Dice roller  e.g. {prefix}d 6  {prefix}d 3d6  {prefix}d 3d6+2",
        ]


def setup(bot):
    return DiceModule(bot)
