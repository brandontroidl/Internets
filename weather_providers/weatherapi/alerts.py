"""WeatherAPI.com - weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_B = "https://api.weatherapi.com/v1"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/forecast.json", params={"key": key, "q": f"{lat},{lon}", "days": 1, "aqi": "no", "alerts": "yes"})
    alerts = []
    for a in data.get("alerts",{}).get("alert",[]):
        alerts.append(AlertEntry(event=a.get("event","Unknown"), severity=(a.get("severity") or "unknown").lower(), headline=a.get("headline",""), start=a.get("effective",""), end=a.get("expires",""), description=(a.get("desc") or "")[:300]))
    return AlertsResult(source="WeatherAPI", location=location, alerts=alerts)
