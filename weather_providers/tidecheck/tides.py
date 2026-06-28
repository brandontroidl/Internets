"""TideCheck — next high/low tide from the nearest station."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import TideResult

_NEAREST = "https://tidecheck.com/api/stations/nearest"
_TIDES = "https://tidecheck.com/api/station/{id}/tides"


def _station(data):
    """Pull the station dict out of the various shapes the API may return.

    The nearest endpoint may return the station object directly, wrapped
    under a ``station`` key, or as the first element of a list.
    """
    if isinstance(data, dict):
        st = data.get("station")
        if isinstance(st, dict):
            return st
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


async def fetch(key, lat, lon, location):
    headers = {"X-API-Key": key}

    near = await get_json(_NEAREST, params={"lat": lat, "lng": lon},
                          headers=headers)
    st = _station(near)
    sid = st.get("id") if isinstance(st, dict) else None
    if sid in (None, ""):
        raise HTTPError("TideCheck: no tide station near this location",
                        status=None, provider_hint="tidecheck")
    name = (st.get("name") or "").strip() if isinstance(st, dict) else ""

    data = await get_json(_TIDES.format(id=sid), headers=headers)
    extremes = data.get("extremes") if isinstance(data, dict) else None
    if not isinstance(extremes, list):
        extremes = []
    # Station name is sometimes only populated on the tides response.
    if not name:
        tst = data.get("station") if isinstance(data, dict) else None
        if isinstance(tst, dict):
            name = (tst.get("name") or "").strip()

    # TideResult is frozen — accumulate into locals, then build it once.
    high_t = low_t = ""
    high_m = low_m = None
    # Extremes are time-ordered; take the first high and first low we see.
    for e in extremes:
        if not isinstance(e, dict):
            continue
        typ = str(e.get("type", "")).lower()
        t = str(e.get("time") or "")
        h = e.get("height")
        h = float(h) if isinstance(h, (int, float)) else None
        if typ == "high" and not high_t:
            high_t, high_m = t, h
        elif typ == "low" and not low_t:
            low_t, low_m = t, h
        if high_t and low_t:
            break

    return TideResult(
        source="TideCheck", location=location, station=name,
        next_high_time=high_t, next_high_m=high_m,
        next_low_time=low_t, next_low_m=low_m,
    )
