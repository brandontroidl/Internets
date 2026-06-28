"""World Weather Online - astronomy (sun/moon)."""
from __future__ import annotations
from datetime import date
from .._http import get_json
from ..base import AstronomyResult
# fix: _float was duplicated in every endpoint file - moved to _codes.
from ._codes import _float

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str) -> AstronomyResult:
    data = await get_json(f"{_B}/weather.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json",
        "date": date.today().isoformat(), "fx": "no", "cc": "no",
        "num_of_days": "1", "showlocaltime": "yes",
    })
    weather = data.get("data", {}).get("weather", [])
    if not weather:
        raise ValueError("WWO: no data")
    astro = weather[0].get("astronomy", [{}])[0] if weather[0].get("astronomy") else {}
    return AstronomyResult(
        source="World Weather Online", location=location,
        sunrise=astro.get("sunrise", ""),
        sunset=astro.get("sunset", ""),
        moonrise=astro.get("moonrise", ""),
        moonset=astro.get("moonset", ""),
        moon_phase=astro.get("moon_phase", ""),
        moon_illumination=_float(astro.get("moon_illumination")),
    )
