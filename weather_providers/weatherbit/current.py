"""WeatherBit.io — current conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ..base import deg_to_card, ms_to_kph

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current", params={
        "key": key, "lat": lat, "lon": lon, "units": "M",
    })
    items = data.get("data", [])
    if not items:
        raise ValueError("WeatherBit returned no data")
    c = items[0]
    w = c.get("weather", {})
    return WeatherResult(
        source="WeatherBit",
        temperature=c.get("temp"),
        description=w.get("description", "Unknown"),
        location=location,
        feels_like_c=c.get("app_temp"),
        humidity=c.get("rh"),
        wind_kph=ms_to_kph(c.get("wind_spd")),
        wind_dir=c.get("wind_cdir", ""),
        pressure_mb=c.get("pres"),
        visibility_m=(c["vis"] * 1000) if c.get("vis") is not None else None,
        dewpoint_c=c.get("dewpt"),
    )
