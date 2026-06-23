"""IQAir (AirVisual) provider package — global US-AQI from nearest city.

https://api-docs.iqair.com/
Air-quality only.  Requires a free API key — register at
https://www.iqair.com/air-pollution-data-api.  The free Community tier
exposes /v2/nearest_city, which returns US AQI for the monitoring station
nearest the requested point (no raw µg/m3 concentrations).
"""
from __future__ import annotations

from ..base import AirQualityResult, aqi_category  # noqa: F401
from . import air_quality


class IQAirProvider:
    name: str = "IQAir"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
