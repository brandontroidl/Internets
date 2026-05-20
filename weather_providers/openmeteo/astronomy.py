"""Open-Meteo — astronomy (sunrise/sunset from daily params)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import AstronomyResult

_BASE = "https://api.open-meteo.com/v1/forecast"

async def fetch(lat: float, lon: float, location: str) -> AstronomyResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "daily": "sunrise,sunset,daylight_duration", "forecast_days": 1, "timezone": "auto"})
    d = data.get("daily", {})
    sunrise = sunset = day_len = ""
    sr_list, ss_list, dl_list = d.get("sunrise", []), d.get("sunset", []), d.get("daylight_duration", [])
    if sr_list:
        try: sunrise = datetime.fromisoformat(sr_list[0]).strftime("%I:%M %p").lstrip("0")
        except Exception: sunrise = sr_list[0]
    if ss_list:
        try: sunset = datetime.fromisoformat(ss_list[0]).strftime("%I:%M %p").lstrip("0")
        except Exception: sunset = ss_list[0]
    if dl_list and isinstance(dl_list[0], (int, float)):
        h, m = divmod(int(dl_list[0]), 3600)
        day_len = f"{h}h {m // 60}m"
    return AstronomyResult(source="Open-Meteo", location=location, sunrise=sunrise, sunset=sunset, day_length=day_len)
