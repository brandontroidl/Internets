"""WeatherAPI.com - daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
_B = "https://api.weatherapi.com/v1"
async def fetch(key, lat, lon, location, days=4):
    data = await get_json(f"{_B}/forecast.json", params={"key": key, "q": f"{lat},{lon}", "days": min(days,14), "aqi": "no", "alerts": "no"})
    cur = data.get("current", {})
    fc = []
    for fd in data.get("forecast",{}).get("forecastday",[])[:days]:
        d = fd.get("day",{})
        try: dn = datetime.fromisoformat(fd.get("date","")).strftime("%A")
        except Exception: dn = fd.get("date","")
        fc.append(ForecastDay(day_name=dn, high_c=d.get("maxtemp_c"), low_c=d.get("mintemp_c"), description=d.get("condition",{}).get("text","N/A")))
    return WeatherResult(source="WeatherAPI", temperature=cur.get("temp_c"), description=cur.get("condition",{}).get("text","N/A"), location=location, forecast=fc)
