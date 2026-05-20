"""OpenWeatherMap — alerts (requires OneCall 3.0 API)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
_B = "https://api.openweathermap.org/data/3.0"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/onecall", params={"lat": lat, "lon": lon, "appid": key, "exclude": "minutely,hourly,daily"})
    alerts = []
    for a in data.get("alerts",[]):
        alerts.append(AlertEntry(event=a.get("event","Unknown"), severity="moderate", headline=a.get("sender_name",""), start=a.get("start",""), end=a.get("end",""), description=(a.get("description") or "")[:300]))
    return AlertsResult(source="OpenWeatherMap", location=location, alerts=alerts)
