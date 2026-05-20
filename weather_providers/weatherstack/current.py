"""Weatherstack — current conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
_B = "http://api.weatherstack.com"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current", params={"access_key": key, "query": f"{lat},{lon}", "units": "m"})
    c = data.get("current",{})
    desc_list = c.get("weather_descriptions",[])
    return WeatherResult(source="Weatherstack", temperature=c.get("temperature"), description=desc_list[0] if desc_list else "Unknown", location=location, feels_like_c=c.get("feelslike"), humidity=c.get("humidity"), wind_kph=c.get("wind_speed"), wind_dir=c.get("wind_dir",""), pressure_mb=c.get("pressure"), visibility_m=(c["visibility"]*1000) if c.get("visibility") is not None else None, dewpoint_c=None)
