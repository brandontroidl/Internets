"""WeatherAPI.com — historical weather."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult
_B = "https://api.weatherapi.com/v1"
async def fetch(key, lat, lon, location, target_date=""):
    if not target_date: target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(f"{_B}/history.json", params={"key": key, "q": f"{lat},{lon}", "dt": target_date})
    fds = data.get("forecast",{}).get("forecastday",[])
    if not fds: raise ValueError("No history data")
    d = fds[0].get("day",{})
    return HistoricalResult(source="WeatherAPI", location=location, date=target_date, high_c=d.get("maxtemp_c"), low_c=d.get("mintemp_c"), avg_c=d.get("avgtemp_c"), description=d.get("condition",{}).get("text",""), precip_mm=d.get("totalprecip_mm"), max_wind_kph=d.get("maxwind_kph"), avg_humidity=d.get("avghumidity"))
