"""OpenWeatherMap - "hourly" forecast.

CADENCE WARNING: the free /forecast endpoint returns 3-hour steps, NOT
1-hour steps. The hourly-resolution forecast lives on the paid OneCall
3.0 API. This module is named ``hourly`` for dispatcher-shape
consistency but the data is 3-hourly. Each ``HourlyEntry.time`` here
represents the start of a 3-hour window. Dispatcher / consumers that
need true 1h cadence should prefer another provider.
"""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import deg_to_card, ms_to_kph
_B = "https://api.openweathermap.org/data/2.5"
async def fetch(key, lat, lon, location, hours=12):
    # fix: was mislabeled "hourly" but /forecast is 3-hour cadence; the
    # `hours` argument is now interpreted as the number of 3-hour
    # slices to return (so hours=12 yields ~36 h of data, four
    # slices = 12 h elapsed). Caller behaviour preserved: we still
    # return up to `hours` entries.
    data = await get_json(f"{_B}/forecast", params={"lat": lat, "lon": lon, "appid": key, "units": "metric"})
    entries = []
    for e in data.get("list",[])[:hours]:
        try: tm = datetime.fromisoformat(e.get("dt_txt","")).strftime("%I %p").lstrip("0")
        except Exception: tm = e.get("dt_txt","")
        w = e.get("weather",[{}])[0] if e.get("weather") else {}
        m = e.get("main",{})
        entries.append(HourlyEntry(time=tm, temp_c=m.get("temp"), description=w.get("description","").title(), precip_mm=e.get("rain",{}).get("3h"), precip_chance=e.get("pop",0)*100 if e.get("pop") is not None else None, humidity=m.get("humidity"), wind_kph=ms_to_kph(e.get("wind",{}).get("speed")), wind_dir=deg_to_card(e.get("wind",{}).get("deg"))))
    return HourlyResult(source="OpenWeatherMap", location=location, hours=entries)
