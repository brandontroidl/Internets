"""Visual Crossing — daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay

_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

async def fetch(key: str, lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    data = await get_json(
        f"{_BASE}/{lat},{lon}/next{min(days, 15)}days",
        params={"unitGroup": "metric", "key": key,
                "include": "days,current", "contentType": "json"},
    )
    cur = data.get("currentConditions", {})
    fc = []
    for d in data.get("days", [])[:days]:
        try:
            day_name = datetime.fromisoformat(d.get("datetime", "")).strftime("%A")
        except Exception:
            day_name = d.get("datetime", "")
        fc.append(ForecastDay(
            day_name=day_name,
            high_c=d.get("tempmax"),
            low_c=d.get("tempmin"),
            description=d.get("conditions", "N/A"),
        ))
    return WeatherResult(
        source="Visual Crossing",
        temperature=cur.get("temp"),
        description=cur.get("conditions", "N/A"),
        location=location,
        forecast=fc,
    )
