"""Weather command module — multi-provider with automatic fallback.

Uses the ``weather_providers`` package to query Open-Meteo, WeatherAPI.com,
or Tomorrow.io in priority order.  The first successful response wins.
Provider priority and API keys are configured in ``config.ini`` under
``[weather_providers]``.
"""

from __future__ import annotations

import re
import logging

from .base    import BotModule
from .geocode import geocode
from .units   import cf, kph, km_mi, mb

log = logging.getLogger("internets.weather")

# SEC-WP-004: Strip IRC formatting and control characters from API-sourced
# strings before they reach privmsg.  Prevents a malicious/misconfigured
# API from injecting bold, colour, reverse, CTCP, or raw CR/LF into IRC.
_IRC_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

def _sanitize(s: str, max_len: int = 200) -> str:
    """Strip IRC control chars and truncate untrusted API strings."""
    return _IRC_CTRL_RE.sub("", s)[:max_len]


def _format_current(r: object) -> str:
    """Format a WeatherResult for current conditions as an IRC line."""
    from weather_providers import WeatherResult
    # SEC-WP-005: Explicit type guard — survives python -O.
    if not isinstance(r, WeatherResult):
        raise TypeError(f"expected WeatherResult, got {type(r).__name__}")

    if r.wind_kph is not None and r.wind_kph < 1:
        wind_str = "Calm"
    elif r.wind_kph is not None:
        wd = _sanitize(r.wind_dir, 4)
        wind_str = f"from {wd} at {kph(r.wind_kph)}" if wd else kph(r.wind_kph)
    else:
        wind_str = "N/A"

    desc = _sanitize(r.description)
    source = _sanitize(r.source, 30)

    parts: list[str] = [
        f"Conditions {desc}",
        f"Temperature {cf(r.temperature)}",
    ]

    if (r.feels_like_c is not None and r.temperature is not None
            and abs(r.feels_like_c - r.temperature) >= 2):
        parts.append(f"Feels like {cf(r.feels_like_c)}")

    parts += [
        f"Dew point {cf(r.dewpoint_c)}",
        f"Pressure {mb(r.pressure_mb)}",
        f"Humidity {f'{r.humidity:.0f}%' if r.humidity is not None else 'N/A'}",
        f"Visibility {km_mi(r.visibility_m)}",
        f"Wind {wind_str}",
    ]
    parts.append(f"[{source}]")
    return " :: ".join(parts)


def _format_forecast(r: object) -> str:
    """Format a WeatherResult's forecast days as an IRC line."""
    from weather_providers import WeatherResult
    # SEC-WP-005: Explicit type guard.
    if not isinstance(r, WeatherResult):
        raise TypeError(f"expected WeatherResult, got {type(r).__name__}")

    if not r.forecast:
        return ""

    source = _sanitize(r.source, 30)
    chunks: list[str] = []
    for day in r.forecast:
        hi = cf(day.high_c)
        lo = cf(day.low_c) if day.low_c is not None else "N/A"
        name = _sanitize(day.day_name, 20)
        desc = _sanitize(day.description)
        chunks.append(f"{name} {desc} {hi} / {lo}")
    chunks.append(f"[{source}]")
    return " :: ".join(chunks)


class WeatherModule(BotModule):
    """Multi-provider weather commands — current conditions and forecast."""

    COMMANDS: dict[str, str] = {
        "weather":  "cmd_weather",  "w":  "cmd_weather",
        "forecast": "cmd_forecast", "f":  "cmd_forecast",
    }

    def on_load(self) -> None:
        """Configure weather providers from bot config."""
        from weather_providers import configure
        configure(self.bot.cfg)

        self._ua       = self.bot.cfg["weather"]["user_agent"]
        self._cooldown = int(self.bot.cfg["bot"]["api_cooldown"])

    def _resolve(self, nick: str, arg: str | None) -> tuple[str | None, str | None]:
        """Resolve a weather query to a raw location string.

        Handles ``-n othernick`` lookups and saved-location fallback.
        Returns ``(raw, error_msg)`` — one is always None.
        """
        if arg:
            m = re.match(r"^-n\s+(\S+)$", arg.strip(), re.IGNORECASE)
            if m:
                saved = self.bot.loc_get(m.group(1))
                return (saved, None) if saved else (None, f"{m.group(1)} has no saved location.")
            return arg.strip(), None
        saved = self.bot.loc_get(nick)
        if saved:
            return saved, None
        p = self.bot.cfg["bot"]["command_prefix"]
        return None, f"{nick}: no location saved — use {p}regloc <city or zip> first."

    async def _geo(self, nick: str, reply_to: str,
                   arg: str | None) -> tuple[float, float, str, str] | None:
        """Geocode a weather query, with error replies and rate limiting."""
        raw, err = self._resolve(nick, arg)
        if raw is None:
            self.bot.privmsg(reply_to, err)  # type: ignore[arg-type]
            return None
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down ({self._cooldown}s cooldown)")
            return None
        geo = await geocode(raw, self._ua)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: location not found: '{raw}'")
        return geo

    async def cmd_weather(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display current weather conditions for a location."""
        from weather_providers import get_weather

        geo = await self._geo(nick, reply_to, arg)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("weather %r (%s) [%.4f,%.4f]", display, cc or "?", lat, lon)

        result = await get_weather(lat, lon, display)
        if result:
            body = _format_current(result)
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: weather data unavailable right now.")

    async def cmd_forecast(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display a multi-day forecast for a location."""
        from weather_providers import get_forecast

        geo = await self._geo(nick, reply_to, arg)
        if geo is None:
            return
        lat, lon, display, cc = geo
        log.info("forecast %r (%s) [%.4f,%.4f]", display, cc or "?", lat, lon)

        result = await get_forecast(lat, lon, display, days=4)
        if result and result.forecast:
            body = _format_forecast(result)
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: forecast unavailable right now.")

    def help_lines(self, prefix: str) -> list[str]:
        """Return weather help text."""
        return [
            f"  {prefix}weather/.w  [zip|city|-n nick]   Current conditions (worldwide)",
            f"  {prefix}forecast/.f [zip|city|-n nick]   Multi-day forecast (worldwide)",
        ]


def setup(bot: object) -> WeatherModule:
    """Module entry point — returns a WeatherModule instance."""
    return WeatherModule(bot)  # type: ignore[arg-type]
