"""AirNow provider package - US EPA official Air Quality Index.

https://docs.airnowapi.org/
Air-quality only.  US locations only (raises so the dispatcher falls
through to a global provider when there's no coverage).  Requires a free
API key - register at https://docs.airnowapi.org/account/request/.
Free tier: 500 requests/hour per key.
"""
from __future__ import annotations

# fix: explicit imports (no `from ..base import *`) - matches the rest of
# the package.
from ..base import AirQualityResult, aqi_category  # noqa: F401
from . import air_quality


class AirNowProvider:
    name: str = "AirNow"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
