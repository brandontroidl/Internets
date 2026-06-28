"""NOAA CO-OPS provider package — official US tide predictions.

https://api.tidesandcurrents.noaa.gov/
Tides only.  US locations only (raises so the dispatcher falls through to a
global provider when there's no nearby station).  Free, no API key required.
"""
from __future__ import annotations

from ..base import TideResult  # noqa: F401
from . import tides


class NoaaCoopsProvider:
    name: str = "NOAA CO-OPS"
    requires_key: bool = False

    async def get_tides(self, lat, lon, location, **kw):
        return await tides.fetch(lat, lon, location)
