"""Google Pollen API — global tree/grass/weed pollen (0-5 UPI).

https://developers.google.com/maps/documentation/pollen/forecast
Returns ``None`` when the API has no pollen data for the location so the
dispatcher can fall through to another provider.
"""
from __future__ import annotations

from .._http import get_json
from ..base import PollenResult, pollen_cat_5  # noqa: F401

_API = "https://pollen.googleapis.com/v1/forecast:lookup"


async def fetch(key, lat, lon, location):
    data = await get_json(_API, params={
        "key": key,
        "location.latitude": lat,
        "location.longitude": lon,
        "days": 1,
        "plantsDescription": "false",
    })
    daily = (data.get("dailyInfo") or []) if isinstance(data, dict) else []
    if not daily:
        return None
    by_code: dict[str, float] = {}
    for pt in daily[0].get("pollenTypeInfo") or []:
        if not isinstance(pt, dict):
            continue
        code = pt.get("code")                    # GRASS / TREE / WEED
        val = (pt.get("indexInfo") or {}).get("value")
        if code and val is not None:
            try:
                by_code[code] = float(val)
            except (TypeError, ValueError):
                pass
    if not by_code:
        return None  # no usable index — let the dispatcher fall through
    return PollenResult(
        source="Google Pollen",
        location=location,
        tree_index=by_code.get("TREE"),
        grass_index=by_code.get("GRASS"),
        weed_index=by_code.get("WEED"),
    )
