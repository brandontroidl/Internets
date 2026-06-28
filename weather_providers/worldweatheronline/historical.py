"""World Weather Online - historical weather (past weather API)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult
# fix: _float was duplicated in every endpoint file - moved to _codes.
from ._codes import _float

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str, target_date: str = "") -> HistoricalResult:
    if not target_date:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(f"{_B}/past-weather.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json",
        "date": target_date, "enddate": target_date,
    })
    weather = data.get("data", {}).get("weather", [])
    if not weather:
        raise ValueError("WWO: no historical data")
    w = weather[0]
    # fix: was reading totalSnow_cm into precip_mm - that's snowfall not
    # precip, and the unit is centimetres. WWO past-weather returns
    # precipMM on each hourly entry; sum those for daily precip.
    hourly_entries = w.get("hourly") or []
    precip_values = []
    for h in hourly_entries:
        if isinstance(h, dict):
            v = _float(h.get("precipMM"))
            if v is not None:
                precip_values.append(v)
    precip_mm = sum(precip_values) if precip_values else None
    return HistoricalResult(
        source="World Weather Online", location=location, date=target_date,
        high_c=_float(w.get("maxtempC")), low_c=_float(w.get("mintempC")),
        avg_c=_float(w.get("avgtempC")),
        description="",
        precip_mm=precip_mm,
        avg_humidity=None,
    )
