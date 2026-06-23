"""NASA POWER provider package — free, no API key required.

https://power.larc.nasa.gov/docs/services/api/temporal/daily/
Historical daily weather only.  Global coverage, but the dataset lags
real-time by a few days, so an empty target_date defaults to ~7 days ago.
"""
from __future__ import annotations

from ..base import HistoricalResult  # noqa: F401
from . import historical


class NasaPowerProvider:
    name: str = "NASA POWER"
    requires_key: bool = False

    async def get_historical(self, lat, lon, location, target_date="", **kw):
        return await historical.fetch(lat, lon, location, target_date)
