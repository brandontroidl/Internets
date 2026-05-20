"""SpaceX next-launch command — wraps the community api.spacexdata.com API.

No API key required.  Three calls per ``.spacex`` invocation:

  1. ``GET /v5/launches/next`` — next-scheduled launch record
  2. ``GET /v4/rockets/<rocket_id>`` — rocket display name
  3. ``GET /v4/launchpads/<launchpad_id>`` — pad full name

Output (single IRC line)::

    \\x02next SpaceX launch\\x02 — Falcon Heavy / Starlink Group 12-3
    |  T-2d 4h 17m (2026-05-22 14:00 UTC)
    |  pad: LC-39A, Kennedy Space Center
    |  https://www.spacex.com/launches/
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests
from .base import BotModule

log = logging.getLogger("internets.spacex")

_NEXT_URL = "https://api.spacexdata.com/v5/launches/next"
_ROCKET_URL = "https://api.spacexdata.com/v4/rockets/{}"
_PAD_URL = "https://api.spacexdata.com/v4/launchpads/{}"
_LANDING = "https://www.spacex.com/launches/"
_MAX_BODY_BYTES = 64 * 1024
_IRC_CTRL_BYTES = frozenset(
    ["\r", "\n", "\x00", "\x01", "\x02", "\x03",
     "\x04", "\x0f", "\x16", "\x1d", "\x1f"]
)


def _strip_ctrl(s: str, max_len: int = 400) -> str:
    return "".join(ch for ch in s if ch not in _IRC_CTRL_BYTES)[:max_len]


def _get_json(url: str, ua: str) -> Any | None:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10, stream=True,
        )
        r.raise_for_status()
        body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
        if len(body) > _MAX_BODY_BYTES:
            log.warning("spacex: response too large from %s", url)
            return None
        return json.loads(body.decode("utf-8", errors="replace"))
    except requests.RequestException as e:
        log.warning(f"spacex request {url}: {e}")
        return None
    except ValueError as e:
        log.warning(f"spacex parse {url}: {e!r}")
        return None


def _fmt_countdown(date_unix: int) -> str:
    now = int(time.time())
    delta = date_unix - now
    prefix = "T-" if delta >= 0 else "T+"
    delta = abs(delta)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{prefix}{days}d {hours}h {minutes}m"


def _fmt_utc(date_unix: int) -> str:
    dt = datetime.fromtimestamp(date_unix, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fetch_sync(ua: str) -> str:
    nxt = _get_json(_NEXT_URL, ua)
    if not isinstance(nxt, dict):
        return "SpaceX API unavailable"

    name = nxt.get("name") or "unknown mission"
    date_unix = nxt.get("date_unix")
    rocket_id = nxt.get("rocket")
    pad_id = nxt.get("launchpad")
    details = nxt.get("details") or ""

    if not isinstance(date_unix, int):
        try:
            date_unix = int(date_unix) if date_unix is not None else 0
        except (TypeError, ValueError):
            date_unix = 0

    rocket_name = "unknown rocket"
    if isinstance(rocket_id, str) and rocket_id:
        rd = _get_json(_ROCKET_URL.format(rocket_id), ua)
        if isinstance(rd, dict) and rd.get("name"):
            rocket_name = str(rd["name"])

    pad_name = "unknown pad"
    if isinstance(pad_id, str) and pad_id:
        pd = _get_json(_PAD_URL.format(pad_id), ua)
        if isinstance(pd, dict) and pd.get("full_name"):
            pad_name = str(pd["full_name"])

    if date_unix:
        when = f"{_fmt_countdown(date_unix)} ({_fmt_utc(date_unix)})"
    else:
        when = "date TBD"

    line = (
        f"\x02next SpaceX launch\x02 — {rocket_name} / {name}  |  "
        f"{when}  |  pad: {pad_name}  |  {_LANDING}"
    )

    if isinstance(details, str):
        details = details.strip()
        if details and len(details) < 80:
            line += f" | {details}"

    return _strip_ctrl(line)


class SpacexModule(BotModule):
    """`.spacex` — next scheduled SpaceX launch."""

    COMMANDS: dict[str, str] = {"spacex": "cmd_spacex"}

    def on_load(self) -> None:
        from .base import cred
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    async def cmd_spacex(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        text = await asyncio.to_thread(_fetch_sync, self._ua)
        self.bot.privmsg(reply_to, text)

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}spacex                  Next scheduled SpaceX launch"]


def setup(bot: object) -> SpacexModule:
    return SpacexModule(bot)  # type: ignore[arg-type]
