"""OpenWeatherMap - 5-day/3-hour forecast → daily aggregation."""
from __future__ import annotations
from datetime import datetime
from collections import defaultdict
from .._http import get_json
from ..base import WeatherResult, ForecastDay
_B = "https://api.openweathermap.org/data/2.5"
async def fetch(key, lat, lon, location, days=4):
    data = await get_json(f"{_B}/forecast", params={"lat": lat, "lon": lon, "appid": key, "units": "metric"})
    by_day = defaultdict(list)
    for entry in data.get("list",[]):
        dt = entry.get("dt_txt","")[:10]; by_day[dt].append(entry)
    fc = []
    for dt_str in sorted(by_day.keys())[:days]:
        entries = by_day[dt_str]
        temps = [e.get("main",{}).get("temp") for e in entries if e.get("main",{}).get("temp") is not None]
        hi = max(temps) if temps else None; lo = min(temps) if temps else None
        desc = entries[len(entries)//2].get("weather",[{}])[0].get("description","N/A").title() if entries else "N/A"
        try: dn = datetime.fromisoformat(dt_str).strftime("%A")
        except Exception: dn = dt_str
        fc.append(ForecastDay(day_name=dn, high_c=hi, low_c=lo, description=desc))
    return WeatherResult(source="OpenWeatherMap", temperature=None, description="", location=location, forecast=fc)
