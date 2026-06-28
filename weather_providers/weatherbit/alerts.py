"""WeatherBit.io - weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/alerts", params={
        "key": key, "lat": lat, "lon": lon,
    })
    alerts = []
    for a in data.get("alerts", []):
        sev = (a.get("severity") or "unknown").lower()
        desc = (a.get("description") or "")[:300]
        alerts.append(AlertEntry(
            event=a.get("title", "Unknown"),
            severity=sev,
            headline=a.get("title", ""),
            start=a.get("onset", a.get("effective", "")),
            end=a.get("expires", a.get("ends", "")),
            description=desc,
        ))
    return AlertsResult(source="WeatherBit", location=location, alerts=alerts)
