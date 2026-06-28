"""SpaceX next-launch command via Launch Library 2 (thespacedevs).

No API key required.  The old community api.spacexdata.com endpoint was
abandoned and now returns HTTP 525 (Cloudflare-to-origin TLS failure), so
this uses Launch Library 2 instead (the same source ``.launches`` uses).
One request per call (rocket + pad come nested in the result), cached briefly
because LL2's anonymous tier is rate-limited.

Output (single IRC line)::

    next SpaceX launch  |  Falcon 9 Block 5 / Starlink Group 12-3  |
    T-2d 4h 17m (2026-05-22 14:00 UTC)  |  pad: LC-39A, Kennedy Space Center
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .base import BotModule, ResponseTooLarge, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.spacex")

_LL2_URL = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
_MAX_BODY_BYTES = 512 * 1024     # detailed launch records can be sizable
_CACHE_TTL = 180.0               # LL2 anon tier is ~15 req/hr; cache to be gentle
_cache: dict[str, object] = {"ts": 0.0, "val": ""}


def _fmt_countdown(date_unix: int) -> str:
    delta = date_unix - int(time.time())
    prefix = "T-" if delta >= 0 else "T+"
    delta = abs(delta)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{prefix}{days}d {hours}h {minutes}m"


def _fmt_when(net: str | None) -> str:
    if not net:
        return "date TBD"
    try:
        dt = datetime.fromisoformat(str(net).replace("Z", "+00:00"))
        unix = int(dt.timestamp())
        utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"{_fmt_countdown(unix)} ({utc})"
    except (ValueError, TypeError):
        return strip_ctrl(str(net), 32)


def _fetch_sync(ua: str) -> str:
    now = time.time()
    if _cache["val"] and now - float(_cache["ts"]) < _CACHE_TTL:  # type: ignore[arg-type]
        return str(_cache["val"])
    try:
        d = fetch_json(
            _LL2_URL,
            params={"search": "SpaceX", "limit": "1"},
            ua=ua, timeout=12, max_bytes=_MAX_BODY_BYTES,
        )
    except (ResponseTooLarge, ValueError, TypeError) as e:
        log.warning(f"spacex LL2: {e}")
        return "SpaceX launch data unavailable"
    except Exception as e:  # requests.RequestException
        log.warning(f"spacex LL2: {e}")
        return "SpaceX launch data unavailable"

    results = d.get("results") if isinstance(d, dict) else None
    if not results or not isinstance(results, list) or not isinstance(results[0], dict):
        return "no upcoming SpaceX launch found"
    r = results[0]

    # LL2 names are "Rocket | Mission"; keep the mission half so we don't repeat
    # the rocket configuration we already show.
    raw_name = str(r.get("name") or "unknown mission")
    name = strip_ctrl(raw_name.split(" | ", 1)[-1], 120)
    cfg = (r.get("rocket") or {}).get("configuration") or {}
    rocket = strip_ctrl(str(cfg.get("full_name") or cfg.get("name") or ""), 60)
    pad = r.get("pad") or {}
    pad_bits = [str(pad.get("name") or ""),
                str((pad.get("location") or {}).get("name") or "")]
    pad_full = strip_ctrl(", ".join(x for x in pad_bits if x), 100)
    status = strip_ctrl(str((r.get("status") or {}).get("abbrev") or ""), 24)
    when = _fmt_when(r.get("net"))

    bits = ["\x02next SpaceX launch\x02"]
    head = " / ".join(x for x in (rocket, name) if x)
    if head:
        bits.append(head)
    bits.append(when)
    if pad_full:
        bits.append(f"pad: {pad_full}")
    if status:
        bits.append(status)
    out = strip_ctrl("  |  ".join(bits), 400)
    _cache["ts"], _cache["val"] = now, out
    return out


class SpacexModule(BotModule):
    """`.spacex` - next scheduled SpaceX launch (Launch Library 2)."""

    COMMANDS: dict[str, str] = {"spacex": "cmd_spacex"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_spacex(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "spacex", "Next scheduled SpaceX launch")]


def setup(bot: object) -> SpacexModule:
    return SpacexModule(bot)  # type: ignore[arg-type]
