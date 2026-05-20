"""Visual Crossing — hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ..base import deg_to_card

_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

async def fetch(key: str, lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await get_json(
        f"{_BASE}/{lat},{lon}/next24hours",
        params={"unitGroup": "metric", "key": key,
                "include": "hours", "contentType": "json"},
    )
    entries = []
    now = datetime.now()
    for day in data.get("days", []):
        for h in day.get("hours", []):
            dt_str = f"{day.get('datetime', '')}T{h.get('datetime', '')}"
            try:
                dt = datetime.fromisoformat(dt_str)
                if dt < now:
                    continue
                tm = dt.strftime("%I %p").lstrip("0")
            except Exception:
                tm = h.get("datetime", "")
            if len(entries) >= hours:
                break
            entries.append(HourlyEntry(
                time=tm,
                temp_c=h.get("temp"),
                description=h.get("conditions", ""),
                precip_mm=h.get("precip"),
                precip_chance=h.get("precipprob"),
                humidity=h.get("humidity"),
                wind_kph=h.get("windspeed"),
                wind_dir=deg_to_card(h.get("winddir")),
            ))
        if len(entries) >= hours:
            break
    return HourlyResult(source="Visual Crossing", location=location, hours=entries)
