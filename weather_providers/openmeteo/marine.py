"""Open-Meteo — marine weather (wave, swell, ocean conditions)."""
from __future__ import annotations
from .._http import get_json
from ..base import MarineResult
from ._codes import deg_to_card

_BASE = "https://marine-api.open-meteo.com/v1/marine"

async def fetch(lat: float, lon: float, location: str) -> MarineResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "current": "wave_height,wave_period,wave_direction,wind_wave_height,swell_wave_height,swell_wave_period,swell_wave_direction", "timezone": "auto"})
    c = data.get("current", {})
    return MarineResult(source="Open-Meteo", location=location, wave_height_m=c.get("wave_height"), wave_period_s=c.get("wave_period"), wave_direction=deg_to_card(c.get("wave_direction")), swell_height_m=c.get("swell_wave_height"), swell_period_s=c.get("swell_wave_period"), swell_direction=deg_to_card(c.get("swell_wave_direction")), wind_wave_height_m=c.get("wind_wave_height"))
