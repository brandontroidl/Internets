"""WAQI provider package — World Air Quality Index (aqicn.org).

https://aqicn.org/api/
Air-quality only.  Global coverage via the nearest reporting station.
Requires a free token — register at https://aqicn.org/data-platform/token/.
"""
from __future__ import annotations

from ..base import AirQualityResult, aqi_category  # noqa: F401
from . import air_quality


class WAQIProvider:
    name: str = "WAQI"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
