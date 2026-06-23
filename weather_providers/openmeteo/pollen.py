"""Open-Meteo — pollen (CAMS; values are non-null over Europe only)."""
from __future__ import annotations
from .._http import get_json
from ..base import PollenResult

_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"
_TAXA = ("alder_pollen", "birch_pollen", "grass_pollen",
         "mugwort_pollen", "olive_pollen", "ragweed_pollen")


async def fetch(lat: float, lon: float, location: str) -> PollenResult:
    data = await get_json(_BASE, params={
        "latitude": lat, "longitude": lon,
        "current": ",".join(_TAXA), "timezone": "auto",
    })
    c = data.get("current", {}) or {}
    return PollenResult(
        source="Open-Meteo", location=location,
        alder=c.get("alder_pollen"), birch=c.get("birch_pollen"),
        grass=c.get("grass_pollen"), mugwort=c.get("mugwort_pollen"),
        olive=c.get("olive_pollen"), ragweed=c.get("ragweed_pollen"),
    )
