"""AccuWeather - daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
# fix: was http:// - leaked apikey in query string on the wire.
_B = "https://dataservice.accuweather.com"
async def fetch(key, loc_key, location, days=4):
    # fix: dead-code `ep = "5day" if days <= 5 else "5day"` - both
    # branches returned the same value. Free tier only exposes 5-day;
    # 10/15-day are paid plans not yet wired up here. Hardcode.
    ep = "5day"
    data = await get_json(f"{_B}/forecasts/v1/daily/{ep}/{loc_key}", params={"apikey": key, "metric": "true"})
    fc = []
    for d in data.get("DailyForecasts",[])[:days]:
        try: dn = datetime.fromisoformat(d.get("Date","").replace("Z","+00:00") if "Z" in d.get("Date","") else d.get("Date","")).strftime("%A")
        except Exception: dn = d.get("Date","")[:10]
        hi = d.get("Temperature",{}).get("Maximum",{}).get("Value")
        lo = d.get("Temperature",{}).get("Minimum",{}).get("Value")
        desc = d.get("Day",{}).get("IconPhrase","N/A")
        fc.append(ForecastDay(day_name=dn, high_c=hi, low_c=lo, description=desc))
    return WeatherResult(source="AccuWeather", temperature=None, description="", location=location, forecast=fc)
