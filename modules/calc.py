from __future__ import annotations

import re
import ast
import math
import operator
import logging
from typing import Any
from .base import BotModule

log = logging.getLogger("internets.calc")

_FUNCS: dict[str, Any] = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "cbrt": getattr(math, "cbrt", lambda x: x ** (1/3) if x >= 0 else -((-x) ** (1/3))),
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
    "degrees": math.degrees, "radians": math.radians,
    "gcd": math.gcd,
    "hypot": math.hypot, "pow": math.pow,
}

_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}

_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type, Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_IMPLICIT_MUL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\d)([a-zA-Z])"), r"\1*\2"),
    (re.compile(r"([a-zA-Z])(\d)"), r"\1*\2"),
]

_DIGIT_NAMES: list[str] = sorted(
    [n for n in list(_FUNCS) + list(_CONSTS) if any(c.isdigit() for c in n)],
    key=len, reverse=True,
)

_MAX_DEPTH = 50


def _safe_factorial(n: int | float) -> int:
    if not isinstance(n, (int, float)) or n < 0 or n != int(n):
        raise ValueError("factorial requires a non-negative integer")
    if n > 170:
        raise ValueError("factorial input too large (max 170)")
    return math.factorial(int(n))

_FUNCS["factorial"] = _safe_factorial


def _safe_eval(node: ast.AST, depth: int = 0) -> int | float:
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
    # Strip CTCP markers (\x01) — they can appear in IRC and collide with
    # the implicit-multiplication placeholder logic.
    expr = expr.replace("\x01", "")
    held: dict[str, str] = {}
    for i, name in enumerate(_DIGIT_NAMES):
        tag = f"\ufdd0{i}\ufdd0"  # Unicode noncharacter — safe sentinel
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
    """Expression evaluator module.  Supports math functions and implicit multiplication."""
    COMMANDS: dict[str, str] = {"cc": "cmd_calc"}

    async def cmd_calc(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Evaluate a mathematical expression and display the result."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}cc <expression>  e.g. {p}cc 2pi")
            return
        self.bot.privmsg(reply_to, f"[calc] {arg} = {_calc(arg)}")

    def help_lines(self, prefix: str) -> list[str]:
        """Return calculator help text."""
        return [f"  {prefix}cc <expression>   Calculator  e.g. {prefix}cc 2pi  {prefix}cc sqrt(144)"]


def setup(bot: object) -> CalcModule:
    """Module entry point — returns a CalcModule instance."""
    return CalcModule(bot)  # type: ignore[arg-type]
