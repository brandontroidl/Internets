"""NOAA CO-OPS — next high/low tide from the nearest prediction station."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import TideResult, haversine_km as _haversine_km

_STATIONS = ("https://api.tidesandcurrents.noaa.gov"
             "/mdapi/prod/webapi/stations.json")
_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
_MAX_KM = 150.0  # beyond this there's no real US coverage — fall back


async def _water_temp(station):
    """Best-effort latest water temperature (°C); None on any failure."""
    try:
        data = await get_json(_DATA, params={
            "product": "water_temperature",
            "units": "metric",
            "time_zone": "lst_ldt",
            "format": "json",
            "station": station,
            "date": "latest",
        })
    except HTTPError:
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    try:
        return round(float(rows[0].get("v")), 1)
    except (TypeError, ValueError):
        return None


async def fetch(lat, lon, location):
    # Step 1 — resolve the nearest tide-prediction station.  The full station
    # list is large (~3500 stations), so lift the response cap.
    sdata = await get_json(
        _STATIONS,
        params={"type": "tidepredictions"},
        max_bytes=8_000_000,
    )
    stations = sdata.get("stations") if isinstance(sdata, dict) else None
    if not stations:
        raise HTTPError("NOAA CO-OPS: no station list",
                        status=None, provider_hint="noaa_coops")
    best = None
    best_km = None
    for st in stations:
        if not isinstance(st, dict):
            continue
        slat, slon = st.get("lat"), st.get("lng")
        if slat is None or slon is None:
            continue
        try:
            d = _haversine_km(lat, lon, float(slat), float(slon))
        except (TypeError, ValueError):
            continue
        if best_km is None or d < best_km:
            best, best_km = st, d
    if best is None:
        raise HTTPError("NOAA CO-OPS: no usable station",
                        status=None, provider_hint="noaa_coops")
    if best_km > _MAX_KM:
        # Nearest station is too far — almost certainly outside US coverage.
        raise HTTPError("NOAA CO-OPS: no tide coverage for this location",
                        status=None, provider_hint="noaa_coops")

    sid = str(best.get("id"))
    name = best.get("name") or sid

    # Step 2 — today's high/low predictions for that station.
    pdata = await get_json(_DATA, params={
        "product": "predictions",
        "interval": "hilo",
        "datum": "MLLW",
        "units": "metric",
        "time_zone": "lst_ldt",
        "format": "json",
        "station": sid,
        "date": "today",
    })
    preds = pdata.get("predictions") if isinstance(pdata, dict) else None
    if not preds:
        raise HTTPError("NOAA CO-OPS: no predictions for station",
                        status=None, provider_hint="noaa_coops")

    # Take the first high ("H") and first low ("L") of the day.
    high_t = high_v = low_t = low_v = None
    for p in preds:
        if not isinstance(p, dict):
            continue
        typ = (p.get("type") or "").upper()
        if typ == "H" and high_t is None:
            high_t, high_v = p.get("t", ""), p.get("v")
        elif typ == "L" and low_t is None:
            low_t, low_v = p.get("t", ""), p.get("v")
        if high_t is not None and low_t is not None:
            break

    def _m(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    water_c = await _water_temp(sid)

    return TideResult(
        source="NOAA CO-OPS",
        location=location,
        station=name,
        next_high_time=high_t or "",
        next_high_m=_m(high_v),
        next_low_time=low_t or "",
        next_low_m=_m(low_v),
        water_temp_c=water_c,
    )
