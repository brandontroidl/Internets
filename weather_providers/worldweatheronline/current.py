"""World Weather Online — current conditions.
https://www.worldweatheronline.com/weather-api/
"""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import deg_to_card

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(f"{_B}/weather.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json", "num_of_days": "0",
        "fx": "no", "cc": "yes", "mca": "no", "fx24": "no",
    })
    c = data.get("data", {}).get("current_condition", [{}])[0]
    desc = c.get("weatherDesc", [{}])
    desc_text = desc[0].get("value", "Unknown") if desc else "Unknown"
    return WeatherResult(
        source="World Weather Online", temperature=_float(c.get("temp_C")),
        description=desc_text, location=location,
        feels_like_c=_float(c.get("FeelsLikeC")),
        humidity=_float(c.get("humidity")),
        wind_kph=_float(c.get("windspeedKmph")),
        wind_dir=c.get("winddir16Point", ""),
        pressure_mb=_float(c.get("pressure")),
        visibility_m=(_float(c.get("visibility")) * 1000) if c.get("visibility") else None,
        dewpoint_c=_float(c.get("DewPointC")),
    )

def _float(v):
    try: return float(v)
    except (TypeError, ValueError): return None
