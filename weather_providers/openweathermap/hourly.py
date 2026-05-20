"""OpenWeatherMap — hourly from 5-day/3-hour forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import deg_to_card, ms_to_kph
_B = "https://api.openweathermap.org/data/2.5"
async def fetch(key, lat, lon, location, hours=12):
    data = await get_json(f"{_B}/forecast", params={"lat": lat, "lon": lon, "appid": key, "units": "metric"})
    entries = []
    for e in data.get("list",[])[:hours]:
        try: tm = datetime.fromisoformat(e.get("dt_txt","")).strftime("%I %p").lstrip("0")
        except Exception: tm = e.get("dt_txt","")
        w = e.get("weather",[{}])[0] if e.get("weather") else {}
        m = e.get("main",{})
        entries.append(HourlyEntry(time=tm, temp_c=m.get("temp"), description=w.get("description","").title(), precip_mm=e.get("rain",{}).get("3h"), precip_chance=e.get("pop",0)*100 if e.get("pop") is not None else None, humidity=m.get("humidity"), wind_kph=ms_to_kph(e.get("wind",{}).get("speed")), wind_dir=deg_to_card(e.get("wind",{}).get("deg"))))
    return HourlyResult(source="OpenWeatherMap", location=location, hours=entries)
