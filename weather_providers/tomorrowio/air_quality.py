"""Tomorrow.io - air quality."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category
_B = "https://api.tomorrow.io/v4"
async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/weather/realtime", params={"apikey": key, "location": f"{lat},{lon}", "units": "metric"})
    v = data.get("data",{}).get("values",{})
    aqi = v.get("epaIndex"); aqi_int = int(aqi) if aqi is not None else None
    pm25=v.get("particulateMatter25"); pm10=v.get("particulateMatter10")
    o3=v.get("pollutantO3"); no2=v.get("pollutantNO2"); so2=v.get("pollutantSO2"); co=v.get("pollutantCO")
    # weather/realtime only returns AQ fields on entitled plans; otherwise
    # every value is None.  Raise so the dispatcher falls through to a
    # provider that has data instead of returning a hollow result.
    if aqi_int is None and all(x is None for x in (pm25, pm10, o3, no2, so2, co)):
        raise HTTPError("Tomorrow.io: no air-quality data in response", status=None, provider_hint="tomorrowio")
    return AirQualityResult(source="Tomorrow.io", location=location, aqi=aqi_int, category=aqi_category(aqi_int), pm25=pm25, pm10=pm10, o3=o3, no2=no2, so2=so2, co=co)
