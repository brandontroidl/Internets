"""AccuWeather — weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_B = "http://dataservice.accuweather.com"
async def fetch(key, loc_key, location):
    data = await get_json(f"{_B}/alerts/v1/{loc_key}", params={"apikey": key})
    alerts = []
    for a in (data if isinstance(data, list) else []):
        desc = a.get("Area",[{}])[0].get("Text","")[:300] if a.get("Area") else ""
        alerts.append(AlertEntry(event=a.get("Description",{}).get("Localized","Unknown"), severity=(a.get("Priority","unknown") or "unknown").lower(), headline=a.get("Description",{}).get("Localized",""), start=a.get("Area",[{}])[0].get("StartTime","") if a.get("Area") else "", end=a.get("Area",[{}])[0].get("EndTime","") if a.get("Area") else "", description=desc))
    return AlertsResult(source="AccuWeather", location=location, alerts=alerts)
