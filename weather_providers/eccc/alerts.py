"""ECCC - weather alerts (Environment and Climate Change Canada)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry

# OGC API Features collection for active weather alerts (GeoJSON).
_BASE = "https://api.weather.gc.ca/collections/weather-alerts/items"

# Map ECCC alert_type ("warning"/"watch"/"statement"/"advisory") to a
# CAP-style severity bucket so output is consistent with other providers.
_SEVERITY = {
    "warning": "severe",
    "watch": "moderate",
    "advisory": "moderate",
    "statement": "minor",
}


async def fetch(lat, lon, location):
    # ~0.5deg box around the point (bbox order is W,S,E,N per OGC/GeoJSON).
    d = 0.5
    bbox = f"{lon - d},{lat - d},{lon + d},{lat + d}"
    data = await get_json(_BASE, params={"f": "json", "bbox": bbox})
    alerts = []
    for f in data.get("features", []):
        p = (f or {}).get("properties") or {}
        # Skip ended/cancelled entries - keep only active alerts.
        if str(p.get("status_en", "")).lower() in ("ended", "cancelled", "canceled"):
            continue
        atype = str(p.get("alert_type", "")).lower()
        event = p.get("alert_name_en") or p.get("alert_short_name_en") or "Alert"
        area = p.get("feature_name_en") or ""
        headline = f"{event} - {area}" if area else event
        alerts.append(AlertEntry(
            event=event,
            severity=_SEVERITY.get(atype, "unknown"),
            headline=headline,
            start=p.get("validity_datetime") or p.get("publication_datetime") or "",
            end=p.get("event_end_datetime") or p.get("expiration_datetime") or "",
            description=(p.get("alert_text_en") or "")[:300],
        ))
    # Empty list (e.g. outside Canada, or no active alerts) is valid data.
    return AlertsResult(source="ECCC", location=location, alerts=alerts)
