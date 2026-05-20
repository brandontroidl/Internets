"""AccuWeather — hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
# fix: was http:// — leaked apikey in query string on the wire.
_B = "https://dataservice.accuweather.com"
async def fetch(key, loc_key, location, hours=12):
    data = await get_json(f"{_B}/forecasts/v1/hourly/12hour/{loc_key}", params={"apikey": key, "metric": "true", "details": "true"})
    entries = []
    for h in (data if isinstance(data, list) else [])[:hours]:
        try: tm = datetime.fromisoformat(h.get("DateTime","").replace("Z","+00:00") if "Z" in h.get("DateTime","") else h.get("DateTime","")).strftime("%I %p").lstrip("0")
        except Exception: tm = h.get("DateTime","")
        entries.append(HourlyEntry(time=tm, temp_c=h.get("Temperature",{}).get("Value"), description=h.get("IconPhrase",""), precip_mm=h.get("Rain",{}).get("Value"), precip_chance=h.get("PrecipitationProbability"), humidity=h.get("RelativeHumidity"), wind_kph=h.get("Wind",{}).get("Speed",{}).get("Value"), wind_dir=h.get("Wind",{}).get("Direction",{}).get("English","")))
    return HourlyResult(source="AccuWeather", location=location, hours=entries)
