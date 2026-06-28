"""OpenAQ provider package - global open air-quality measurements.

https://docs.openaq.org/  (v3 API; v1/v2 retired)
Air-quality only.  Aggregates raw concentrations from the nearest
monitoring location's sensors and derives a US EPA AQI from PM2.5.
Requires a free API key (header X-API-Key) - register at
https://explore.openaq.org/.  Raises so the dispatcher falls through to
another provider when there's no station within the search radius.
"""
from __future__ import annotations

from ..base import AirQualityResult, aqi_category  # noqa: F401
from . import air_quality


class OpenAQProvider:
    name: str = "OpenAQ"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
