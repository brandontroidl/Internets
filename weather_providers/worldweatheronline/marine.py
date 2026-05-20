"""World Weather Online — marine weather (premium marine API)."""
from __future__ import annotations
from .._http import get_json
from ..base import MarineResult
# fix: _float was duplicated in every endpoint file — moved to _codes.
from ._codes import _float

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str) -> MarineResult:
    data = await get_json(f"{_B}/marine.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json",
    })
    weather = data.get("data", {}).get("weather", [])
    if not weather:
        raise ValueError("WWO: no marine data")
    # Take current hour's marine data.
    hourly = weather[0].get("hourly", [])
    now_data = hourly[len(hourly) // 2] if hourly else {}
    # fix: wave_direction was using swellDir16Point — that's the swell
    # bearing, not the wave bearing. WWO's marine schema exposes the
    # combined-sea direction in winddir16Point (the surface wind drives
    # wind-waves) — use that for wave_direction, and keep
    # swellDir16Point dedicated to swell_direction.
    return MarineResult(
        source="World Weather Online", location=location,
        wave_height_m=_float(now_data.get("sigHeight_m")),
        wave_period_s=_float(now_data.get("swellPeriod_secs")),
        wave_direction=now_data.get("winddir16Point", ""),
        swell_height_m=_float(now_data.get("swellHeight_m")),
        swell_period_s=_float(now_data.get("swellPeriod_secs")),
        swell_direction=now_data.get("swellDir16Point", ""),
        water_temp_c=_float(now_data.get("waterTemp_C")),
    )
