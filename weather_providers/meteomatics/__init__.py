"""Meteomatics provider package — requires username + password.
https://www.meteomatics.com/en/api/
Free tier: limited, professional weather data.
"""
from __future__ import annotations
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
)
from . import current, forecast, hourly

class MeteomaticsProvider:
    name: str = "Meteomatics"
    requires_key: bool = True
    def __init__(self, username: str, password: str) -> None:
        self._user = username; self._pw = password
    def _auth(self): import base64; return {"Authorization": "Basic " + base64.b64encode(f"{self._user}:{self._pw}".encode()).decode()}
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._auth(), lat, lon, location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._auth(), lat, lon, location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._auth(), lat, lon, location, hours)
