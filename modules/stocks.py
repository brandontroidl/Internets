from __future__ import annotations

import asyncio
import logging
import time
from typing import Any


from .base import BotModule, fetch_json, help_row

log = logging.getLogger("internets.stocks")

# ── Provider implementations ─────────────────────────────────────────────────
#
# Each provider function is synchronous (blocking HTTP) and called via
# asyncio.to_thread.  Returns a formatted string or raises on failure.
#
# Supported free-tier providers:
#   Finnhub      — 60 calls/min   https://finnhub.io/register
#   Alpha Vantage — 25 calls/day  https://www.alphavantage.co/support/#api-key
#   Twelve Data  — 800 calls/day  https://twelvedata.com/account
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_change(change: float, pct: float) -> str:
    """Format price change with direction arrow."""
    arrow = "\x0303▲\x03" if change >= 0 else "\x0304▼\x03"  # green / red
    sign = "+" if change >= 0 else ""
    return f"{arrow} {sign}{change:.2f} ({sign}{pct:.2f}%)"


def _fmt_number(n: float) -> str:
    """Format large numbers with K/M/B suffixes."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:.2f}"


# ── Finnhub ──────────────────────────────────────────────────────────────────

def _finnhub_quote(symbol: str, key: str, ua: str) -> str:
    d: dict[str, Any] = fetch_json(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol.upper(), "token": key},
        ua=ua,
        timeout=10,
    )
    c = d.get("c", 0)  # current
    if c == 0:
        raise ValueError("no data")
    pc = d.get("pc", 0)  # previous close
    chg = c - pc
    pct = (chg / pc * 100) if pc else 0
    h, lo, o = d.get("h", 0), d.get("l", 0), d.get("o", 0)
    return (
        f"\x02{symbol.upper()}\x02 ${c:.2f} {_fmt_change(chg, pct)} "
        f"| O: ${o:.2f}  H: ${h:.2f}  L: ${lo:.2f}  "
        f"| prev close ${pc:.2f}  [finnhub]"
    )


def _finnhub_crypto(symbol: str, key: str, ua: str) -> str:
    # Finnhub uses BINANCE:BTCUSDT style
    pair = f"BINANCE:{symbol.upper()}USDT"
    d = fetch_json(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": pair, "token": key},
        ua=ua,
        timeout=10,
    )
    c = d.get("c", 0)
    if c == 0:
        raise ValueError("no data")
    pc = d.get("pc", 0)
    chg = c - pc
    pct = (chg / pc * 100) if pc else 0
    return (
        f"\x02{symbol.upper()}/USDT\x02 ${c:,.2f} {_fmt_change(chg, pct)} "
        f"| H: ${d.get('h', 0):,.2f}  L: ${d.get('l', 0):,.2f}  [finnhub]"
    )


# ── Alpha Vantage ────────────────────────────────────────────────────────────

def _alphavantage_quote(symbol: str, key: str, ua: str) -> str:
    gq = fetch_json(
        "https://www.alphavantage.co/query",
        params={"function": "GLOBAL_QUOTE", "symbol": symbol.upper(), "apikey": key},
        ua=ua,
        timeout=10,
    ).get("Global Quote", {})
    price = float(gq.get("05. price", 0))
    if price == 0:
        raise ValueError("no data")
    chg = float(gq.get("09. change", 0))
    pct = float(gq.get("10. change percent", "0%").rstrip("%"))
    o = float(gq.get("02. open", 0))
    h = float(gq.get("03. high", 0))
    lo = float(gq.get("04. low", 0))
    vol = float(gq.get("06. volume", 0))
    return (
        f"\x02{symbol.upper()}\x02 ${price:.2f} {_fmt_change(chg, pct)} "
        f"| O: ${o:.2f}  H: ${h:.2f}  L: ${lo:.2f}  "
        f"| vol {_fmt_number(vol)}  [alphavantage]"
    )


def _alphavantage_crypto(symbol: str, key: str, ua: str) -> str:
    er = fetch_json(
        "https://www.alphavantage.co/query",
        params={
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": symbol.upper(),
            "to_currency": "USD",
            "apikey": key,
        },
        ua=ua,
        timeout=10,
    ).get("Realtime Currency Exchange Rate", {})
    price = float(er.get("5. Exchange Rate", 0))
    if price == 0:
        raise ValueError("no data")
    return (
        f"\x02{symbol.upper()}/USD\x02 ${price:,.2f}  "
        f"| bid ${float(er.get('8. Bid Price', 0)):,.2f}  "
        f"ask ${float(er.get('9. Ask Price', 0)):,.2f}  [alphavantage]"
    )


# ── Twelve Data ──────────────────────────────────────────────────────────────

def _twelvedata_quote(symbol: str, key: str, ua: str) -> str:
    d = fetch_json(
        "https://api.twelvedata.com/quote",
        params={"symbol": symbol.upper(), "apikey": key},
        ua=ua,
        timeout=10,
    )
    if d.get("code"):
        raise ValueError(d.get("message", "error"))
    price = float(d.get("close", 0))
    if price == 0:
        raise ValueError("no data")
    chg = float(d.get("change", 0))
    pct = float(d.get("percent_change", 0))
    o = float(d.get("open", 0))
    h = float(d.get("high", 0))
    lo = float(d.get("low", 0))
    vol = float(d.get("volume", 0))
    return (
        f"\x02{symbol.upper()}\x02 ${price:.2f} {_fmt_change(chg, pct)} "
        f"| O: ${o:.2f}  H: ${h:.2f}  L: ${lo:.2f}  "
        f"| vol {_fmt_number(vol)}  [twelvedata]"
    )


def _twelvedata_crypto(symbol: str, key: str, ua: str) -> str:
    d = fetch_json(
        "https://api.twelvedata.com/quote",
        params={"symbol": f"{symbol.upper()}/USD", "apikey": key},
        ua=ua,
        timeout=10,
    )
    if d.get("code"):
        raise ValueError(d.get("message", "error"))
    price = float(d.get("close", 0))
    if price == 0:
        raise ValueError("no data")
    chg = float(d.get("change", 0))
    pct = float(d.get("percent_change", 0))
    return (
        f"\x02{symbol.upper()}/USD\x02 ${price:,.2f} {_fmt_change(chg, pct)} "
        f"| H: ${float(d.get('high', 0)):,.2f}  L: ${float(d.get('low', 0)):,.2f}  "
        f"[twelvedata]"
    )


# ── Provider registry ────────────────────────────────────────────────────────

_STOCK_PROVIDERS: list[tuple[str, str, Any]] = [
    ("finnhub",       "finnhub_key",       _finnhub_quote),
    ("alphavantage",  "alphavantage_key",  _alphavantage_quote),
    ("twelvedata",    "twelvedata_key",    _twelvedata_quote),
]

_CRYPTO_PROVIDERS: list[tuple[str, str, Any]] = [
    ("finnhub",       "finnhub_key",       _finnhub_crypto),
    ("alphavantage",  "alphavantage_key",  _alphavantage_crypto),
    ("twelvedata",    "twelvedata_key",    _twelvedata_crypto),
]


def _try_providers(
    providers: list[tuple[str, str, Any]],
    symbol: str,
    keys: dict[str, str],
    ua: str,
) -> str:
    """Try each provider in order, return first success."""
    errors: list[str] = []
    for name, key_field, fn in providers:
        key = keys.get(key_field, "")
        if not key:
            continue
        try:
            return fn(symbol, key, ua)
        except Exception as e:
            log.debug(f"{name} failed for {symbol}: {e}")
            errors.append(f"{name}: {e}")
    if not any(keys.get(kf) for _, kf, _ in providers):
        return "no finance API keys configured — see [stocks] in config.ini"
    return f"all providers failed for '{symbol}' ({'; '.join(errors)})"


# ── Module ───────────────────────────────────────────────────────────────────

class StocksModule(BotModule):
    """Stock & crypto price lookup with multi-provider failover."""

    COMMANDS: dict[str, str] = {
        "stock":  "cmd_stock",
        "s":      "cmd_stock",
        "crypto": "cmd_crypto",
    }

    def on_load(self) -> None:
        """Read API keys + UA via secret_store (config.ini as fallback)."""
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._keys: dict[str, str] = {}
        for field in ("finnhub_key", "alphavantage_key", "twelvedata_key"):
            val = cred(self.bot.cfg, field, "stocks", field)
            if val:
                self._keys[field] = val
        active = [n for n, kf, _ in _STOCK_PROVIDERS if self._keys.get(kf)]
        log.info(f"stocks: active providers: {active or ['none']}")

    def is_configured(self) -> bool:
        # At least one provider key must be present for stocks/crypto to work.
        return bool(self._keys)

    async def cmd_stock(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a stock quote.  Usage: .stock AAPL"""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(
                reply_to,
                f"{nick}: {p}stock <symbol>  e.g. {p}stock AAPL  |  "
                f"{p}crypto <symbol>  e.g. {p}crypto BTC",
            )
            return
        symbol = arg.strip().split()[0]
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(
            _try_providers, _STOCK_PROVIDERS, symbol, self._keys, self._ua,
        )
        self.bot.privmsg(reply_to, result)

    async def cmd_crypto(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Look up a cryptocurrency price.  Usage: .crypto BTC"""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}crypto <symbol>  e.g. {p}crypto BTC")
            return
        symbol = arg.strip().split()[0]
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        result = await asyncio.to_thread(
            _try_providers, _CRYPTO_PROVIDERS, symbol, self._keys, self._ua,
        )
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        """Return stocks help text."""
        return [
            help_row(prefix, "stock/.s <symbol>", f"Stock quote     e.g. {prefix}s AAPL"),
            help_row(prefix, "crypto <symbol>", f"Crypto price    e.g. {prefix}crypto BTC"),
        ]


def setup(bot: object) -> StocksModule:
    """Module entry point — returns a StocksModule instance."""
    return StocksModule(bot)  # type: ignore[arg-type]
