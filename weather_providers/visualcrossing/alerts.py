"""Visual Crossing - weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry

_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

async def fetch(key: str, lat: float, lon: float, location: str) -> AlertsResult:
    data = await get_json(
        f"{_BASE}/{lat},{lon}/today",
        params={"unitGroup": "metric", "key": key,
                "include": "alerts", "contentType": "json"},
    )
    alerts = []
    for a in data.get("alerts", []):
        alerts.append(AlertEntry(
            event=a.get("event", "Unknown"),
            severity=(a.get("severity") or "unknown").lower(),
            headline=a.get("headline", ""),
            start=a.get("onset", ""),
            end=a.get("ends", ""),
            description=(a.get("description") or "")[:300],
        ))
    return AlertsResult(source="Visual Crossing", location=location, alerts=alerts)
