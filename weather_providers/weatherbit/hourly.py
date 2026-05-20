"""WeatherBit.io — hourly forecast (48 hours free, 120 hours paid)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ..base import ms_to_kph

_B = "https://api.weatherbit.io/v2.0"

async def fetch(key, lat, lon, location, hours=12):
    data = await get_json(f"{_B}/forecast/hourly", params={
        "key": key, "lat": lat, "lon": lon, "units": "M", "hours": min(hours, 48),
    })
    entries = []
    for h in data.get("data", [])[:hours]:
        ts = h.get("timestamp_local", h.get("datetime", ""))
        try:
            tm = datetime.fromisoformat(ts).strftime("%I %p").lstrip("0")
        except Exception:
            tm = ts
        w = h.get("weather", {})
        entries.append(HourlyEntry(
            time=tm,
            temp_c=h.get("temp"),
            description=w.get("description", ""),
            precip_mm=h.get("precip"),
            precip_chance=h.get("pop"),
            humidity=h.get("rh"),
            wind_kph=ms_to_kph(h.get("wind_spd")),
            wind_dir=h.get("wind_cdir", ""),
        ))
    return HourlyResult(source="WeatherBit", location=location, hours=entries)
