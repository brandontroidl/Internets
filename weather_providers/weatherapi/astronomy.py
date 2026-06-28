"""WeatherAPI.com - astronomy (sun/moon)."""
from __future__ import annotations
from datetime import date
from .._http import get_json
from ..base import AstronomyResult
_B = "https://api.weatherapi.com/v1"
def _tof(v):
    try: return float(v)
    except (ValueError, TypeError): return None
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/astronomy.json", params={"key": key, "q": f"{lat},{lon}", "dt": date.today().isoformat()})
    a = data.get("astronomy",{}).get("astro",{})
    return AstronomyResult(source="WeatherAPI", location=location, sunrise=a.get("sunrise",""), sunset=a.get("sunset",""), moonrise=a.get("moonrise",""), moonset=a.get("moonset",""), moon_phase=a.get("moon_phase",""), moon_illumination=_tof(a.get("moon_illumination")))
