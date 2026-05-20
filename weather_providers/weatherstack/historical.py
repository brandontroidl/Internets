"""Weatherstack — historical weather (paid plans only)."""
from __future__ import annotations
from datetime import date, timedelta
from .._http import get_json
from ..base import HistoricalResult
_B = "http://api.weatherstack.com"
async def fetch(key, lat, lon, location, target_date=""):
    if not target_date: target_date = (date.today() - timedelta(days=1)).isoformat()
    data = await get_json(f"{_B}/historical", params={"access_key": key, "query": f"{lat},{lon}", "units": "m", "historical_date": target_date})
    hist = data.get("historical",{}).get(target_date,{})
    return HistoricalResult(source="Weatherstack", location=location, date=target_date, high_c=hist.get("maxtemp"), low_c=hist.get("mintemp"), avg_c=hist.get("avgtemp"), description="", precip_mm=hist.get("totalsnow"), avg_humidity=None)
