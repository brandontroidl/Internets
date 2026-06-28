"""Tomorrow.io - hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import CODES, deg_to_card, ms_to_kph
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location, hours=12):
    data = await get_json(f"{_B}/weather/forecast", params={"apikey": key, "location": f"{lat},{lon}", "units": "metric", "timesteps": "1h"})
    entries = []
    for e in data.get("timelines",{}).get("hourly",[])[:hours]:
        v = e.get("values",{})
        wc = v.get("weatherCode")
        try: tm = datetime.fromisoformat(e.get("time","").replace("Z","+00:00")).strftime("%I %p").lstrip("0")
        except Exception: tm = e.get("time","")
        entries.append(HourlyEntry(time=tm, temp_c=v.get("temperature"), description=CODES.get(wc,"") if wc is not None else "", precip_mm=v.get("precipitationIntensity"), precip_chance=v.get("precipitationProbability"), humidity=v.get("humidity"), wind_kph=ms_to_kph(v.get("windSpeed")), wind_dir=deg_to_card(v.get("windDirection"))))
    return HourlyResult(source="Tomorrow.io", location=location, hours=entries)
