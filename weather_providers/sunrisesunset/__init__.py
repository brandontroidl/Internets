"""SunriseSunset provider package - sun/moon astronomy.

https://sunrisesunset.io/api/
Astronomy only.  No API key required.  Global coverage.
"""
from __future__ import annotations

from ..base import AstronomyResult  # noqa: F401
from . import astronomy


class SunriseSunsetProvider:
    name: str = "SunriseSunset"
    requires_key: bool = False

    async def get_astronomy(self, lat, lon, location, **kw):
        return await astronomy.fetch(lat, lon, location)
