"""Open-Meteo - pollen (CAMS; values are non-null over Europe only)."""
from __future__ import annotations
from .._http import get_json
from ..base import PollenResult

_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"
_TAXA = ("alder_pollen", "birch_pollen", "grass_pollen",
         "mugwort_pollen", "olive_pollen", "ragweed_pollen")


async def fetch(lat: float, lon: float, location: str) -> PollenResult | None:
    data = await get_json(_BASE, params={
        "latitude": lat, "longitude": lon,
        "current": ",".join(_TAXA), "timezone": "auto",
    })
    c = data.get("current", {}) or {}
    vals = {
        "alder": c.get("alder_pollen"), "birch": c.get("birch_pollen"),
        "grass": c.get("grass_pollen"), "mugwort": c.get("mugwort_pollen"),
        "olive": c.get("olive_pollen"), "ragweed": c.get("ragweed_pollen"),
    }
    if all(v is None for v in vals.values()):
        # CAMS is Europe-only - no data here.  Return None so the dispatcher
        # falls through to another pollen provider (Pollen.com / Google).
        return None
    return PollenResult(source="Open-Meteo", location=location, **vals)
