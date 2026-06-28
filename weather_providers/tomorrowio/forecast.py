"""Tomorrow.io - daily forecast."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
from ._codes import CODES
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location, days=4):
    data = await get_json(f"{_B}/weather/forecast", params={"apikey": key, "location": f"{lat},{lon}", "units": "metric", "timesteps": "1d"})
    tl = data.get("timelines",{}).get("daily",[])
    fc = []
    for e in tl[:days]:
        v = e.get("values",{})
        try: dn = datetime.fromisoformat(e.get("time","").replace("Z","+00:00")).strftime("%A")
        except Exception: dn = e.get("time","")[:10]
        wc = v.get("weatherCodeMax") or v.get("weatherCode")
        fc.append(ForecastDay(day_name=dn, high_c=v.get("temperatureMax"), low_c=v.get("temperatureMin"), description=CODES.get(wc,"N/A") if wc is not None else "N/A"))
    cv = tl[0].get("values",{}) if tl else {}
    cc = cv.get("weatherCodeMax")
    return WeatherResult(source="Tomorrow.io", temperature=cv.get("temperatureAvg"), description=CODES.get(cc,"N/A") if cc else "N/A", location=location, forecast=fc)
