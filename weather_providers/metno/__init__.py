"""MET Norway (api.met.no) weather provider package - free, no API key."""
from __future__ import annotations
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
    NowcastResult, NowcastEntry,
)  # noqa: F401
from . import current, forecast, hourly, alerts, nowcast

class MetNoProvider:
    name: str = "MET Norway"
    requires_key: bool = False

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(lat, lon, location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(lat, lon, location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(lat, lon, location, hours)
    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(lat, lon, location)
    async def get_nowcast(self, lat, lon, location, **kw):
        return await nowcast.fetch(lat, lon, location)
