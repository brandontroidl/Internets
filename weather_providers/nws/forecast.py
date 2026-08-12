"""NWS - daily forecast."""
from __future__ import annotations
from ._scope import OutOfCoverage, nws_json as get_json
from ..base import WeatherResult, ForecastDay

_HEADERS = {"User-Agent": "(Internets IRC Bot)", "Accept": "application/geo+json"}

async def fetch(lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    pts = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_HEADERS)
    fc_url = pts.get("properties", {}).get("forecast")
    if not fc_url:
        raise OutOfCoverage("NWS: no forecast URL")
    data = await get_json(fc_url, headers=_HEADERS)
    periods = data.get("properties", {}).get("periods", [])
    fc = []
    i = 0
    while i < len(periods) and len(fc) < days:
        p = periods[i]
        if p.get("isDaytime") is False:
            i += 1
            continue
        high = _f_to_c(p.get("temperature")) if p.get("temperatureUnit") == "F" else p.get("temperature")
        low = None
        if i + 1 < len(periods) and periods[i + 1].get("isDaytime") is False:
            np = periods[i + 1]
            low = _f_to_c(np.get("temperature")) if np.get("temperatureUnit") == "F" else np.get("temperature")
            i += 2
        else:
            i += 1
        fc.append(ForecastDay(
            day_name=p.get("name", ""),
            high_c=high,
            low_c=low,
            description=p.get("shortForecast", "N/A"),
        ))
    return WeatherResult(
        source="NWS", temperature=None, description="",
        location=location, forecast=fc,
    )

def _f_to_c(f):
    if f is None: return None
    return (f - 32) * 5 / 9
