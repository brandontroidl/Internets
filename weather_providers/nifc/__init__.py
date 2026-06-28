"""NIFC provider package - US active wildfire incidents.

National Interagency Fire Center (WFIGS) current incident locations,
served as a public ArcGIS FeatureServer (no API key).
https://data-nifc.opendata.arcgis.com/
Wildfire only.  US-only data; an empty result near a point is valid
("no fires nearby") and is returned, not raised.
"""
from __future__ import annotations

# Explicit import (no `from ..base import *`) - matches the rest of the package.
from ..base import WildfireResult  # noqa: F401
from . import wildfire


class NIFCProvider:
    name: str = "NIFC"
    requires_key: bool = False

    async def get_wildfire(self, lat, lon, location, **kw):
        return await wildfire.fetch(lat, lon, location)
