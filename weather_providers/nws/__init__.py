"""NWS (Weather.gov) provider package - free, no API key required.

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
from ._scope import none_if_uncovered


class NWSProvider:
    """NWS endpoints, each yielding None for a point NWS does not cover.

    A None result makes the dispatcher fall through to a global provider
    without recording a failure - see ``_scope`` for why that matters.
    """

    name: str = "NWS"
    requires_key: bool = False

    async def get_weather(self, lat, lon, location, **kw):
        return await none_if_uncovered(current.fetch(lat, lon, location))

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await none_if_uncovered(forecast.fetch(lat, lon, location, days))

    async def get_hourly(self, lat, lon, location, hours=12, **kw):
        return await none_if_uncovered(hourly.fetch(lat, lon, location, hours))

    async def get_alerts(self, lat, lon, location, **kw):
        # ``area`` (a USPS state code) widens the query from the geocoded
        # point to the whole state; see alerts.fetch.
        return await none_if_uncovered(
            alerts.fetch(lat, lon, location, area=kw.get("area")))

    async def get_marine(self, lat, lon, location, **kw):
        return await none_if_uncovered(marine.fetch(lat, lon, location))
