"""WeatherBit.io — air quality (current conditions)."""
from __future__ import annotations
from .._http import get_json
from ..base import AirQualityResult, aqi_category

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current/airquality", params={
        "key": key, "lat": lat, "lon": lon,
    })
    items = data.get("data", [])
    if not items:
        raise ValueError("WeatherBit AQ returned no data")
    c = items[0]
    aqi = c.get("aqi")
    aqi_int = int(aqi) if aqi is not None else None
    return AirQualityResult(
        source="WeatherBit",
        location=location,
        aqi=aqi_int,
        category=aqi_category(aqi_int),
        pm25=c.get("pm25"),
        pm10=c.get("pm10"),
        o3=c.get("o3"),
        no2=c.get("no2"),
        so2=c.get("so2"),
        co=c.get("co"),
    )
