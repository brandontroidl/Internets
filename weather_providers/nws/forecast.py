"""NWS - daily forecast."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult, ForecastDay

_HEADERS = {"User-Agent": "(Internets IRC Bot)", "Accept": "application/geo+json"}

async def fetch(lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    pts = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_HEADERS)
    fc_url = pts.get("properties", {}).get("forecast")
    if not fc_url:
        raise ValueError("NWS: no forecast URL")
    data = await get_json(fc_url, headers=_HEADERS)
    periods = data.get("properties", {}).get("periods", [])
    # NWS returns periods for day/night. Pair them up.
    fc = []
    seen_days = set()
    for p in periods:
        name = p.get("name", "")
        if p.get("isDaytime") is False:
            continue
        if len(fc) >= days:
            break
        fc.append(ForecastDay(
            day_name=name,
            high_c=_f_to_c(p.get("temperature")) if p.get("temperatureUnit") == "F" else p.get("temperature"),
            low_c=None,
            description=p.get("shortForecast", "N/A"),
        ))
    return WeatherResult(
        source="NWS", temperature=None, description="",
        location=location, forecast=fc,
    )

def _f_to_c(f):
    if f is None: return None
    return round((f - 32) * 5 / 9, 1)
