"""NWS (Weather.gov) provider package — free, no API key required.

https://api.weather.gov/
US locations only.  Unlimited calls.  Most authoritative source for US alerts.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
    MarineResult,
)
from . import current, forecast, hourly, alerts, marine


class NWSProvider:
    name: str = "NWS"
    requires_key: bool = False

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(lat, lon, location, days)

    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(lat, lon, location, hours)

    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(lat, lon, location)

    async def get_marine(self, lat, lon, location, **kw):
        return await marine.fetch(lat, lon, location)
