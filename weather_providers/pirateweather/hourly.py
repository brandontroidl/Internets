"""Pirate Weather - hourly forecast."""
from __future__ import annotations
from datetime import datetime
from ..base import HourlyResult, HourlyEntry
# fix: key embedded in URL path leaks into HTTPError messages - use
# safe_get_json wrapper which redacts the key before re-raising.
from ._codes import deg_to_card, icon_to_desc, ms_to_kph, safe_get_json

_BASE = "https://api.pirateweather.net/forecast"

async def fetch(key: str, lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await safe_get_json(f"{_BASE}/{key}/{lat},{lon}", key,
                          params={"units": "si", "exclude": "minutely,daily,alerts"})
    entries = []
    for h in data.get("hourly", {}).get("data", [])[:hours]:
        try: tm = datetime.fromtimestamp(h.get("time", 0)).strftime("%I %p").lstrip("0")
        except Exception: tm = "?"
        entries.append(HourlyEntry(
            time=tm, temp_c=h.get("temperature"),
            description=icon_to_desc(h.get("icon")),
            precip_mm=h.get("precipIntensity"),
            precip_chance=(h["precipProbability"] * 100) if h.get("precipProbability") is not None else None,
            humidity=(h["humidity"] * 100) if h.get("humidity") is not None else None,
            wind_kph=ms_to_kph(h.get("windSpeed")),
            wind_dir=deg_to_card(h.get("windBearing")),
        ))
    return HourlyResult(source="Pirate Weather", location=location, hours=entries)
