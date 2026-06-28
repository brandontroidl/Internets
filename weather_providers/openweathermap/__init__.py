"""OpenWeatherMap provider package - requires API key.
https://openweathermap.org/api
Free tier: 60 calls/min, current + 5-day/3-hour forecast.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
    AirQualityResult, aqi_category,
)
from . import current, forecast, hourly, alerts, air_quality

class OpenWeatherMapProvider:
    name: str = "OpenWeatherMap"
    requires_key: bool = True
    def __init__(self, api_key: str) -> None: self._key = api_key
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._key, lat, lon, location, hours)
    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(self._key, lat, lon, location)
    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
