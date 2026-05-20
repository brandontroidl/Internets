"""Stormglass.io — marine weather (waves, swell, water temperature)."""
from __future__ import annotations
from .._http import get_json
from ..base import MarineResult
from ._codes import deg_to_card, _sg_val

_B = "https://api.stormglass.io/v2"
_PARAMS = ("waveHeight,wavePeriod,waveDirection,"
           "windWaveHeight,windWavePeriod,"
           "swellHeight,swellPeriod,swellDirection,"
           "waterTemperature")

async def fetch(headers, lat, lon, location):
    data = await get_json(f"{_B}/weather/point", params={
        "lat": lat, "lng": lon, "params": _PARAMS,
    }, headers=headers)
    hours = data.get("hours", [])
    if not hours:
        # fix: was raising ValueError; every other provider returns an
        # empty dataclass on empty upstream data so the dispatcher can
        # treat "no data" uniformly. Match that behaviour.
        return MarineResult(source="Stormglass", location=location)
    c = hours[0]
    return MarineResult(
        source="Stormglass",
        location=location,
        wave_height_m=_sg_val(c, "waveHeight"),
        wave_period_s=_sg_val(c, "wavePeriod"),
        wave_direction=deg_to_card(_sg_val(c, "waveDirection")),
        swell_height_m=_sg_val(c, "swellHeight"),
        swell_period_s=_sg_val(c, "swellPeriod"),
        swell_direction=deg_to_card(_sg_val(c, "swellDirection")),
        water_temp_c=_sg_val(c, "waterTemperature"),
        wind_wave_height_m=_sg_val(c, "windWaveHeight"),
    )
