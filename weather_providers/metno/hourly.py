"""MET Norway — hourly forecast (locationforecast/2.0/compact)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import HourlyResult, HourlyEntry, deg_to_card, ms_to_kph

_BASE = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_HEADERS = {"User-Agent": "Internets-IRC-Bot/2.x github.com/brandontroidl/Internets"}


def _humanize(code: str) -> str:
    if not code:
        return ""
    for suffix in ("_day", "_night", "_polartwilight"):
        if code.endswith(suffix):
            code = code[: -len(suffix)]
            break
    return code.replace("_", " ")


async def fetch(lat: float, lon: float, location: str, hours: int = 12) -> HourlyResult:
    data = await get_json(_BASE, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                          headers=_HEADERS)
    ts = data.get("properties", {}).get("timeseries", [])
    entries: list[HourlyEntry] = []
    for item in ts[: max(hours, 0)]:
        d = item.get("data", {})
        det = d.get("instant", {}).get("details", {})
        n1 = d.get("next_1_hours", {})
        code = n1.get("summary", {}).get("symbol_code", "")
        precip = n1.get("details", {}).get("precipitation_amount")
        try:
            tm = datetime.fromisoformat(
                item.get("time", "").replace("Z", "+00:00")
            ).strftime("%I %p").lstrip("0")
        except Exception:  # noqa: BLE001
            tm = item.get("time", "")
        entries.append(HourlyEntry(
            time=tm,
            temp_c=det.get("air_temperature"),
            description=_humanize(code),
            precip_mm=precip,
            humidity=det.get("relative_humidity"),
            wind_kph=ms_to_kph(det.get("wind_speed")),
            wind_dir=deg_to_card(det.get("wind_from_direction")),
        ))
    return HourlyResult(source="MET Norway", location=location, hours=entries)
