"""NOAA SWPC provider package - space-weather (geomagnetic activity, aurora).

https://services.swpc.noaa.gov/

Space-weather only.  No API key required.  Two public JSON products:
the 1-minute planetary K index and the latest OVATION aurora nowcast
(a 1-degree global probability grid).  Global coverage.
"""
from __future__ import annotations

from ..base import SpaceWeatherResult, kp_category  # noqa: F401
from . import space_weather


class SWPCProvider:
    name: str = "NOAA SWPC"
    requires_key: bool = False

    async def get_space_weather(self, lat, lon, location, **kw):
        return await space_weather.fetch(lat, lon, location)
