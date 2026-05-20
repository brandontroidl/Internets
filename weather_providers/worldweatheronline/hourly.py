"""World Weather Online — hourly forecast (3-hourly intervals)."""
from __future__ import annotations
from .._http import get_json
from ..base import HourlyResult, HourlyEntry
# fix: _float was duplicated in every endpoint file — moved to _codes.
from ._codes import _float

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await get_json(f"{_B}/weather.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json",
        "num_of_days": "2", "fx": "yes", "fx24": "yes", "tp": "1",
    })
    entries = []
    for day in data.get("data", {}).get("weather", []):
        for h in day.get("hourly", []):
            if len(entries) >= hours: break
            time_str = h.get("time", "0").zfill(4)
            hr = int(time_str[:2]) if time_str.isdigit() else 0
            ampm = "AM" if hr < 12 else "PM"
            hr12 = hr % 12 or 12
            desc_list = h.get("weatherDesc", [{}])
            entries.append(HourlyEntry(
                time=f"{hr12} {ampm}",
                temp_c=_float(h.get("tempC")),
                description=desc_list[0].get("value", "") if desc_list else "",
                precip_mm=_float(h.get("precipMM")),
                precip_chance=_float(h.get("chanceofrain")),
                humidity=_float(h.get("humidity")),
                wind_kph=_float(h.get("windspeedKmph")),
                wind_dir=h.get("winddir16Point", ""),
            ))
        if len(entries) >= hours: break
    return HourlyResult(source="World Weather Online", location=location, hours=entries)
