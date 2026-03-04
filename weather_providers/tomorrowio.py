"""Tomorrow.io weather provider — requires an API key.

https://docs.tomorrow.io/reference/welcome
Free tier: 500 calls/day, current + 5-day forecast.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .base  import WeatherResult, ForecastDay
from ._http import get_json

log = logging.getLogger("internets.weather.tomorrowio")

_BASE = "https://api.tomorrow.io/v4"

_WEATHER_CODES: dict[int, str] = {
    0: "Unknown",
    1000: "Clear", 1100: "Mostly Clear", 1101: "Partly Cloudy",
    1102: "Mostly Cloudy", 1001: "Cloudy",
    2000: "Fog", 2100: "Light Fog",
    3000: "Light Wind", 3001: "Wind", 3002: "Strong Wind",
    4000: "Drizzle", 4001: "Rain", 4200: "Light Rain", 4201: "Heavy Rain",
    5000: "Snow", 5001: "Flurries", 5100: "Light Snow", 5101: "Heavy Snow",
    6000: "Freezing Drizzle", 6001: "Freezing Rain",
    6200: "Light Freezing Rain", 6201: "Heavy Freezing Rain",
    7000: "Ice Pellets", 7101: "Heavy Ice Pellets", 7102: "Light Ice Pellets",
    8000: "Thunderstorm",
}

_WIND_DIRS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_card(deg: float | None) -> str:
    if deg is None:
        return ""
    return _WIND_DIRS[round(deg / 22.5) % 16]


class TomorrowIOProvider:
    """Tomorrow.io — global weather with free tier.  Requires API key."""

    name: str = "Tomorrow.io"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(
        self, lat: float, lon: float, location: str, **kwargs: object
    ) -> WeatherResult:
        """Fetch current conditions from Tomorrow.io."""
        data = await get_json(f"{_BASE}/weather/realtime", params={
            "apikey": self._key,
            "location": f"{lat},{lon}",
            "units": "metric",
        })
        vals = data.get("data", {}).get("values", {})

        wcode = vals.get("weatherCode")
        desc  = _WEATHER_CODES.get(wcode, f"Code {wcode}") if wcode is not None else "Unknown"

        return WeatherResult(
            source=self.name,
            temperature=vals.get("temperature"),
            description=desc,
            location=location,
            feels_like_c=vals.get("temperatureApparent"),
            humidity=vals.get("humidity"),
            wind_kph=(vals["windSpeed"] * 3.6) if vals.get("windSpeed") is not None else None,
            wind_dir=_deg_to_card(vals.get("windDirection")),
            pressure_mb=vals.get("pressureSurfaceLevel"),
            visibility_m=(vals["visibility"] * 1000) if vals.get("visibility") is not None else None,
            dewpoint_c=vals.get("dewPoint"),
        )

    async def get_forecast(
        self, lat: float, lon: float, location: str,
        days: int = 4, **kwargs: object
    ) -> WeatherResult:
        """Fetch multi-day forecast from Tomorrow.io."""
        data = await get_json(f"{_BASE}/weather/forecast", params={
            "apikey": self._key,
            "location": f"{lat},{lon}",
            "units": "metric",
            "timesteps": "1d",
        })
        timelines = data.get("timelines", {}).get("daily", [])

        fc: list[ForecastDay] = []
        for entry in timelines[:days]:
            vals = entry.get("values", {})
            time_str = entry.get("time", "")
            try:
                day_name = datetime.fromisoformat(
                    time_str.replace("Z", "+00:00")
                ).strftime("%A")
            except Exception:
                day_name = time_str[:10]

            wcode = vals.get("weatherCodeMax") or vals.get("weatherCode")
            desc  = _WEATHER_CODES.get(wcode, "N/A") if wcode is not None else "N/A"

            fc.append(ForecastDay(
                day_name=day_name,
                high_c=vals.get("temperatureMax"),
                low_c=vals.get("temperatureMin"),
                description=desc,
            ))

        # Current conditions from first timeline entry or separate call.
        cur_vals = timelines[0].get("values", {}) if timelines else {}
        cur_code = cur_vals.get("weatherCodeMax")
        cur_desc = _WEATHER_CODES.get(cur_code, "N/A") if cur_code else "N/A"

        return WeatherResult(
            source=self.name,
            temperature=cur_vals.get("temperatureAvg"),
            description=cur_desc,
            location=location,
            forecast=fc,
        )
