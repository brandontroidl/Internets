"""Open-Meteo — historical weather (ERA5 reanalysis, 1940-present)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult
from ._codes import WMO_CODES

_BASE = "https://archive-api.open-meteo.com/v1/archive"

def _first(lst): return lst[0] if lst else None

async def fetch(lat: float, lon: float, location: str, target_date: str = "") -> HistoricalResult:
    if not target_date: target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "start_date": target_date, "end_date": target_date, "daily": "weather_code,temperature_2m_max,temperature_2m_min,temperature_2m_mean,precipitation_sum,wind_speed_10m_max,relative_humidity_2m_mean", "timezone": "auto"})
    d = data.get("daily", {})
    code = _first(d.get("weather_code"))
    return HistoricalResult(source="Open-Meteo", location=location, date=target_date, high_c=_first(d.get("temperature_2m_max")), low_c=_first(d.get("temperature_2m_min")), avg_c=_first(d.get("temperature_2m_mean")), description=WMO_CODES.get(code, "") if code is not None else "", precip_mm=_first(d.get("precipitation_sum")), max_wind_kph=_first(d.get("wind_speed_10m_max")), avg_humidity=_first(d.get("relative_humidity_2m_mean")))
