"""WeatherKit — current conditions."""
from __future__ import annotations
from .._http import get_json
from ..base import WeatherResult
from ._codes import readable, deg_to_card
async def fetch(url, headers, location):
    data = await get_json(url, params={"dataSets": "currentWeather"}, headers=headers)
    c = data.get("currentWeather", {})
    hum = c.get("humidity"); hpct = (hum * 100) if hum is not None else None
    return WeatherResult(source="Apple Weather", temperature=c.get("temperature"), description=readable(c.get("conditionCode")), location=location, feels_like_c=c.get("temperatureApparent"), humidity=hpct, wind_kph=c.get("windSpeed"), wind_dir=deg_to_card(c.get("windDirection")), pressure_mb=c.get("pressure"), visibility_m=c.get("visibility"), dewpoint_c=c.get("temperatureDewPoint"))
