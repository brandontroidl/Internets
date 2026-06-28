"""Pirate Weather - weather alerts."""
from __future__ import annotations
from ..base import AlertsResult, AlertEntry
# fix: key embedded in URL path leaks into HTTPError messages - use
# safe_get_json wrapper which redacts the key before re-raising.
from ._codes import safe_get_json

_BASE = "https://api.pirateweather.net/forecast"

async def fetch(key: str, lat: float, lon: float, location: str) -> AlertsResult:
    data = await safe_get_json(f"{_BASE}/{key}/{lat},{lon}", key,
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
