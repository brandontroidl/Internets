"""Pirate Weather — daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
from ._codes import icon_to_desc

_BASE = "https://api.pirateweather.net/forecast"

async def fetch(key: str, lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    data = await get_json(f"{_BASE}/{key}/{lat},{lon}",
                          params={"units": "si", "exclude": "minutely,hourly,alerts"})
    cur = data.get("currently", {})
    fc = []
    for d in data.get("daily", {}).get("data", [])[:days]:
        try: day_name = datetime.fromtimestamp(d.get("time", 0)).strftime("%A")
        except Exception: day_name = "?"
        fc.append(ForecastDay(
            day_name=day_name, high_c=d.get("temperatureHigh"),
            low_c=d.get("temperatureLow"), description=icon_to_desc(d.get("icon")),
        ))
    return WeatherResult(
        source="Pirate Weather", temperature=cur.get("temperature"),
        description=icon_to_desc(cur.get("icon")), location=location, forecast=fc,
    )
