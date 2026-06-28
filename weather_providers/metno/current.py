"""MET Norway - current conditions (locationforecast/2.0/compact)."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult, deg_to_card, ms_to_kph

_BASE = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# api.met.no rejects missing/generic User-Agent with 403.
_HEADERS = {"User-Agent": "Internets-IRC-Bot/2.x github.com/brandontroidl/Internets"}


def _humanize(code: str) -> str:
    """"partlycloudy_day" -> "partlycloudy"; underscores -> spaces."""
    if not code:
        return ""
    for suffix in ("_day", "_night", "_polartwilight"):
        if code.endswith(suffix):
            code = code[: -len(suffix)]
            break
    return code.replace("_", " ")


async def fetch(lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(_BASE, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                          headers=_HEADERS)
    ts = data.get("properties", {}).get("timeseries", [])
    if not ts:
        return WeatherResult(source="MET Norway", temperature=None,
                             description="Unknown", location=location)
    entry = ts[0].get("data", {})
    det = entry.get("instant", {}).get("details", {})
    n1 = entry.get("next_1_hours", {})
    code = n1.get("summary", {}).get("symbol_code", "")
    return WeatherResult(
        source="MET Norway",
        temperature=det.get("air_temperature"),
        description=_humanize(code) or "Unknown",
        location=location,
        humidity=det.get("relative_humidity"),
        wind_kph=ms_to_kph(det.get("wind_speed")),
        wind_dir=deg_to_card(det.get("wind_from_direction")),
        pressure_mb=det.get("air_pressure_at_sea_level"),
        dewpoint_c=det.get("dew_point_temperature"),
    )
