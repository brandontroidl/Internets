"""TideCheck provider package - tide predictions for coastal stations.

https://tidecheck.com/developers
Tides only.  Resolves the nearest station to the point, then reads its
upcoming high/low extremes.  Requires an API key (sent as the
``X-API-Key`` header).  Raises so the dispatcher falls through when no
station can be resolved for the location (e.g. far inland).
"""
from __future__ import annotations

from ..base import TideResult  # noqa: F401
from . import tides


class TideCheckProvider:
    name: str = "TideCheck"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_tides(self, lat, lon, location, **kw):
        return await tides.fetch(self._key, lat, lon, location)
