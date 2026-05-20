"""WeatherKit — weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_SEV = {"extreme":"extreme","severe":"severe","moderate":"moderate","minor":"minor"}
async def fetch(url, headers, location):
    data = await get_json(url, params={"dataSets": "weatherAlerts"}, headers=headers)
    alerts = []
    for a in data.get("weatherAlerts",{}).get("alerts",[]):
        desc = (a.get("description") or "")[:300]
        alerts.append(AlertEntry(event=a.get("description","Unknown")[:100], severity=_SEV.get((a.get("severity") or "").lower(), "unknown"), headline=a.get("source","Apple Weather"), start=a.get("effectiveTime",""), end=a.get("expireTime",""), description=desc))
    return AlertsResult(source="Apple Weather", location=location, alerts=alerts)
