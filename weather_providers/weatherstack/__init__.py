"""Weatherstack provider package — requires API key.
https://weatherstack.com/documentation
Free tier: 250 calls/month, current only. Paid adds forecast + historical.

NOTE: Weatherstack's free tier historically required HTTP. As of 2024
HTTPS is available on all paid plans and most free-tier endpoints.
If the free tier rejects a request the dispatcher will see a 4xx with
a "https_access_restricted" error code in the JSON envelope; callers
should upgrade or switch providers.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HistoricalResult,
)
from . import current, forecast, historical

class WeatherstackProvider:
    name: str = "Weatherstack"
    requires_key: bool = True
    def __init__(self, api_key: str) -> None: self._key = api_key
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)
    async def get_historical(self, lat, lon, location, target_date="", **kw):
        return await historical.fetch(self._key, lat, lon, location, target_date)
