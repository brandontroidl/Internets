"""Open-Meteo - daily forecast endpoint."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
from ._codes import WMO_CODES

_BASE = "https://api.open-meteo.com/v1/forecast"

async def fetch(lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "daily": "weather_code,temperature_2m_max,temperature_2m_min", "current": "temperature_2m,weather_code", "forecast_days": min(days, 16), "timezone": "auto"})
    cur = data.get("current", {})
    daily = data.get("daily", {})
    wc = cur.get("weather_code")
    dates, codes = daily.get("time", []), daily.get("weather_code", [])
    highs, lows = daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", [])
    fc = []
    for i in range(min(days, len(dates))):
        try: dn = datetime.fromisoformat(dates[i]).strftime("%A")
        except Exception: dn = dates[i]
        code = codes[i] if i < len(codes) else None
        fc.append(ForecastDay(day_name=dn, high_c=highs[i] if i < len(highs) else None, low_c=lows[i] if i < len(lows) else None, description=WMO_CODES.get(code, "N/A") if code is not None else "N/A"))
    return WeatherResult(source="Open-Meteo", temperature=cur.get("temperature_2m"), description=WMO_CODES.get(wc, "N/A") if wc is not None else "N/A", location=location, forecast=fc)
