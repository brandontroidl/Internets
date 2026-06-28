"""Google Pollen API provider package - global pollen, requires a key.

Needs ``[secrets] google_pollen_key`` - a Google Maps Platform API key with
the Pollen API enabled.  Native lat/lon (no reverse-geocode).  Returns the
tree/grass/weed Universal Pollen Index (0-5) for the location.
"""
from __future__ import annotations

from ..base import PollenResult, pollen_cat_5  # noqa: F401
from . import pollen


class GooglePollenProvider:
    name: str = "Google Pollen"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_pollen(self, lat, lon, location, **kw):
        return await pollen.fetch(self._key, lat, lon, location)
