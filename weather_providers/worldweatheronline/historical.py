"""World Weather Online — historical weather (past weather API)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult

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
    return HistoricalResult(
        source="World Weather Online", location=location, date=target_date,
        high_c=_float(w.get("maxtempC")), low_c=_float(w.get("mintempC")),
        avg_c=_float(w.get("avgtempC")),
        description="",
        precip_mm=_float(w.get("totalSnow_cm")),  # WWO uses totalSnow_cm for some reason
        avg_humidity=None,
    )

def _float(v):
    try: return float(v)
    except (TypeError, ValueError): return None
