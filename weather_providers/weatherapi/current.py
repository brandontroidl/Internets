"""WeatherAPI.com - current conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
_B = "https://api.weatherapi.com/v1"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current.json", params={"key": key, "q": f"{lat},{lon}", "aqi": "no"})
    c = data.get("current", {})
    return WeatherResult(source="WeatherAPI", temperature=c.get("temp_c"), description=c.get("condition",{}).get("text","Unknown"), location=location, feels_like_c=c.get("feelslike_c"), humidity=c.get("humidity"), wind_kph=c.get("wind_kph"), wind_dir=c.get("wind_dir",""), pressure_mb=c.get("pressure_mb"), visibility_m=(c["vis_km"]*1000) if c.get("vis_km") is not None else None, dewpoint_c=c.get("dewpoint_c"))
