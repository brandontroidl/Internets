"""WeatherBit.io - daily forecast (up to 16 days)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location, days=4):
    data = await get_json(f"{_B}/forecast/daily", params={
        "key": key, "lat": lat, "lon": lon, "units": "M", "days": min(days, 16),
    })
    items = data.get("data", [])
    fc = []
    for d in items[:days]:
        dt = d.get("valid_date", "")
        try:
            day_name = datetime.fromisoformat(dt).strftime("%A")
        except Exception:
            day_name = dt
        w = d.get("weather", {})
        fc.append(ForecastDay(
            day_name=day_name,
            high_c=d.get("high_temp") or d.get("max_temp"),
            low_c=d.get("low_temp") or d.get("min_temp"),
            description=w.get("description", "N/A"),
        ))
    return WeatherResult(
        source="WeatherBit", temperature=None, description="",
        location=location, forecast=fc,
    )
