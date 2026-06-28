"""AirNow - air quality (current observations, US EPA AQI)."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category

_BASE = "https://www.airnowapi.org/aq/observation/latLong/current/"


async def fetch(key, lat, lon, location):
    data = await get_json(_BASE, params={
        "format": "application/json",
        "latitude": lat,
        "longitude": lon,
        "distance": 25,          # search radius in miles
        "API_KEY": key,
    })
    # AirNow returns one observation object per pollutant (O3, PM2.5, PM10).
    # An empty list means no monitors near this point - typically outside the
    # US.  Raise so the dispatcher falls through to a global provider instead
    # of returning a misleading "AQI N/A" (mirrors NWS's US-only behaviour).
    if not isinstance(data, list) or not data:
        raise HTTPError("AirNow: no AQI coverage for this location",
                        status=None, provider_hint="airnow")
    # Overall AQI is the dominant (highest) pollutant sub-index - the EPA
    # convention for a single reported number.
    dominant = max(
        (o for o in data if isinstance(o, dict) and o.get("AQI") is not None),
        key=lambda o: o.get("AQI", -1),
        default=None,
    )
    if dominant is None:
        raise HTTPError("AirNow: no AQI value in response",
                        status=None, provider_hint="airnow")
    aqi = int(dominant["AQI"])
    param = str(dominant.get("ParameterName", "")).strip()
    category = (dominant.get("Category") or {}).get("Name") or aqi_category(aqi)
    # AirNow reports per-pollutant AQI sub-indices, not raw concentrations,
    # so pm25/pm10/etc. stay None - we surface the dominant pollutant in the
    # source label instead (standard AQI reporting, e.g. "AirNow (PM2.5)").
    source = f"AirNow ({param})" if param else "AirNow"
    return AirQualityResult(source=source, location=location,
                            aqi=aqi, category=category)
