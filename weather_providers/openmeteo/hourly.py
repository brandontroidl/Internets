"""Open-Meteo — hourly forecast endpoint."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import WMO_CODES, deg_to_card

_BASE = "https://api.open-meteo.com/v1/forecast"


# fix: previously every field used ``h.get(field, [None]*999)[i]
# if i < len(h.get(field, []))`` — a 999-wide dead fallback (allocated
# per field per hour) that the guard never let through. Replace with a
# safe one-liner: only index when the array is long enough.
def _at(arr, i):
    """Return ``arr[i]`` when ``i`` is in range, else None."""
    return arr[i] if isinstance(arr, list) and i < len(arr) else None


async def fetch(lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,relative_humidity_2m,wind_speed_10m,wind_direction_10m", "forecast_hours": min(hours, 48), "wind_speed_unit": "kmh", "timezone": "auto"})
    h = data.get("hourly", {})
    times = h.get("time", [])
    temps = h.get("temperature_2m", [])
    codes = h.get("weather_code", [])
    precip = h.get("precipitation", [])
    pop = h.get("precipitation_probability", [])
    humid = h.get("relative_humidity_2m", [])
    wspd = h.get("wind_speed_10m", [])
    wdir = h.get("wind_direction_10m", [])
    now = datetime.now()
    start = 0
    for i, t in enumerate(times):
        try:
            if datetime.fromisoformat(t) >= now: start = i; break
        except Exception: pass  # nosec B110: best-effort cleanup
    entries = []
    for i in range(start, min(start + hours, len(times))):
        code = _at(codes, i)
        try: tm = datetime.fromisoformat(times[i]).strftime("%I %p").lstrip("0")
        except Exception: tm = times[i]
        entries.append(HourlyEntry(
            time=tm,
            temp_c=_at(temps, i),
            description=WMO_CODES.get(code, "") if code is not None else "",
            precip_mm=_at(precip, i),
            precip_chance=_at(pop, i),
            humidity=_at(humid, i),
            wind_kph=_at(wspd, i),
            wind_dir=deg_to_card(_at(wdir, i)),
        ))
    return HourlyResult(source="Open-Meteo", location=location, hours=entries)
