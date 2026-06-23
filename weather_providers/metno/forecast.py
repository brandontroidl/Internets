"""MET Norway — daily forecast (grouped from compact timeseries)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay

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


async def fetch(lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    data = await get_json(_BASE, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                          headers=_HEADERS)
    ts = data.get("properties", {}).get("timeseries", [])

    # Group timeseries by calendar date -> {date: {temps:[], midday_code:str}}.
    groups: dict[str, dict] = {}
    order: list[str] = []
    cur_temp = None
    cur_code = ""
    for idx, item in enumerate(ts):
        t = item.get("time", "")
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        d = item.get("data", {})
        det = d.get("instant", {}).get("details", {})
        temp = det.get("air_temperature")
        code = d.get("next_1_hours", {}).get("summary", {}).get("symbol_code", "") \
            or d.get("next_6_hours", {}).get("summary", {}).get("symbol_code", "")
        if idx == 0:
            cur_temp, cur_code = temp, code
        key = dt.strftime("%Y-%m-%d")
        g = groups.get(key)
        if g is None:
            g = {"name": dt.strftime("%A"), "temps": [], "code": ""}
            groups[key] = g
            order.append(key)
        if temp is not None:
            g["temps"].append(temp)
        # Pick the symbol nearest midday as the day's representative.
        if 11 <= dt.hour <= 13 and code:
            g["code"] = code
        elif not g["code"] and code:
            g["code"] = code

    fc: list[ForecastDay] = []
    for key in order[: max(days, 0)]:
        g = groups[key]
        temps = g["temps"]
        fc.append(ForecastDay(
            day_name=g["name"],
            high_c=max(temps) if temps else None,
            low_c=min(temps) if temps else None,
            description=_humanize(g["code"]) or "N/A",
        ))
    return WeatherResult(
        source="MET Norway",
        temperature=cur_temp,
        description=_humanize(cur_code) or "N/A",
        location=location,
        forecast=fc,
    )
