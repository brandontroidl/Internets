"""Stormglass.io provider package - marine weather specialist.
https://docs.stormglass.io/
Free tier: 10 requests/day. Best-in-class marine data.
Wave height, swell, water temperature, tides.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult,
    HourlyResult, HourlyEntry,
    MarineResult,
)
from . import current, marine, hourly

class StormglassProvider:
    name: str = "Stormglass"
    requires_key: bool = True
    def __init__(self, api_key: str) -> None: self._key = api_key
    def _headers(self): return {"Authorization": self._key}
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._headers(), lat, lon, location)
    async def get_marine(self, lat, lon, location, **kw):
        return await marine.fetch(self._headers(), lat, lon, location)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._headers(), lat, lon, location, hours)
