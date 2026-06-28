"""currentuvindex - UV index (current + today's max).

Data from currentuvindex.com (CC-BY).  Keyless, global coverage.
"""
from __future__ import annotations
from datetime import datetime
from .._http import get_json, HTTPError
from ..base import UVResult, uv_category

_BASE = "https://currentuvindex.com/api/v1/uvi"


def _parse_dt(s):
    # ISO 8601, e.g. "2026-06-22T22:00:00Z".
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def fetch(lat, lon, location):
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon})
    if not isinstance(data, dict) or not data.get("ok"):
        raise HTTPError("currentuvindex: no UV data for this location",
                        status=None, provider_hint="currentuvindex")

    now = data.get("now") or {}
    uv_index = now.get("uvi")
    now_dt = _parse_dt(now.get("time"))

    # Today's peak: max forecast uvi on the same calendar date as `now`
    # (fall back to the max of all forecast points if dates don't line up).
    forecast = data.get("forecast")
    forecast = forecast if isinstance(forecast, list) else []
    uvis = []
    same_day = []
    for entry in forecast:
        if not isinstance(entry, dict):
            continue
        u = entry.get("uvi")
        if u is None:
            continue
        uvis.append(u)
        dt = _parse_dt(entry.get("time"))
        if now_dt is not None and dt is not None and dt.date() == now_dt.date():
            same_day.append(u)
    candidates = same_day or uvis
    if uv_index is not None:
        candidates = candidates + [uv_index]
    uv_max = max(candidates) if candidates else None

    return UVResult(source="currentuvindex", location=location,
                    uv_index=uv_index, uv_max=uv_max,
                    category=uv_category(uv_index))
