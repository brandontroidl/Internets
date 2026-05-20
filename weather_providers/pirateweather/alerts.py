"""Pirate Weather — weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry

_BASE = "https://api.pirateweather.net/forecast"

async def fetch(key: str, lat: float, lon: float, location: str) -> AlertsResult:
    data = await get_json(f"{_BASE}/{key}/{lat},{lon}",
                          params={"units": "si", "exclude": "minutely,hourly,daily"})
    alerts = []
    for a in data.get("alerts", []):
        alerts.append(AlertEntry(
            event=a.get("title", "Unknown"),
            severity=(a.get("severity") or "unknown").lower(),
            headline=a.get("title", ""),
            start=a.get("time", ""), end=a.get("expires", ""),
            description=(a.get("description") or "")[:300],
        ))
    return AlertsResult(source="Pirate Weather", location=location, alerts=alerts)
