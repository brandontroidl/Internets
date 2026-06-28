"""currentuvindex provider package — UV index now + today's peak.

https://currentuvindex.com/  — free, keyless API.  Data licensed CC-BY;
credit currentuvindex.com.  UV only, global coverage.
"""
from __future__ import annotations

from ..base import UVResult, uv_category  # noqa: F401
from . import uv


class CurrentUVIndexProvider:
    name: str = "currentuvindex"
    requires_key: bool = False

    async def get_uv(self, lat, lon, location, **kw):
        return await uv.fetch(lat, lon, location)
