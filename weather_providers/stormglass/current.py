"""Stormglass.io — current weather conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import deg_to_card, ms_to_kph, _sg_val

_B = "https://api.stormglass.io/v2"
_PARAMS = "airTemperature,humidity,pressure,windSpeed,windDirection,visibility"

async def fetch(headers, lat, lon, location):
    data = await get_json(f"{_B}/weather/point", params={
        "lat": lat, "lng": lon, "params": _PARAMS,
    }, headers=headers)
    hours = data.get("hours", [])
    if not hours:
        # fix: was raising ValueError; every other provider returns an
        # empty dataclass on empty upstream data so the dispatcher can
        # treat "no data" uniformly. Match that behaviour.
        return WeatherResult(
            source="Stormglass", temperature=None,
            description="", location=location,
        )
    c = hours[0]
    return WeatherResult(
        source="Stormglass",
        temperature=_sg_val(c, "airTemperature"),
        description="Current",
        location=location,
        humidity=_sg_val(c, "humidity"),
        wind_kph=ms_to_kph(_sg_val(c, "windSpeed")),
        wind_dir=deg_to_card(_sg_val(c, "windDirection")),
        pressure_mb=_sg_val(c, "pressure"),
        visibility_m=(_sg_val(c, "visibility") * 1000)
                     if _sg_val(c, "visibility") is not None else None,
    )
