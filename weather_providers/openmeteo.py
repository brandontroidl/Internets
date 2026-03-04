"""Open-Meteo weather provider — free, no API key required.

https://open-meteo.com/en/docs
"""

from __future__ import annotations

import logging
from datetime import datetime

from .base  import WeatherResult, ForecastDay
from ._http import get_json

log = logging.getLogger("internets.weather.openmeteo")

_BASE = "https://api.open-meteo.com/v1/forecast"

_CURRENT_FIELDS = ",".join([
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

_WIND_DIRS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_card(deg: float | None) -> str:
    """Convert wind direction degrees to a cardinal abbreviation."""
    if deg is None:
        return ""
    return _WIND_DIRS[round(deg / 22.5) % 16]


class OpenMeteoProvider:
    """Open-Meteo — free worldwide weather, no API key needed."""

    name: str = "Open-Meteo"
    requires_key: bool = False

    async def get_weather(
        self, lat: float, lon: float, location: str, **kwargs: object
    ) -> WeatherResult:
        """Fetch current conditions from Open-Meteo."""
        data = await get_json(_BASE, params={
            "latitude": lat, "longitude": lon,
            "current": _CURRENT_FIELDS,
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        })
        # SEC-WP-010: Defensive access — malformed response won't KeyError.
        cur = data.get("current")
        if not isinstance(cur, dict):
            raise ValueError("Open-Meteo response missing 'current' object")

        wcode = cur.get("weather_code")
        desc  = WMO_CODES.get(wcode, f"Code {wcode}") if wcode is not None else "Unknown"

        return WeatherResult(
            source=self.name,
            temperature=cur.get("temperature_2m"),
            description=desc,
            location=location,
            feels_like_c=cur.get("apparent_temperature"),
            humidity=cur.get("relative_humidity_2m"),
            wind_kph=cur.get("wind_speed_10m"),
            wind_dir=_deg_to_card(cur.get("wind_direction_10m")),
            pressure_mb=cur.get("surface_pressure"),
            visibility_m=cur.get("visibility"),
            dewpoint_c=cur.get("dew_point_2m"),
        )

    async def get_forecast(
        self, lat: float, lon: float, location: str,
        days: int = 4, **kwargs: object
    ) -> WeatherResult:
        """Fetch multi-day forecast from Open-Meteo."""
        data = await get_json(_BASE, params={
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "current": "temperature_2m,weather_code",
            "forecast_days": min(days, 16),
            "timezone": "auto",
        })
        cur   = data.get("current", {})
        daily = data.get("daily", {})

        wcode = cur.get("weather_code")
        desc  = WMO_CODES.get(wcode, "N/A") if wcode is not None else "N/A"

        dates  = daily.get("time", [])
        codes  = daily.get("weather_code", [])
        highs  = daily.get("temperature_2m_max", [])
        lows   = daily.get("temperature_2m_min", [])

        fc: list[ForecastDay] = []
        for i in range(min(days, len(dates))):
            try:
                day_name = datetime.fromisoformat(dates[i]).strftime("%A")
            except Exception:
                day_name = dates[i]
            code = codes[i] if i < len(codes) else None
            fc.append(ForecastDay(
                day_name=day_name,
                high_c=highs[i] if i < len(highs) else None,
                low_c=lows[i] if i < len(lows) else None,
                description=WMO_CODES.get(code, "N/A") if code is not None else "N/A",
            ))

        return WeatherResult(
            source=self.name,
            temperature=cur.get("temperature_2m"),
            description=desc,
            location=location,
            forecast=fc,
        )
