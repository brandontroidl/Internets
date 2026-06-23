"""PurpleAir provider package — crowdsourced real-time PM2.5 sensors.

https://api.purpleair.com/
Air-quality only.  Global coverage from low-cost community sensors.
Requires a free READ key — request at https://develop.purpleair.com/.
Crowdsourced readings are noisier than regulatory monitors, so PurpleAir
is ranked below the model/observation-based providers in the air_quality
chain and its readings carry an EPA humidity correction.
"""
from __future__ import annotations

from ..base import AirQualityResult, aqi_category  # noqa: F401
from . import air_quality


class PurpleAirProvider:
    name: str = "PurpleAir"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_air_quality(self, lat, lon, location, **kw):
        return await air_quality.fetch(self._key, lat, lon, location)
