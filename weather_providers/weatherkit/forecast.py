"""WeatherKit — daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
from ._codes import readable
async def fetch(url, headers, location, days=4):
    data = await get_json(url, params={"dataSets": "currentWeather,forecastDaily"}, headers=headers)
    cw = data.get("currentWeather", {}); fd = data.get("forecastDaily", {})
    fc = []
    for e in fd.get("days", [])[:days]:
        try: dn = datetime.fromisoformat(e.get("forecastStart","").replace("Z","+00:00")).strftime("%A")
        except Exception: dn = e.get("forecastStart","")[:10]
        fc.append(ForecastDay(day_name=dn, high_c=e.get("temperatureMax"), low_c=e.get("temperatureMin"), description=readable(e.get("conditionCode"))))
    return WeatherResult(source="Apple Weather", temperature=cw.get("temperature"), description=readable(cw.get("conditionCode")), location=location, forecast=fc)
