"""ECCC provider package - Environment and Climate Change Canada alerts.

https://api.weather.gc.ca/  (OGC API Features, GeoJSON; no key required).
Alerts only.  Canada-only coverage - outside Canada the bbox query simply
returns zero features, which is valid "no active alerts" data (we do NOT
raise; the dispatcher only falls through on real errors).
"""
from __future__ import annotations

from ..base import AlertsResult, AlertEntry  # noqa: F401
from . import alerts


class ECCCProvider:
    name: str = "ECCC"
    requires_key: bool = False

    async def get_alerts(self, lat, lon, location, **kw):
        return await alerts.fetch(lat, lon, location)
