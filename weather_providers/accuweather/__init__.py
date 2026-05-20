"""AccuWeather provider package — requires API key.
https://developer.accuweather.com/apis
Free tier: 50 calls/day. Requires location key lookup.
"""
from __future__ import annotations
import logging
from ..base import *
from .._http import get_json
from . import current, forecast, hourly, alerts

log = logging.getLogger("internets.weather.accuweather")
_LOC_CACHE: dict[str, str] = {}

async def _get_location_key(key: str, lat: float, lon: float) -> str:
    cache_key = f"{lat:.2f},{lon:.2f}"
    if cache_key in _LOC_CACHE: return _LOC_CACHE[cache_key]
    data = await get_json("http://dataservice.accuweather.com/locations/v1/cities/geoposition/search", params={"apikey": key, "q": f"{lat},{lon}"})
    loc_key = data.get("Key", "")
    if loc_key: _LOC_CACHE[cache_key] = loc_key
    return loc_key

class AccuWeatherProvider:
    name: str = "AccuWeather"
    requires_key: bool = True
    def __init__(self, api_key: str) -> None: self._key = api_key
    async def _lk(self, lat, lon): return await _get_location_key(self._key, lat, lon)
    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, await self._lk(lat,lon), location)
    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, await self._lk(lat,lon), location, days)
    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await hourly.fetch(self._key, await self._lk(lat,lon), location, hours)
    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(self._key, await self._lk(lat,lon), location)
