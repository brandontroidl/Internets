"""Open-Meteo - precipitation nowcast (15-minutely, next ~2 h)."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .._http import get_json
from ..base import NowcastResult, NowcastEntry
from ._codes import WMO_CODES

_BASE = "https://api.open-meteo.com/v1/forecast"


def _intensity(mm):
    if mm is None or mm <= 0:
        return "none"
    if mm < 0.5:
        return "light"
    if mm < 2.0:
        return "moderate"
    return "heavy"


def _ptype(code):
    if code is None:
        return ""
    d = WMO_CODES.get(code, "").lower()
    if "snow" in d:
        return "snow"
    if "rain" in d or "drizzle" in d or "shower" in d:
        return "rain"
    return ""


async def fetch(lat: float, lon: float, location: str, steps: int = 8) -> NowcastResult:
    data = await get_json(_BASE, params={
        "latitude": lat, "longitude": lon,
        "minutely_15": "precipitation,weather_code",
        "forecast_minutely_15": 48, "timezone": "auto",
    })
    m = data.get("minutely_15", {})
    times = m.get("time", [])
    precip = m.get("precipitation", [])
    codes = m.get("weather_code", [])
    # Align "now" to the location's zone (times are timezone=auto) using the
    # response utc_offset, so the host timezone can't drop the first slot.
    offset = data.get("utc_offset_seconds") or 0
    now = (datetime.now(timezone.utc) + timedelta(seconds=offset)).replace(tzinfo=None)
    start = 0
    for i, t in enumerate(times):
        try:
            if datetime.fromisoformat(t) >= now:
                start = i
                break
        except Exception:  # noqa: BLE001
            pass  # nosec B110: best-effort cleanup
    entries: list[NowcastEntry] = []
    for i in range(start, min(start + steps, len(times))):
        mm = precip[i] if i < len(precip) else None
        code = codes[i] if i < len(codes) else None
        try:
            tm = datetime.fromisoformat(times[i]).strftime("%I:%M %p").lstrip("0")
        except Exception:  # noqa: BLE001
            tm = times[i]
        entries.append(NowcastEntry(
            time=tm, precip_mm=mm, precip_type=_ptype(code), intensity=_intensity(mm)))
    summary = "No precipitation expected in the next 2 hours"
    for e in entries:
        if e.precip_mm and e.precip_mm > 0:
            summary = f"Precipitation around {e.time}"
            break
    return NowcastResult(source="Open-Meteo", location=location,
                         summary=summary, entries=entries)
