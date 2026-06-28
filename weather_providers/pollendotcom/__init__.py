"""Pollen.com (IQVIA) provider package - US allergy index.

No API key.  Uses an unofficial public endpoint, and reverse-geocodes
lat/lon → US ZIP via Nominatim first, so the factory passes the configured
``[secrets] weather_user_agent`` for the Nominatim request.  US coverage
only - returns ``None`` for non-US locations so the dispatcher falls through
to another pollen provider.
"""
from __future__ import annotations

from ..base import PollenResult, pollen_cat_12  # noqa: F401
from . import pollen


class PollenDotComProvider:
    name: str = "Pollen.com"
    requires_key: bool = False

    def __init__(self, user_agent: str) -> None:
        self._ua = user_agent

    async def get_pollen(self, lat, lon, location, **kw):
        return await pollen.fetch(self._ua, lat, lon, location)
