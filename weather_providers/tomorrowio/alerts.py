"""Tomorrow.io — weather events/alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/events", params={"apikey": key, "location": f"{lat},{lon}"})
    alerts = []
    for ev in data.get("data",{}).get("events",[]):
        alerts.append(AlertEntry(event=ev.get("eventType",ev.get("title","Unknown")), severity=(ev.get("severity") or "unknown").lower(), headline=ev.get("title",""), start=ev.get("startTime",""), end=ev.get("endTime",""), description=(ev.get("description") or "")[:300]))
    return AlertsResult(source="Tomorrow.io", location=location, alerts=alerts)
