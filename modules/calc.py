import re
import ast
import math
import operator
import logging
from .base import BotModule

log = logging.getLogger("internets.calc")

# Safe functions and constants exposed to the calculator.
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "cbrt": math.cbrt,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
    "degrees": math.degrees, "radians": math.radians,
    "gcd": math.gcd,
    "hypot": math.hypot, "pow": math.pow,
}
# factorial is handled separately with an input cap — see _safe_factorial.
_FUNCS["factorial"] = None  # placeholder, replaced after _safe_factorial is defined

_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_IMPLICIT_MUL = [
    (re.compile(r"(\d)([a-zA-Z])"), r"\1*\2"),
    (re.compile(r"([a-zA-Z])(\d)"), r"\1*\2"),
]

# Function/constant names containing digits that implicit multiplication
# would mangle (log2 -> log*2).  Protected before regex runs.
_DIGIT_NAMES = sorted(
    [n for n in list(_FUNCS) + list(_CONSTS) if any(c.isdigit() for c in n)],
    key=len, reverse=True,
)


_MAX_DEPTH = 50


def _safe_factorial(n):
    if not isinstance(n, (int, float)) or n < 0 or n != int(n):
        raise ValueError("factorial requires a non-negative integer")
    if n > 170:
        raise ValueError("factorial input too large (max 170)")
    return math.factorial(int(n))

_FUNCS["factorial"] = _safe_factorial


def _safe_eval(node, depth=0):
    if depth > _MAX_DEPTH:
        raise ValueError("expression too deeply nested")
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        left, right = _safe_eval(node.left, depth + 1), _safe_eval(node.right, depth + 1)
        # Guard against exponent bombs (e.g. 9**9**9**9)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, (int, float)) and abs(right) > 10000:
                raise ValueError("exponent too large")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return op(_safe_eval(node.operand, depth + 1))
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            args = [_safe_eval(a, depth + 1) for a in node.args]
            if node.keywords:
                raise ValueError("keyword arguments not supported")
            return _FUNCS[node.func.id](*args)
        raise ValueError(f"unknown function: {ast.dump(node.func)}")
    raise ValueError(f"unsupported expression: {type(node).__name__}")


def _calc(expr: str) -> str:
    expr = expr.strip()
    # Temporarily hide function names that contain digits (log2, log10, atan2)
    # so the implicit multiplication regex doesn't split them.
    held = {}
    for i, name in enumerate(_DIGIT_NAMES):
        tag = f"\x01{i}\x01"
        expr = expr.replace(name, tag)
        held[tag] = name
    for pattern, sub in _IMPLICIT_MUL:
        expr = pattern.sub(sub, expr)
    for tag, name in held.items():
        expr = expr.replace(tag, name)
    try:
        tree   = ast.parse(expr, mode="eval")
        result = _safe_eval(tree)
        if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
            return str(int(result))
        return f"{result:.8g}" if isinstance(result, float) else str(result)
    except ZeroDivisionError:
        return "division by zero"
    except (ValueError, TypeError, OverflowError, SyntaxError) as e:
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
