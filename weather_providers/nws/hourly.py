"""NWS — hourly forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import deg_to_card

_HEADERS = {"User-Agent": "(Internets IRC Bot)", "Accept": "application/geo+json"}

async def fetch(lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    pts = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_HEADERS)
    fc_url = pts.get("properties", {}).get("forecastHourly")
    if not fc_url:
        raise ValueError("NWS: no hourly forecast URL")
    data = await get_json(fc_url, headers=_HEADERS)
    periods = data.get("properties", {}).get("periods", [])
    entries = []
    for p in periods[:hours]:
        try: tm = datetime.fromisoformat(p.get("startTime", "")).strftime("%I %p").lstrip("0")
        except Exception: tm = p.get("startTime", "")
        temp = p.get("temperature")
        if p.get("temperatureUnit") == "F" and temp is not None:
            temp = round((temp - 32) * 5 / 9, 1)
        entries.append(HourlyEntry(
            time=tm, temp_c=temp,
            description=p.get("shortForecast", ""),
            wind_kph=_parse_wind(p.get("windSpeed")),
            wind_dir=p.get("windDirection", ""),
        ))
    return HourlyResult(source="NWS", location=location, hours=entries)

def _parse_wind(s):
    """Parse NWS wind like '15 mph' to kph."""
    if not s: return None
    try:
        parts = s.split()
        mph = float(parts[0])
        return round(mph * 1.609, 1)
    except Exception:
        return None
