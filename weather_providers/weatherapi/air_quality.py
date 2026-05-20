"""WeatherAPI.com — air quality."""
from __future__ import annotations
from .._http import get_json
from ..base import AirQualityResult, aqi_category
_B = "https://api.weatherapi.com/v1"
_EPA = {1:25,2:75,3:125,4:175,5:250,6:400}
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current.json", params={"key": key, "q": f"{lat},{lon}", "aqi": "yes"})
    aq = data.get("current",{}).get("air_quality",{})
    idx = aq.get("us-epa-index"); mapped = _EPA.get(int(idx)) if idx else None
    return AirQualityResult(source="WeatherAPI", location=location, aqi=mapped, category=aqi_category(mapped), pm25=aq.get("pm2_5"), pm10=aq.get("pm10"), o3=aq.get("o3"), no2=aq.get("no2"), so2=aq.get("so2"), co=aq.get("co"))
