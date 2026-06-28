"""Tomorrow.io - realtime weather."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import CODES, deg_to_card, ms_to_kph, km_to_m
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/weather/realtime", params={"apikey": key, "location": f"{lat},{lon}", "units": "metric"})
    v = data.get("data",{}).get("values",{})
    wc = v.get("weatherCode")
    return WeatherResult(source="Tomorrow.io", temperature=v.get("temperature"), description=CODES.get(wc, f"Code {wc}") if wc is not None else "Unknown", location=location, feels_like_c=v.get("temperatureApparent"), humidity=v.get("humidity"), wind_kph=ms_to_kph(v.get("windSpeed")), wind_dir=deg_to_card(v.get("windDirection")), pressure_mb=v.get("pressureSurfaceLevel"), visibility_m=km_to_m(v.get("visibility")), dewpoint_c=v.get("dewPoint"))
