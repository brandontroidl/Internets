"""Crypto price command - wraps CoinGecko's free public API.

No API key required.  Two-call flow per lookup:

  1. ``GET /api/v3/search?query=<input>``
     Resolve the user input (symbol or name) to a CoinGecko coin id.
     A small per-instance ``self._cache`` skips this call on repeat
     lookups for the same lowercased input.

  2. ``GET /api/v3/simple/price?ids=<coin_id>&vs_currencies=usd
        &include_24hr_change=true&include_market_cap=true``
     Pull the price, 24h percent change and market cap.

Output (single IRC line, ``\\x02`` for bold, ``\\x03NN`` for colour)::

    \\x02BTC\\x02 $43,210.50  \\x03031234.56% 24h\\x03  |  market cap $850.0B
    |  coingecko: bitcoin
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import requests
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.crypto")

_SEARCH_URL = "https://api.coingecko.com/api/v3/search"
_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_MAX_BODY_BYTES = 256 * 1024  # search responses can be sizeable
_CACHE_MAX = 512  # bound the per-instance query -> coin_id cache (FIFO evict)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return strip_ctrl(s, max_len)


def _fmt_marketcap(n: float) -> str:
    """Format a USD market cap with K/M/B/T suffix."""
    if n >= 1_000_000_000_000:
        return f"${n / 1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.2f}"


def _fmt_price(p: float) -> str:
    """Format a USD spot price - comma thousands, sane precision for tiny coins."""
    if p >= 1:
        return f"${p:,.2f}"
    if p >= 0.01:
        return f"${p:,.4f}"
    # very small: keep four significant digits
    return f"${p:.4g}"


def _get_json(url: str, params: dict[str, str], ua: str) -> Any | None:
    try:
        with requests.get(
            url, params=params,
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10, stream=True,
        ) as r:
            r.raise_for_status()
            body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
            if len(body) > _MAX_BODY_BYTES:
                log.warning("crypto: response too large from %s", url)
                return None
            return json.loads(body.decode("utf-8", errors="replace"))
    except requests.RequestException as e:
        log.warning(f"crypto request: {e}")
        return None
    except ValueError as e:
        log.warning(f"crypto parse: {e!r}")
        return None


def _resolve_coin_id(query: str, ua: str) -> str | None:
    """Return CoinGecko coin id for the user's input, or None if no match."""
    d = _get_json(_SEARCH_URL, {"query": query}, ua)
    if not isinstance(d, dict):
        return None
    coins = d.get("coins") or []
    if not coins:
        return None
    q_lower = query.lower()
    for c in coins:
        sym = str(c.get("symbol", "")).lower()
        if sym == q_lower:
            cid = c.get("id")
            if isinstance(cid, str) and cid:
                return cid
    first = coins[0]
    cid = first.get("id")
    return cid if isinstance(cid, str) and cid else None


def _fetch_sync(query: str, cache: dict[str, str], ua: str) -> str:
    q = query.strip()
    if not q:
        return "usage: .crypto <symbol-or-name>"
    key = q.lower()
    coin_id = cache.get(key)
    if not coin_id:
        coin_id = _resolve_coin_id(q, ua)
        if not coin_id:
            return _strip_ctrl(f"no coin matched '{q}'")
        if len(cache) >= _CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = coin_id

    price_data = _get_json(
        _PRICE_URL,
        {
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
        },
        ua,
    )
    if not isinstance(price_data, dict) or coin_id not in price_data:
        return "coingecko price unavailable"
    info = price_data[coin_id]
    if not isinstance(info, dict):
        return "coingecko price unavailable"
    try:
        price = float(info.get("usd", 0) or 0)
        change = float(info.get("usd_24h_change", 0) or 0)
        mcap = float(info.get("usd_market_cap", 0) or 0)
    except (TypeError, ValueError):
        return "coingecko price parse error"
    if price <= 0:
        return "coingecko price unavailable"

    arrow = "↑" if change >= 0 else "↓"
    colour = "\x0303" if change >= 0 else "\x0304"  # green / red
    sign = "+" if change >= 0 else ""
    display_sym = q.upper() if len(q) <= 6 else coin_id.upper()
    text = (
        f"\x02{display_sym}\x02 {_fmt_price(price)}  "
        f"{colour}{arrow}{sign}{change:.2f}% 24h\x03  |  "
        f"market cap {_fmt_marketcap(mcap)}  |  "
        f"coingecko: {coin_id}"
    )
    return _strip_ctrl(text)


class CryptoModule(BotModule):
    """`.gecko <symbol-or-name>` - spot price from CoinGecko (no key).

    The shorter aliases `.gecko` / `.cg` are used so this module can
    coexist with the keyed ``stocks.crypto`` command (which routes via
    Finnhub/Alphavantage/Twelvedata and requires those API keys).
    """

    COMMANDS: dict[str, str] = {
        "gecko": "cmd_crypto",
        "coingecko": "cmd_crypto",
        "cg": "cmd_crypto",
    }

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._cache: dict[str, str] = {}

    def is_configured(self) -> bool:
        return True

    async def cmd_crypto(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}gecko <symbol-or-name>  e.g. {p}gecko btc")
            return
        query = arg.strip().split()[0]
        text = await asyncio.to_thread(_fetch_sync, query, self._cache, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "gecko/.cg <symbol>", "Crypto spot price + 24h change via CoinGecko"),
        ]


def setup(bot: object) -> CryptoModule:
    return CryptoModule(bot)  # type: ignore[arg-type]
