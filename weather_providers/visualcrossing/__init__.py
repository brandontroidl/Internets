"""Visual Crossing provider package - requires API key.

https://www.visualcrossing.com/weather-api
Free tier: 1000 calls/day.  Current, forecast, hourly, alerts, historical.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
    HistoricalResult,
)
from . import current, forecast, hourly, alerts, historical


class VisualCrossingProvider:
    name: str = "Visual Crossing"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)

    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._key, lat, lon, location, hours)

    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(self._key, lat, lon, location)

    async def get_historical(self, lat, lon, location, target_date="", **kw):
        return await historical.fetch(self._key, lat, lon, location, target_date)
