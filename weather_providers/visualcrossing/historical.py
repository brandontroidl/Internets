"""Visual Crossing — historical weather."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult

_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

async def fetch(key: str, lat: float, lon: float, location: str, target_date: str = "") -> HistoricalResult:
    if not target_date:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(
        f"{_BASE}/{lat},{lon}/{target_date}/{target_date}",
        params={"unitGroup": "metric", "key": key,
                "include": "days", "contentType": "json"},
    )
    days = data.get("days", [])
    if not days:
        raise ValueError("No historical data")
    d = days[0]
    return HistoricalResult(
        source="Visual Crossing", location=location, date=target_date,
        high_c=d.get("tempmax"), low_c=d.get("tempmin"),
        avg_c=d.get("temp"),
        description=d.get("conditions", ""),
        precip_mm=d.get("precip"),
        max_wind_kph=d.get("windspeed"),
        avg_humidity=d.get("humidity"),
    )
