"""Satellite visible passes via N2YO — requires an n2yo_api_key.

    .passes <sat> <lat,lon>   next visible pass: start, max elevation, duration

Free key from https://www.n2yo.com/api/ .  Without a key the command is
inert (and hidden from .help via is_configured()).  <sat> is a NORAD id or
one of the common names below.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import requests
from .base import BotModule, ResponseTooLarge, cred, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.satpass")

_BASE = "https://api.n2yo.com/rest/v1/satellite/visualpasses"
_SATS: dict[str, int] = {
    "iss": 25544, "zarya": 25544, "hst": 20580, "hubble": 20580,
    "css": 48274, "tiangong": 48274, "noaa-15": 25338, "noaa-18": 28654,
    "noaa-19": 33591, "terra": 25994, "aqua": 27424, "landsat-8": 39084,
    "landsat-9": 49260, "envisat": 27386,
}


def _fetch(satid: int, lat: float, lon: float, key: str, ua: str) -> str:
    try:
        # /{id}/{lat}/{lon}/{alt}/{days}/{min_visibility_sec}/
        d = fetch_json(f"{_BASE}/{satid}/{lat}/{lon}/0/5/30/",
                       ua=ua, params={"apiKey": key}, timeout=12)
        if not isinstance(d, dict):
            return "satellite pass data unavailable"
        info = d.get("info") or {}
        name = strip_ctrl(info.get("satname") or str(satid), 40)
        passes = d.get("passes") or []
        if not passes:
            return f"{name}: no visible passes in the next 5 days from here"
        p0 = passes[0]
        start = datetime.fromtimestamp(
            float(p0["startUTC"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        maxel = p0.get("maxEl", "?")
        dur = p0.get("duration", "?")
        return strip_ctrl(
            f"{name} next pass: {start} :: max elevation {maxel}° :: duration {dur}s")
    except (requests.RequestException, ResponseTooLarge) as e:
        log.warning("satpass request: %s", e)
        return "satellite pass lookup failed"
    except Exception as e:  # parse — never raise to caller  # noqa: BLE001
        log.warning("satpass parse: %r", e)
        return "satellite pass data unavailable"


class SatpassModule(BotModule):
    """`.passes` — next visible satellite pass (N2YO, needs n2yo_api_key)."""

    COMMANDS: dict[str, str] = {"passes": "cmd_passes"}

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")
        self._key: str = cred(self.bot.cfg, "n2yo_api_key",
                             "satpass", "n2yo_api_key", "")

    def is_configured(self) -> bool:
        return bool(getattr(self, "_key", ""))

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_passes(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        key = getattr(self, "_key", "")
        if not key:
            self.bot.privmsg(reply_to,
                             f"{nick}: .passes needs an n2yo_api_key (free at n2yo.com/api)")
            return
        parts = (arg or "").split()
        p = self.bot.cfg["bot"]["command_prefix"]
        if len(parts) < 2:
            self.bot.privmsg(
                reply_to, f"{nick}: {p}passes <sat> <lat,lon>  e.g. {p}passes iss 34.1,-117.8")
            return
        sat = parts[0].lower()
        satid = _SATS.get(sat)
        if satid is None and sat.isdigit():
            satid = int(sat)
        if satid is None:
            names = ", ".join(sorted(_SATS)[:6])
            self.bot.privmsg(
                reply_to, f"{nick}: unknown satellite '{strip_ctrl(sat, 20)}' — "
                f"use a NORAD id or one of: {names}…")
            return
        try:
            lat_s, lon_s = parts[1].split(",")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            self.bot.privmsg(reply_to, f"{nick}: location must be lat,lon  e.g. 34.1,-117.8")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self.bot.privmsg(reply_to, "lat/lon out of range")
            return
        result = await asyncio.to_thread(_fetch, satid, lat, lon, key, self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "passes <sat> <lat,lon>",
                     "Next visible satellite pass (needs n2yo key)"),
        ]


def setup(bot: object) -> SatpassModule:
    return SatpassModule(bot)  # type: ignore[arg-type]
