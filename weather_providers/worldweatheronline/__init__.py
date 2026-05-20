"""World Weather Online provider package — requires API key.

https://www.worldweatheronline.com/weather-api/
Free tier: 500 calls/day.  Current, forecast, hourly, astronomy, historical, marine.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AstronomyResult,
    HistoricalResult,
    MarineResult,
)
from . import current, forecast, hourly, astronomy, historical, marine


class WorldWeatherOnlineProvider:
    name: str = "World Weather Online"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)

    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._key, lat, lon, location, hours)

    async def get_astronomy(self, lat, lon, location, **kw):
        return await astronomy.fetch(self._key, lat, lon, location)

    async def get_historical(self, lat, lon, location, target_date="", **kw):
        return await historical.fetch(self._key, lat, lon, location, target_date)

    async def get_marine(self, lat, lon, location, **kw):
        return await marine.fetch(self._key, lat, lon, location)
