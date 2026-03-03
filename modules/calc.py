import re
import math
import logging
from .base import BotModule

log = logging.getLogger("internets.calc")

_GLOBALS = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_GLOBALS.update({"pi": math.pi, "e": math.e, "abs": abs, "round": round})

_IMPLICIT_MUL = [
    (re.compile(r"(\d)(\s*)([a-zA-Z])"), r"\1*\3"),
    (re.compile(r"([a-zA-Z])(\s*)(\d)"), r"\1*\3"),
]


def _calc(expr: str) -> str:
    for pattern, sub in _IMPLICIT_MUL:
        expr = pattern.sub(sub, expr.strip())
    try:
        result = eval(expr, {"__builtins__": {}}, _GLOBALS)
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        return f"{result:.8g}" if isinstance(result, float) else str(result)
    except ZeroDivisionError:
        return "division by zero"
    except Exception as e:
        return f"error: {e}"


class CalcModule(BotModule):
    COMMANDS = {"cc": "cmd_calc"}

    def cmd_calc(self, nick, reply_to, arg):
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cc <expression>  e.g. {p}cc 2pi")
            return
        self.bot.privmsg(reply_to, f"[calc] {arg} = {_calc(arg)}")

    def help_lines(self, prefix):
        return [f"  {prefix}cc <expression>   Calculator  e.g. {prefix}cc 2pi  {prefix}cc sqrt(144)"]


def setup(bot):
    return CalcModule(bot)
