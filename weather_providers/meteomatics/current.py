"""Meteomatics — current conditions."""
from __future__ import annotations
from datetime import datetime, timezone
from .._http import get_json
from ..base import WeatherResult
from ..base import deg_to_card
_B = "https://api.meteomatics.com"
_PARAMS = "t_2m:C,wind_speed_10m:kmh,wind_dir_10m:d,msl_pressure:hPa,relative_humidity_2m:p,dew_point_2m:C"
async def fetch(headers, lat, lon, location):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = await get_json(f"{_B}/{now}/{_PARAMS}/{lat},{lon}/json", headers=headers)
    vals = {}
    for item in data.get("data",[]):
        param = item.get("parameter","")
        coords = item.get("coordinates",[{}])
        if coords and coords[0].get("dates"): vals[param] = coords[0]["dates"][0].get("value")
    return WeatherResult(source="Meteomatics", temperature=vals.get("t_2m:C"), description="Current", location=location, humidity=vals.get("relative_humidity_2m:p"), wind_kph=vals.get("wind_speed_10m:kmh"), wind_dir=deg_to_card(vals.get("wind_dir_10m:d")), pressure_mb=vals.get("msl_pressure:hPa"), dewpoint_c=vals.get("dew_point_2m:C"))
