"""Open-Meteo - UV index (current + today's max)."""
from __future__ import annotations
from .._http import get_json
from ..base import UVResult, uv_category

_BASE = "https://api.open-meteo.com/v1/forecast"


async def fetch(lat: float, lon: float, location: str) -> UVResult:
    data = await get_json(_BASE, params={
        "latitude": lat, "longitude": lon,
        "current": "uv_index", "daily": "uv_index_max",
        "forecast_days": 1, "timezone": "auto",
    })
    uv = (data.get("current") or {}).get("uv_index")
    maxes = (data.get("daily") or {}).get("uv_index_max") or []
    uv_max = maxes[0] if maxes else None
    return UVResult(source="Open-Meteo", location=location,
                    uv_index=uv, uv_max=uv_max, category=uv_category(uv))
