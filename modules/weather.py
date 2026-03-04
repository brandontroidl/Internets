from __future__ import annotations

import asyncio
import re
import logging
import requests
from datetime import datetime

from .base    import BotModule
from .geocode import geocode
from .units   import cf, kph, km_mi, mb, deg_to_card, fmt_dt
from .        import nws
from .nws     import WeatherDict

log = logging.getLogger("internets.weather")

_OM_BASE  = "https://api.open-meteo.com/v1/forecast"
_OM_CURRENT_FIELDS = ",".join([
    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
    "dew_point_2m", "weather_code", "surface_pressure",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "visibility",
])

WMO_CODES: dict[int, str] = {
    0: "Clear",
    1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    56: "Freezing Drizzle", 57: "Heavy Freezing Drizzle",
    61: "Slight Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Heavy Freezing Rain",
    71: "Slight Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains",
    80: "Slight Showers", 81: "Showers", 82: "Violent Showers",
    85: "Snow Showers", 86: "Heavy Snow Showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ Hail", 99: "Thunderstorm w/ Heavy Hail",
}


def _om_get(url: str, *, params: dict | None = None,
            timeout: int = 10) -> requests.Response:
    """Blocking HTTP GET for Open-Meteo — called via asyncio.to_thread."""
    return requests.get(url, params=params, timeout=timeout)


async def _om_current(lat: float, lon: float) -> WeatherDict | None:
    try:
        r = await asyncio.to_thread(
            _om_get, _OM_BASE,
            params={
                "latitude": lat, "longitude": lon,
                "current": _OM_CURRENT_FIELDS,
                "wind_speed_unit": "kmh",
                "timezone": "auto",
            }, timeout=10,
        )
        r.raise_for_status()
        cur = r.json().get("current", {})

        temp_c   = cur.get("temperature_2m")
        feels_c  = cur.get("apparent_temperature")
        wcode    = cur.get("weather_code")
        desc     = WMO_CODES.get(wcode, f"Code {wcode}") if wcode is not None else None

        return {
            "conditions":     desc,
            "temp_c":         temp_c,
            "feels_c":        feels_c,
            "feels_label":    "Feels like" if feels_c is not None else None,
            "dewpoint_c":     cur.get("dew_point_2m"),
            "pressure_mb":    cur.get("surface_pressure"),
            "humidity":       cur.get("relative_humidity_2m"),
            "visibility_m":   cur.get("visibility"),
            "wind_kph":       cur.get("wind_speed_10m"),
            "wind_deg":       cur.get("wind_direction_10m"),
            "wind_gusts_kph": cur.get("wind_gusts_10m"),
            "updated":        cur.get("time", ""),
        }
    except Exception as e:
        log.warning(f"Open-Meteo current: {e}")
    return None


def _merge_current(primary: WeatherDict | None, fallback: WeatherDict | None) -> WeatherDict | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    merged = dict(fallback)
    for k, v in primary.items():
        if v is not None:
            merged[k] = v
    return merged


def _format_current(d: WeatherDict | None) -> str | None:
    if d is None:
        return None

    temp_c       = d.get("temp_c")
    feels_c      = d.get("feels_c")
    label        = d.get("feels_label", "Feels like")
    wind_kph_val = d.get("wind_kph")
    wind_deg     = d.get("wind_deg")
    gusts        = d.get("wind_gusts_kph")
    humidity     = d.get("humidity")

    if wind_kph_val is not None and wind_kph_val < 1:
        wind_str = "Calm"
    elif wind_kph_val is not None:
        card     = deg_to_card(wind_deg)  # type: ignore[arg-type]
        wind_str = f"from {card} at {kph(wind_kph_val)}" if card else kph(wind_kph_val)  # type: ignore[arg-type]
        if gusts is not None and gusts > 0 and wind_kph_val > 0 and gusts > wind_kph_val * 1.3:
            wind_str += f" (gusts {kph(gusts)})"  # type: ignore[arg-type]
    else:
        wind_str = "N/A"

    cond = d.get("conditions") or "N/A"
    parts: list[str] = [f"Conditions {cond}", f"Temperature {cf(temp_c)}"]  # type: ignore[arg-type]

    if feels_c is not None and temp_c is not None and abs(feels_c - temp_c) >= 2:  # type: ignore[operator]
        parts.append(f"{label} {cf(feels_c)}")  # type: ignore[arg-type]

    parts += [
        f"Dew point {cf(d.get('dewpoint_c'))}",  # type: ignore[arg-type]
        f"Pressure {mb(d.get('pressure_mb'))}",  # type: ignore[arg-type]
        f"Humidity {f'{humidity:.0f}%' if humidity is not None else 'N/A'}",
        f"Visibility {km_mi(d.get('visibility_m'))}",  # type: ignore[arg-type]
        f"Wind {wind_str}",
        f"Updated {fmt_dt(d.get('updated', ''))}",  # type: ignore[arg-type]
    ]
    return " :: ".join(parts)


async def _om_forecast(lat: float, lon: float) -> str | None:
    try:
        r = await asyncio.to_thread(
            _om_get, _OM_BASE,
            params={
                "latitude": lat, "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "forecast_days": 4,
                "timezone": "auto",
            }, timeout=10,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        dates = daily.get("time", [])
        if not dates:
            return None
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows  = daily.get("temperature_2m_min", [])
        chunks: list[str] = []
        for i in range(min(4, len(dates))):
            try:
                name = datetime.fromisoformat(dates[i]).strftime("%A")
            except Exception:
                name = dates[i]
            code = codes[i] if i < len(codes) else None
            desc = WMO_CODES.get(code, "N/A") if code is not None else "N/A"
            chunks.append(f"{name} {desc} {cf(highs[i] if i < len(highs) else None)} / "
                          f"{cf(lows[i] if i < len(lows) else None)}")
        return " :: ".join(chunks)
    except Exception as e:
        log.warning(f"Open-Meteo forecast: {e}")
    return None


class WeatherModule(BotModule):
    """Weather command handler — routes US queries to NWS, international to Open-Meteo."""
    COMMANDS: dict[str, str] = {
        "weather": "cmd_weather", "w":    "cmd_weather",
        "forecast":"cmd_forecast", "f":   "cmd_forecast",
        "hourly":  "cmd_hourly",   "fh":  "cmd_hourly",
        "alerts":  "cmd_alerts",   "wx":  "cmd_alerts",
        "discuss": "cmd_discuss",  "disc":"cmd_discuss",
    }

    def on_load(self) -> None:
        """Load weather configuration (user agent, cooldown)."""
        ua = self.bot.cfg["weather"]["user_agent"]
        self._headers: dict[str, str] = {"User-Agent": ua, "Accept": "application/geo+json"}
        self._ua       = ua
        self._cooldown = int(self.bot.cfg["bot"]["api_cooldown"])

    def _resolve(self, nick: str, arg: str | None) -> tuple[str | None, str | None]:
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

    async def _geo(self, nick: str, reply_to: str, arg: str | None) -> tuple[float, float, str, str] | None:
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

    def _us_only(self, nick: str, reply_to: str, feature: str) -> None:
        self.bot.privmsg(reply_to, f"{nick}: {feature} requires a US location (NWS).")

    async def cmd_weather(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display current weather conditions for a location."""
        geo = await self._geo(nick, reply_to, arg)
        if geo is None: return
        lat, lon, display, cc = geo
        log.info(f"weather {display!r} ({cc or '?'}) [{lat:.4f},{lon:.4f}]")

        nws_data: WeatherDict | None = None
        if cc == "us":
            grid = await nws.get_grid(lat, lon, self._headers)
            if grid:
                nws_data = await nws.current(lat, lon, grid, self._headers)
            else:
                log.info(f"NWS no grid for {display!r}, falling back to Open-Meteo")

        om_data = await _om_current(lat, lon)
        merged  = _merge_current(nws_data, om_data)
        body    = _format_current(merged)

        if body:
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: weather data unavailable right now.")

    async def cmd_forecast(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display a 4-day forecast for a location."""
        geo = await self._geo(nick, reply_to, arg)
        if geo is None: return
        lat, lon, display, cc = geo
        log.info(f"forecast {display!r} ({cc or '?'}) [{lat:.4f},{lon:.4f}]")
        body: str | None = None
        if cc == "us":
            grid = await nws.get_grid(lat, lon, self._headers)
            if grid:
                body = await nws.forecast(grid, self._headers)
            else:
                log.info(f"NWS no grid for {display!r}, falling back to Open-Meteo")
        if body is None:
            body = await _om_forecast(lat, lon)
        if body:
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: forecast unavailable right now.")

    async def cmd_hourly(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display next 8-hour forecast (US/NWS only)."""
        geo = await self._geo(nick, reply_to, arg)
        if geo is None: return
        lat, lon, display, cc = geo
        if cc != "us": return self._us_only(nick, reply_to, "hourly forecast")
        grid = await nws.get_grid(lat, lon, self._headers)
        if grid is None:
            self.bot.privmsg(reply_to, f"{nick}: weather.gov has no grid data for {display}.")
            return
        body = await nws.hourly(grid, self._headers)
        if body:
            self.bot.privmsg(reply_to, f":: {display} — Next 8 Hours :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: hourly forecast unavailable right now.")

    async def cmd_alerts(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display active NWS alerts (US only)."""
        geo = await self._geo(nick, reply_to, arg)
        if geo is None: return
        lat, lon, display, cc = geo
        if cc != "us": return self._us_only(nick, reply_to, "NWS alerts")
        lines = await nws.alerts(lat, lon, self._headers)
        if lines is None:
            self.bot.privmsg(reply_to, f"{nick}: alerts unavailable right now.")
        elif not lines:
            self.bot.privmsg(reply_to, f":: {display} :: No active NWS alerts.")
        else:
            self.bot.privmsg(reply_to, f":: {display} :: {len(lines)} active alert(s) ::")
            for line in lines:
                self.bot.privmsg(reply_to, line)

    async def cmd_discuss(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display NWS area forecast discussion (US only)."""
        geo = await self._geo(nick, reply_to, arg)
        if geo is None: return
        lat, lon, display, cc = geo
        if cc != "us": return self._us_only(nick, reply_to, "forecast discussion")
        grid = await nws.get_grid(lat, lon, self._headers)
        if grid is None:
            self.bot.privmsg(reply_to, f"{nick}: weather.gov has no grid data for {display}.")
            return
        office = grid.get("cwa", "?")
        paras  = await nws.discussion(grid, self._headers)
        if paras is None:
            self.bot.privmsg(reply_to, f"{nick}: no forecast discussion for {display} ({office}).")
        else:
            self.bot.privmsg(reply_to, f":: {display} :: NWS {office} Forecast Discussion ::")
            for para in paras:
                self.bot.privmsg(reply_to, para)

    def help_lines(self, prefix: str) -> list[str]:
        """Return weather help text."""
        return [
            f"  {prefix}weather/.w  [zip|city|-n nick]   Current conditions (worldwide)",
            f"  {prefix}forecast/.f [zip|city|-n nick]   4-day forecast (worldwide)",
            f"  {prefix}hourly/.fh  [zip|city|-n nick]   Next 8-hour forecast (US/NWS)",
            f"  {prefix}alerts/.wx  [zip|city|-n nick]   Active NWS alerts (US only)",
            f"  {prefix}discuss/.disc [zip|city|-n nick] NWS forecast discussion (US only)",
        ]


def setup(bot: object) -> WeatherModule:
    """Module entry point — returns a WeatherModule instance."""
    return WeatherModule(bot)  # type: ignore[arg-type]
