"""Pirate Weather provider package - Dark Sky API replacement.

https://pirateweather.net/
Free tier: 20,000 calls/month.  Current, forecast, hourly, alerts, minutely nowcast.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
    NowcastResult, NowcastEntry,
)
from . import current, forecast, hourly, alerts, nowcast


class PirateWeatherProvider:
    name: str = "Pirate Weather"
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

    async def get_nowcast(self, lat, lon, location, **kw):
        return await nowcast.fetch(self._key, lat, lon, location)
