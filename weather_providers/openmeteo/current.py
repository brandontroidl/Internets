"""Open-Meteo — current conditions endpoint."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import WMO_CODES, deg_to_card

_BASE = "https://api.open-meteo.com/v1/forecast"
_FIELDS = "temperature_2m,relative_humidity_2m,apparent_temperature,dew_point_2m,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m,visibility"

async def fetch(lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "current": _FIELDS, "wind_speed_unit": "kmh", "timezone": "auto"})
    c = data.get("current", {})
    wc = c.get("weather_code")
    return WeatherResult(source="Open-Meteo", temperature=c.get("temperature_2m"), description=WMO_CODES.get(wc, f"Code {wc}") if wc is not None else "Unknown", location=location, feels_like_c=c.get("apparent_temperature"), humidity=c.get("relative_humidity_2m"), wind_kph=c.get("wind_speed_10m"), wind_dir=deg_to_card(c.get("wind_direction_10m")), pressure_mb=c.get("surface_pressure"), visibility_m=c.get("visibility"), dewpoint_c=c.get("dew_point_2m"))
