"""
Calculator module — evaluate math expressions safely.
Commands: .cc
"""

import re
import math
import logging
from .base import BotModule

log = logging.getLogger("internets.calc")

_CALC_GLOBALS = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_CALC_GLOBALS.update({"pi": math.pi, "e": math.e, "abs": abs, "round": round})


def safe_calc(expr: str) -> str:
    """Evaluate a math expression with no builtins — safe from code execution."""
    expr = expr.strip()
    # Implicit multiplication: 2pi → 2*pi, 3e → 3*e
    expr = re.sub(r"(\d)(\s*)([a-zA-Z])", r"\1*\3", expr)
    expr = re.sub(r"([a-zA-Z])(\s*)(\d)", r"\1*\3", expr)
    try:
        result = eval(expr, {"__builtins__": {}}, _CALC_GLOBALS)
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        if isinstance(result, float):
            return f"{result:.8g}"
        return str(result)
    except ZeroDivisionError:
        return "Error: division by zero"
    except Exception as e:
        return f"Error: {e}"


class CalcModule(BotModule):
    COMMANDS = {"cc": "cmd_calc"}

    def on_load(self):
        log.info("CalcModule loaded")

    def cmd_calc(self, nick, reply_to, arg):
        if self.bot.flood_limited(nick): return
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}cc <expression>  e.g. {p}cc 2pi")
            return
        self.bot.privmsg(reply_to, f"[calc] {arg} = {safe_calc(arg)}")

    def help_lines(self, prefix):
        return [
            f"  {prefix}cc <expression>   Calculator  e.g. {prefix}cc 2pi  {prefix}cc sqrt(144)",
        ]


def setup(bot):
    return CalcModule(bot)
