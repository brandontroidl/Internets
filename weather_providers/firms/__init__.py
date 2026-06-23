"""NASA FIRMS provider package — active wildfire detections (global).

Fire Information for Resource Management System.  Satellite thermal-anomaly
detections (VIIRS S-NPP, near-real-time).  Global coverage.  Requires a free
MAP_KEY in the URL path — register at https://firms.modaps.eosdis.nasa.gov/api/.

These are point detections, not mapped incidents: we get a count of nearby
detections and the distance to the nearest one, but no incident names or
acreage (those fields stay empty/None).
"""
from __future__ import annotations

from ..base import WildfireResult  # noqa: F401
from . import wildfire


class FirmsProvider:
    name: str = "NASA FIRMS"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_wildfire(self, lat, lon, location, **kw):
        return await wildfire.fetch(self._key, lat, lon, location)
