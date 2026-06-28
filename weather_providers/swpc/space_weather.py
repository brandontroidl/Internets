"""NOAA SWPC — geomagnetic activity (planetary Kp) and aurora probability."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import SpaceWeatherResult, kp_category

# 1-minute planetary K index: array of {"time_tag","kp_index","estimated_kp",...}
_KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
# OVATION aurora nowcast: {"coordinates": [[lon(0-359), lat(-90..90), pct], ...]}
_AURORA_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"


def _latest_kp(data) -> float | None:
    """Float Kp from the last entry; prefer estimated_kp, fall back to kp_index."""
    if not isinstance(data, list) or not data:
        return None
    last = data[-1]
    if not isinstance(last, dict):
        return None
    val = last.get("estimated_kp")
    if val is None:
        val = last.get("kp_index")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _aurora_pct(data, lat, lon) -> float | None:
    """Aurora percent at the grid point nearest the (rounded) user lat/lon."""
    coords = data.get("coordinates") if isinstance(data, dict) else None
    if not isinstance(coords, list) or not coords:
        return None
    target_lon = round(lon) % 360          # grid longitude is 0..359
    target_lat = round(lat)                # grid latitude is -90..90
    best = None
    best_dist = None
    for entry in coords:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        g_lon, g_lat, pct = entry[0], entry[1], entry[2]
        # squared planar distance on the integer grid — fine for nearest-cell
        dist = (g_lon - target_lon) ** 2 + (g_lat - target_lat) ** 2
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, pct
    try:
        return float(best) if best is not None else None
    except (TypeError, ValueError):
        return None


async def fetch(lat, lon, location):
    # Kp is small and may legitimately be missing; aurora is the larger grid.
    kp = None
    kp_failed = False
    try:
        kp = _latest_kp(await get_json(_KP_URL))
    except HTTPError:
        kp_failed = True

    try:
        aurora = await get_json(_AURORA_URL, max_bytes=2_000_000)
    except HTTPError:
        # If both products failed there's nothing to report — fall through.
        if kp_failed:
            raise HTTPError("NOAA SWPC: space-weather data unavailable",
                            status=None, provider_hint="swpc")
        aurora = None

    aurora_pct = _aurora_pct(aurora, lat, lon) if aurora is not None else None
    return SpaceWeatherResult(source="NOAA SWPC", location=location,
                              kp_index=kp, kp_category=kp_category(kp),
                              aurora_pct=aurora_pct)
