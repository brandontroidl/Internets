"""GDACS provider package - Global Disaster Alert and Coordination System.

https://www.gdacs.org/  (joint UN / European Commission)
Disaster alerts only (earthquakes, tropical cyclones, floods, droughts,
volcanoes, wildfires).  No API key.  Worldwide coverage; "no events
nearby" is valid data (returns an empty AlertsResult, never raises).
"""
from __future__ import annotations

from ..base import AlertsResult, AlertEntry  # noqa: F401
from . import alerts


class GdacsProvider:
    name: str = "GDACS"
    requires_key: bool = False

    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(lat, lon, location)
