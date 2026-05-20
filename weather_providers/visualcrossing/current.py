"""Visual Crossing — current conditions.

https://www.visualcrossing.com/resources/documentation/weather-api/timeline-weather-api/
"""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ..base import deg_to_card

_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"

async def fetch(key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(
        f"{_BASE}/{lat},{lon}/today",
        params={"unitGroup": "metric", "key": key,
                "include": "current", "contentType": "json"},
    )
    c = data.get("currentConditions", {})
    return WeatherResult(
        source="Visual Crossing",
        temperature=c.get("temp"),
        description=c.get("conditions", "Unknown"),
        location=location,
        feels_like_c=c.get("feelslike"),
        humidity=c.get("humidity"),
        wind_kph=c.get("windspeed"),
        wind_dir=deg_to_card(c.get("winddir")),
        pressure_mb=c.get("pressure"),
        visibility_m=(c["visibility"] * 1000) if c.get("visibility") is not None else None,
        dewpoint_c=c.get("dew"),
    )
