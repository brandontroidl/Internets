"""GDACS - global disaster alerts (earthquakes, cyclones, floods, etc.)."""
from __future__ import annotations

from .._http import get_json, HTTPError
from ..base import AlertsResult, AlertEntry, haversine_km as _haversine_km

_BASE = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
_RADIUS_KM = 1000.0   # only surface events whose epicentre is within this range
_MAX_ALERTS = 8

# GDACS uses two-letter hazard codes; expand for readable event labels.
_EVENT_TYPES = {
    "EQ": "Earthquake",
    "TC": "Tropical Cyclone",
    "FL": "Flood",
    "DR": "Drought",
    "VO": "Volcano",
    "WF": "Wildfire",
}


async def fetch(lat, lon, location) -> AlertsResult:
    data = await get_json(_BASE, headers={"Accept": "application/json"})
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise HTTPError("GDACS: unexpected response shape",
                        status=None, provider_hint="gdacs")

    plat, plon = float(lat), float(lon)
    nearby: list[tuple[float, AlertEntry]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        # GeoJSON Point: [lon, lat].
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            continue
        try:
            elon, elat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        dist = _haversine_km(plat, plon, elat, elon)
        if dist > _RADIUS_KM:
            continue

        props = feat.get("properties") or {}
        etype = str(props.get("eventtype") or "").strip()
        name = str(props.get("name") or "").strip()
        desc = str(props.get("htmldescription") or props.get("description") or "").strip()
        event = _EVENT_TYPES.get(etype) or name or etype or "Disaster"
        headline = name or desc or event
        nearby.append((dist, AlertEntry(
            event=event,
            severity=str(props.get("alertlevel") or "").strip(),
            headline=headline,
            start=str(props.get("fromdate") or ""),
            end=str(props.get("todate") or ""),
            description=desc,
        )))

    # Nearest first; cap the list.  Empty is valid (no active events nearby).
    nearby.sort(key=lambda t: t[0])
    alerts = [entry for _, entry in nearby[:_MAX_ALERTS]]
    return AlertsResult(source="GDACS", location=location, alerts=alerts)
