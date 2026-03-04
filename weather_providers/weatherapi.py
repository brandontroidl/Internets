"""WeatherAPI.com provider — requires a free or paid API key.

https://www.weatherapi.com/docs/
Free tier: 1M calls/month, current + 3-day forecast.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .base  import WeatherResult, ForecastDay
from ._http import get_json

log = logging.getLogger("internets.weather.weatherapi")

_BASE = "https://api.weatherapi.com/v1"

_WIND_DIRS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_card(deg: float | None) -> str:
    if deg is None:
        return ""
    return _WIND_DIRS[round(deg / 22.5) % 16]


class WeatherAPIProvider:
    """WeatherAPI.com — global weather with free tier.  Requires API key."""

    name: str = "WeatherAPI"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(
        self, lat: float, lon: float, location: str, **kwargs: object
    ) -> WeatherResult:
        """Fetch current conditions from WeatherAPI.com."""
        data = await get_json(f"{_BASE}/current.json", params={
            "key": self._key,
            "q": f"{lat},{lon}",
            "aqi": "no",
        })
        # SEC-WP-010: Defensive access — malformed response won't KeyError.
        cur = data.get("current")
        if not isinstance(cur, dict):
            raise ValueError("WeatherAPI response missing 'current' object")

        return WeatherResult(
            source=self.name,
            temperature=cur.get("temp_c"),
            description=cur.get("condition", {}).get("text", "Unknown"),
            location=location,
            feels_like_c=cur.get("feelslike_c"),
            humidity=cur.get("humidity"),
            wind_kph=cur.get("wind_kph"),
            wind_dir=cur.get("wind_dir", ""),
            pressure_mb=cur.get("pressure_mb"),
            visibility_m=(cur["vis_km"] * 1000) if cur.get("vis_km") is not None else None,
            dewpoint_c=cur.get("dewpoint_c"),
        )

    async def get_forecast(
        self, lat: float, lon: float, location: str,
        days: int = 4, **kwargs: object
    ) -> WeatherResult:
        """Fetch multi-day forecast from WeatherAPI.com."""
        # Free tier caps at 3 days; paid supports up to 14.
        data = await get_json(f"{_BASE}/forecast.json", params={
            "key": self._key,
            "q": f"{lat},{lon}",
            "days": min(days, 14),
            "aqi": "no",
            "alerts": "no",
        })
        cur = data.get("current", {})
        forecast_days = data.get("forecast", {}).get("forecastday", [])

        fc: list[ForecastDay] = []
        for fd in forecast_days[:days]:
            day = fd.get("day", {})
            # Parse date to day name.
            date_str = fd.get("date", "")
            try:
                day_name = datetime.fromisoformat(date_str).strftime("%A")
            except Exception:
                day_name = date_str
            fc.append(ForecastDay(
                day_name=day_name,
                high_c=day.get("maxtemp_c"),
                low_c=day.get("mintemp_c"),
                description=day.get("condition", {}).get("text", "N/A"),
            ))

        return WeatherResult(
            source=self.name,
            temperature=cur.get("temp_c"),
            description=cur.get("condition", {}).get("text", "N/A"),
            location=location,
            forecast=fc,
        )
