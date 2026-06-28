"""Weatherstack - forecast (paid plans only)."""
from __future__ import annotations
from datetime import datetime
from .._http import get_json
from ..base import WeatherResult, ForecastDay
from .current import _check_envelope
# fix: was http:// - leaked access_key in plaintext query string.
_B = "https://api.weatherstack.com"
async def fetch(key, lat, lon, location, days=4):
    data = await get_json(f"{_B}/forecast", params={"access_key": key, "query": f"{lat},{lon}", "units": "m", "forecast_days": min(days,7)})
    # fix: detect {"success":false,"error":...} envelope (was silently swallowed).
    _check_envelope(data)
    fc_data = data.get("forecast",{})
    fc = []
    for dt_str in sorted(fc_data.keys())[:days]:
        d = fc_data[dt_str]
        try: dn = datetime.fromisoformat(dt_str).strftime("%A")
        except Exception: dn = dt_str
        fc.append(ForecastDay(day_name=dn, high_c=d.get("maxtemp"), low_c=d.get("mintemp"), description=d.get("hourly",[{}])[len(d.get("hourly",[]))//2].get("weather_descriptions",["N/A"])[0] if d.get("hourly") else "N/A"))
    return WeatherResult(source="Weatherstack", temperature=None, description="", location=location, forecast=fc)
