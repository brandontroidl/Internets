"""AccuWeather provider package — requires API key.
https://developer.accuweather.com/apis
Free tier: 50 calls/day. Requires location key lookup.

NOTE: AccuWeather supports HTTPS on all tiers; we use https:// uniformly
to avoid leaking the apikey query parameter on the wire. If the free
tier ever rejects TLS for a given endpoint the dispatcher will surface
a 400/403 — switch to ``http://`` only as a documented downgrade.
"""
from __future__ import annotations
import logging
from collections import OrderedDict
# fix: replaced "from ..base import *" with explicit imports for clarity
from ..base import (
    WeatherResult, ForecastDay,
    HourlyResult, HourlyEntry,
    AlertsResult, AlertEntry,
)
from .._http import get_json
from . import current, forecast, hourly, alerts

log = logging.getLogger("internets.weather.accuweather")

# fix: was an unbounded dict — under attack or just long-running this
# grows without limit. Bounded LRU (move-to-end on hit, evict-oldest on
# overflow). ~2 KB ceiling at LRU_MAX = 512 entries.
_LRU_MAX = 512
_LOC_CACHE: "OrderedDict[str, str]" = OrderedDict()

async def _get_location_key(key: str, lat: float, lon: float) -> str:
    cache_key = f"{lat:.2f},{lon:.2f}"
    if cache_key in _LOC_CACHE:
        _LOC_CACHE.move_to_end(cache_key)
        return _LOC_CACHE[cache_key]
    # fix: was http:// — leaked apikey in plaintext query string.
    data = await get_json(
        "https://dataservice.accuweather.com/locations/v1/cities/geoposition/search",
        params={"apikey": key, "q": f"{lat},{lon}"},
    )
    loc_key = data.get("Key", "")
    if loc_key:
        _LOC_CACHE[cache_key] = loc_key
        _LOC_CACHE.move_to_end(cache_key)
        while len(_LOC_CACHE) > _LRU_MAX:
            _LOC_CACHE.popitem(last=False)
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
