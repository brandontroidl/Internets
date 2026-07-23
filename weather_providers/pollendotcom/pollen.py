"""Pollen.com (IQVIA) - US allergy forecast (unofficial public API).

lat/lon is reverse-geocoded to a US ZIP via Nominatim, then the current
allergy index (0-12) + dominant allergens are read for that ZIP.  Returns
``None`` for non-US locations (so the dispatcher falls through to another
pollen provider) rather than raising - a coverage gap is not an error.
"""
from __future__ import annotations

from .._http import get_json
from ..base import PollenResult, pollen_cat_12

_REV = "https://nominatim.openstreetmap.org/reverse"
_API = "https://www.pollen.com/api/forecast/current/pollen/"


async def fetch(user_agent, lat, lon, location):
    ua = user_agent or "InternetsBot/1.0 (weather)"
    # 1. lat/lon → US ZIP (Pollen.com is keyed on ZIP and US-only).
    # zoom=18 (Nominatim's reverse default) - a postcode is only present at
    # street/building granularity.  zoom=10 (city) omitted it for most US
    # locations, so Pollen.com silently returned None and the command fell
    # through to the Europe-only CAMS provider; only a place whose OSM node
    # carries a ZIP at city zoom (e.g. San Dimas) worked.
    rev = await get_json(_REV, params={
        "format": "jsonv2", "lat": lat, "lon": lon,
        "zoom": "18", "addressdetails": "1",
    }, headers={"User-Agent": ua})
    addr = rev.get("address", {}) if isinstance(rev, dict) else {}
    if (addr.get("country_code") or "").lower() != "us":
        return None  # US-only - let the dispatcher try the next provider
    zip5 = (addr.get("postcode") or "").split("-")[0].strip()
    if not (len(zip5) == 5 and zip5.isdigit()):
        return None

    # 2. Current allergy index for that ZIP.
    data = await get_json(_API + zip5, headers={
        "User-Agent": ua,
        "Referer": f"https://www.pollen.com/forecast/current/pollen/{zip5}",
        "Accept": "application/json, text/plain, */*",
    })
    loc = data.get("Location", {}) if isinstance(data, dict) else {}
    periods = loc.get("periods") or []
    today = None
    for p in periods:
        if isinstance(p, dict) and (p.get("Type") or "").lower() == "today":
            today = p
            break
    if today is None:
        return None

    idx = today.get("Index")
    try:
        idx = float(idx) if idx is not None else None
    except (TypeError, ValueError):
        idx = None
    triggers = tuple(
        (t.get("Name") or "").strip()
        for t in (today.get("Triggers") or [])
        if isinstance(t, dict) and (t.get("Name") or "").strip()
    )[:4]

    return PollenResult(
        source="Pollen.com",
        location=location,
        overall_index=idx,
        category=pollen_cat_12(idx),
        triggers=triggers,
    )
