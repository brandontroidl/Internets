"""Open-Meteo — hourly forecast endpoint."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import WMO_CODES, deg_to_card

_BASE = "https://api.open-meteo.com/v1/forecast"

async def fetch(lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,relative_humidity_2m,wind_speed_10m,wind_direction_10m", "forecast_hours": min(hours, 48), "wind_speed_unit": "kmh", "timezone": "auto"})
    h = data.get("hourly", {})
    times = h.get("time", [])
    now = datetime.now()
    start = 0
    for i, t in enumerate(times):
        try:
            if datetime.fromisoformat(t) >= now: start = i; break
        except Exception: pass
    entries = []
    for i in range(start, min(start + hours, len(times))):
        code = h.get("weather_code", [])[i] if i < len(h.get("weather_code", [])) else None
        try: tm = datetime.fromisoformat(times[i]).strftime("%I %p").lstrip("0")
        except Exception: tm = times[i]
        entries.append(HourlyEntry(time=tm, temp_c=h.get("temperature_2m", [None]*999)[i] if i < len(h.get("temperature_2m",[])) else None, description=WMO_CODES.get(code, "") if code is not None else "", precip_mm=h.get("precipitation", [None]*999)[i] if i < len(h.get("precipitation",[])) else None, precip_chance=h.get("precipitation_probability", [None]*999)[i] if i < len(h.get("precipitation_probability",[])) else None, humidity=h.get("relative_humidity_2m", [None]*999)[i] if i < len(h.get("relative_humidity_2m",[])) else None, wind_kph=h.get("wind_speed_10m", [None]*999)[i] if i < len(h.get("wind_speed_10m",[])) else None, wind_dir=deg_to_card(h.get("wind_direction_10m", [None]*999)[i] if i < len(h.get("wind_direction_10m",[])) else None)))
    return HourlyResult(source="Open-Meteo", location=location, hours=entries)
