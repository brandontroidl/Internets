"""Pirate Weather — minutely precipitation nowcast (next 60 min)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import NowcastResult, NowcastEntry

_BASE = "https://api.pirateweather.net/forecast"

def _intensity(mm: float | None) -> str:
    if mm is None or mm < 0.1: return "none"
    if mm < 2.5: return "light"
    if mm < 7.6: return "moderate"
    return "heavy"

async def fetch(key: str, lat: float, lon: float, location: str) -> NowcastResult:
    data = await get_json(f"{_BASE}/{key}/{lat},{lon}",
                          params={"units": "si", "exclude": "hourly,daily,alerts"})
    minutely = data.get("minutely", {})
    summary = minutely.get("summary", "")
    entries = []
    for m in minutely.get("data", []):
        try: tm = datetime.fromtimestamp(m.get("time", 0)).strftime("%I:%M %p").lstrip("0")
        except Exception: tm = "?"
        mm = m.get("precipIntensity")
        entries.append(NowcastEntry(
            time=tm, precip_mm=mm,
            precip_type=m.get("precipType", "none") if mm and mm > 0 else "none",
            intensity=_intensity(mm),
        ))
    return NowcastResult(
        source="Pirate Weather", location=location,
        summary=summary, entries=entries,
    )
