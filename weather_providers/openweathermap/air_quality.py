"""OpenWeatherMap - air quality (air pollution API)."""
from __future__ import annotations
from .._http import get_json
from ..base import AirQualityResult, aqi_category
from ._codes import AQI_MAP
_B = "https://api.openweathermap.org/data/2.5"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/air_pollution", params={"lat": lat, "lon": lon, "appid": key})
    items = data.get("list",[])
    if not items: return AirQualityResult(source="OpenWeatherMap", location=location)
    c = items[0].get("components",{}); idx = items[0].get("main",{}).get("aqi")
    mapped = AQI_MAP.get(idx)
    return AirQualityResult(source="OpenWeatherMap", location=location, aqi=mapped, category=aqi_category(mapped), pm25=c.get("pm2_5"), pm10=c.get("pm10"), o3=c.get("o3"), no2=c.get("no2"), so2=c.get("so2"), co=c.get("co"))
