"""Tomorrow.io — air quality."""
from __future__ import annotations
from .._http import get_json
from ..base import AirQualityResult, aqi_category
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/weather/realtime", params={"apikey": key, "location": f"{lat},{lon}", "units": "metric"})
    v = data.get("data",{}).get("values",{})
    aqi = v.get("epaIndex"); aqi_int = int(aqi) if aqi is not None else None
    return AirQualityResult(source="Tomorrow.io", location=location, aqi=aqi_int, category=aqi_category(aqi_int), pm25=v.get("particulateMatter25"), pm10=v.get("particulateMatter10"), o3=v.get("pollutantO3"), no2=v.get("pollutantNO2"), so2=v.get("pollutantSO2"), co=v.get("pollutantCO"))
