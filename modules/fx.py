"""FX (foreign-exchange) command — wraps frankfurter.dev (ECB rates).

No API key required.  Single call::

    GET https://api.frankfurter.dev/v1/latest?base=<FROM>&symbols=<TO>

Response shape::

    {"amount": 1.0, "base": "USD", "date": "2026-05-19",
     "rates": {"EUR": 0.9215}}

Output::

    1.00 USD = 0.9215 EUR  (frankfurter.dev — ECB rates)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import requests
from .base import BotModule

log = logging.getLogger("internets.fx")

_URL = "https://api.frankfurter.dev/v1/latest"
_MAX_BODY_BYTES = 16 * 1024
_CCY_RE = re.compile(r"^[A-Za-z]{3}$")
_MAX_AMOUNT = 1e12
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _fmt_amount(n: float) -> str:
    """Format a money amount — 2 decimals for ≥1, 4 sig figs for <1."""
    if abs(n) >= 1:
        return f"{n:,.2f}"
    # 4 significant digits for sub-unit results
    return f"{n:,.4g}"


def _fetch_sync(src: str, dst: str, amount: float, ua: str) -> str:
    try:
        r = requests.get(
            _URL,
            params={"base": src, "symbols": dst},
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=8, stream=True,
        )
        if r.status_code == 404:
            return "unknown currency code"
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("fx response too large")
            return "fx response too large"
        d = json.loads(body.decode("utf-8", errors="replace"))
    except requests.RequestException as e:
        log.warning(f"fx request: {e}")
        return "fx API unavailable"
    except ValueError as e:
        log.warning(f"fx parse: {e!r}")
        return "fx parse error"

    rates = d.get("rates") if isinstance(d, dict) else None
    if not isinstance(rates, dict) or dst not in rates:
        return "unknown currency code"
    try:
        rate = float(rates[dst])
    except (TypeError, ValueError):
        return "fx parse error"
    converted = rate * amount
    text = (
        f"{_fmt_amount(amount)} {src} = {_fmt_amount(converted)} {dst}  "
        f"(frankfurter.dev — ECB rates)"
    )
    return _strip_ctrl(text)


class FxModule(BotModule):
    """`.fx <from> <to> [amount]` — ECB foreign-exchange rate lookup."""

    COMMANDS: dict[str, str] = {"fx": "cmd_fx"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_fx(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        parts = (arg or "").split()
        if len(parts) < 2 or len(parts) > 3:
            self.bot.privmsg(reply_to, "fx <from> <to> [amount]")
            return
        src, dst = parts[0], parts[1]
        if not _CCY_RE.match(src) or not _CCY_RE.match(dst):
            self.bot.privmsg(reply_to, "fx <from> <to> [amount]")
            return
        amount = 1.0
        if len(parts) == 3:
            try:
                amount = float(parts[2])
            except ValueError:
                self.bot.privmsg(reply_to, "fx <from> <to> [amount]")
                return
            if not (amount > 0) or amount > _MAX_AMOUNT:
                self.bot.privmsg(reply_to, "fx <from> <to> [amount]")
                return
        src_u = src.upper()
        dst_u = dst.upper()
        text = await asyncio.to_thread(_fetch_sync, src_u, dst_u, amount, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}fx <from> <to> [amount]  FX conversion via frankfurter.dev (ECB)",
        ]


def setup(bot: object) -> FxModule:
    return FxModule(bot)  # type: ignore[arg-type]
