"""Meteomatics — daily forecast."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from .._http import get_json
from ..base import WeatherResult, ForecastDay
_B = "https://api.meteomatics.com"
async def fetch(headers, lat, lon, location, days=4):
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT00:00:00Z")
    end = (now + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    data = await get_json(f"{_B}/{start}--{end}:P1D/t_max_2m_24h:C,t_min_2m_24h:C/{lat},{lon}/json", headers=headers)
    highs, lows = {}, {}
    for item in data.get("data",[]):
        param = item.get("parameter","")
        for coord in item.get("coordinates",[]):
            for d in coord.get("dates",[]):
                dt = d.get("date","")[:10]
                if "max" in param: highs[dt] = d.get("value")
                elif "min" in param: lows[dt] = d.get("value")
    fc = []
    for dt in sorted(highs.keys())[:days]:
        try: dn = datetime.fromisoformat(dt).strftime("%A")
        except Exception: dn = dt
        fc.append(ForecastDay(day_name=dn, high_c=highs.get(dt), low_c=lows.get(dt), description=""))
    return WeatherResult(source="Meteomatics", temperature=None, description="", location=location, forecast=fc)
