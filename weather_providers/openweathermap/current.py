"""OpenWeatherMap - current conditions (2.5 API)."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import deg_to_card, ms_to_kph
_B = "https://api.openweathermap.org/data/2.5"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/weather", params={"lat": lat, "lon": lon, "appid": key, "units": "metric"})
    w = data.get("weather",[{}])[0] if data.get("weather") else {}
    m = data.get("main",{})
    wind = data.get("wind",{})
    vis = data.get("visibility")
    return WeatherResult(source="OpenWeatherMap", temperature=m.get("temp"), description=w.get("description","Unknown").title(), location=location, feels_like_c=m.get("feels_like"), humidity=m.get("humidity"), wind_kph=ms_to_kph(wind.get("speed")), wind_dir=deg_to_card(wind.get("deg")), pressure_mb=m.get("pressure"), visibility_m=vis, dewpoint_c=None)
