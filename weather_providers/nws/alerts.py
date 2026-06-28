"""NWS - active weather alerts (most authoritative for US)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
from ._codes import map_severity

_HEADERS = {"User-Agent": "(Internets IRC Bot)", "Accept": "application/geo+json"}

async def fetch(lat: float, lon: float, location: str) -> AlertsResult:
    data = await get_json(
        f"https://api.weather.gov/alerts/active",
        params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual"},
        headers=_HEADERS,
    )
    alerts = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        alerts.append(AlertEntry(
            event=p.get("event", "Unknown"),
            severity=map_severity(p.get("severity")),
            headline=p.get("headline", ""),
            start=p.get("effective", ""),
            end=p.get("expires", ""),
            description=(p.get("description") or "")[:300],
        ))
    return AlertsResult(source="NWS", location=location, alerts=alerts)
