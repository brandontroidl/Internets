"""WeatherKit - hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import readable, deg_to_card
async def fetch(url, headers, location, hours=12):
    data = await get_json(url, params={"dataSets": "forecastHourly"}, headers=headers)
    entries = []
    for e in data.get("forecastHourly",{}).get("hours",[])[:hours]:
        hum = e.get("humidity"); hpct = (hum*100) if hum is not None else None
        try: tm = datetime.fromisoformat(e.get("forecastStart","").replace("Z","+00:00")).strftime("%I %p").lstrip("0")
        except Exception: tm = e.get("forecastStart","")
        entries.append(HourlyEntry(time=tm, temp_c=e.get("temperature"), description=readable(e.get("conditionCode")), precip_mm=e.get("precipitationAmount"), precip_chance=(e.get("precipitationChance",0)*100) if e.get("precipitationChance") is not None else None, humidity=hpct, wind_kph=e.get("windSpeed"), wind_dir=deg_to_card(e.get("windDirection"))))
    return HourlyResult(source="Apple Weather", location=location, hours=entries)
