"""Stormglass.io — hourly weather forecast."""
from __future__ import annotations
from datetime import datetime, timezone
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
from ._codes import deg_to_card, ms_to_kph, _sg_val

_B = "https://api.stormglass.io/v2"
_PARAMS = "airTemperature,humidity,precipitation,windSpeed,windDirection"

async def fetch(headers, lat, lon, location, hours=12):
    data = await get_json(f"{_B}/weather/point", params={
        "lat": lat, "lng": lon, "params": _PARAMS,
    }, headers=headers)
    entries = []
    now = datetime.now(timezone.utc)
    for h in data.get("hours", []):
        ts = h.get("time", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < now:
                continue
            tm = dt.strftime("%I %p").lstrip("0")
        except Exception:
            tm = ts
        if len(entries) >= hours:
            break
        entries.append(HourlyEntry(
            time=tm,
            temp_c=_sg_val(h, "airTemperature"),
            description="",
            precip_mm=_sg_val(h, "precipitation"),
            humidity=_sg_val(h, "humidity"),
            wind_kph=ms_to_kph(_sg_val(h, "windSpeed")),
            wind_dir=deg_to_card(_sg_val(h, "windDirection")),
        ))
    return HourlyResult(source="Stormglass", location=location, hours=entries)
