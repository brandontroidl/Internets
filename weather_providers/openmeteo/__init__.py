"""Open-Meteo weather provider package — free, no API key required."""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AirQualityResult, aqi_category,
    AstronomyResult,
    HistoricalResult,
    MarineResult,
)
from . import current, forecast, hourly, air_quality, astronomy, historical, marine

class OpenMeteoProvider:
    name: str = "Open-Meteo"
    requires_key: bool = False

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(lat, lon, location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(lat, lon, location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(lat, lon, location, hours)
    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(lat, lon, location)
    async def get_astronomy(self, lat, lon, location, **kw):
        return await astronomy.fetch(lat, lon, location)
    async def get_historical(self, lat, lon, location, target_date="", **kw):
        return await historical.fetch(lat, lon, location, target_date)
    async def get_marine(self, lat, lon, location, **kw):
        return await marine.fetch(lat, lon, location)
