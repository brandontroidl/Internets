"""World Weather Online - daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
# fix: _float was duplicated in every endpoint file - moved to _codes.
from ._codes import _float

_B = "https://api.worldweatheronline.com/premium/v1"

async def fetch(key: str, lat: float, lon: float, location: str, days: int = 4) -> WeatherResult:
    data = await get_json(f"{_B}/weather.ashx", params={
        "key": key, "q": f"{lat},{lon}", "format": "json",
        "num_of_days": str(min(days, 14)), "fx": "yes", "cc": "yes",
    })
    d = data.get("data", {})
    fc = []
    for w in d.get("weather", [])[:days]:
        try: dn = datetime.fromisoformat(w.get("date", "")).strftime("%A")
        except Exception: dn = w.get("date", "")
        desc = w.get("hourly", [{}])[len(w.get("hourly", [])) // 2] if w.get("hourly") else {}
        desc_list = desc.get("weatherDesc", [{}])
        desc_text = desc_list[0].get("value", "N/A") if desc_list else "N/A"
        fc.append(ForecastDay(day_name=dn, high_c=_float(w.get("maxtempC")),
                               low_c=_float(w.get("mintempC")), description=desc_text))
    return WeatherResult(source="World Weather Online", temperature=None,
                         description="", location=location, forecast=fc)
