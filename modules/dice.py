from __future__ import annotations

import re
import random
import logging
from .base import BotModule

log = logging.getLogger("internets.dice")

_DICE_RE = re.compile(r"^(?:(\d+)d)?(\d+)([+-]\d+)?$")


def _roll(expr: str) -> str:
    m = _DICE_RE.match(expr.strip().lower().replace(" ", ""))
    if not m:
        return "invalid format — use: N  XdN  XdN+M"
    count  = int(m.group(1)) if m.group(1) else 1
    sides  = int(m.group(2))
    mod    = int(m.group(3)) if m.group(3) else 0
    if not 1 <= count <= 100:   return "dice count must be 1–100"
    if not 2 <= sides <= 10000: return "sides must be 2–10000"
    rolls   = [random.randint(1, sides) for _ in range(count)]
    total   = sum(rolls) + mod
    maximum = sides * count + mod
    minimum = count + mod
    pct     = round((total - minimum) / max(maximum - minimum, 1) * 100)
    if count <= 20:
        rolls_str = str(rolls)
    else:
        shown = ", ".join(str(r) for r in rolls[:10])
        rolls_str = f"[{shown}, ... ({count} dice)]"
    return f":: Total {total}/{maximum} [{pct}%] :: Rolls {rolls_str} ::"


class DiceModule(BotModule):
    COMMANDS: dict[str, str] = {"d": "cmd_dice"}

    def cmd_dice(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}d [X]dN[+/-M]  e.g. {p}d 3d6+2")
            return
        self.bot.privmsg(reply_to, _roll(arg))

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}d [X]dN[+/-M]   Dice roller  e.g. {prefix}d 6  {prefix}d 3d6  {prefix}d 3d6+2"]


def setup(bot: object) -> DiceModule:
    return DiceModule(bot)  # type: ignore[arg-type]
