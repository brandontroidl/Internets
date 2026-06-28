"""AccuWeather - current conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
# fix: was http:// - leaked apikey in query string on the wire.
_B = "https://dataservice.accuweather.com"
async def fetch(key, loc_key, location):
    data = await get_json(f"{_B}/currentconditions/v1/{loc_key}", params={"apikey": key, "details": "true"})
    if not data: raise ValueError("No data")
    c = data[0] if isinstance(data, list) else data
    temp = c.get("Temperature",{}).get("Metric",{}).get("Value")
    fl = c.get("RealFeelTemperature",{}).get("Metric",{}).get("Value")
    wind = c.get("Wind",{})
    ws = wind.get("Speed",{}).get("Metric",{}).get("Value")
    wd = wind.get("Direction",{}).get("English","")
    vis = c.get("Visibility",{}).get("Metric",{}).get("Value")
    pres = c.get("Pressure",{}).get("Metric",{}).get("Value")
    return WeatherResult(source="AccuWeather", temperature=temp, description=c.get("WeatherText","Unknown"), location=location, feels_like_c=fl, humidity=c.get("RelativeHumidity"), wind_kph=ws, wind_dir=wd, pressure_mb=pres, visibility_m=(vis*1000) if vis is not None else None, dewpoint_c=c.get("DewPoint",{}).get("Metric",{}).get("Value"))
