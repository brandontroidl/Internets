"""IQAir (AirVisual) — air quality (US AQI from nearest city station)."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category

_BASE = "https://api.airvisual.com/v2/nearest_city"


async def fetch(key, lat, lon, location):
    data = await get_json(_BASE, params={
        "lat": lat,
        "lon": lon,
        "key": key,
    })
    # Envelope: {"status": "success", "data": {...}}.  Anything other than
    # "success" (no nearby station, bad key, quota) means no usable reading —
    # raise so the dispatcher falls through to another provider.
    if not isinstance(data, dict) or data.get("status") != "success":
        raise HTTPError("IQAir: no AQI coverage for this location",
                        status=None, provider_hint="iqair")
    payload = data.get("data") or {}
    pollution = ((payload.get("current") or {}).get("pollution")) or {}
    aqi = pollution.get("aqius")
    if aqi is None:
        raise HTTPError("IQAir: no AQI value in response",
                        status=None, provider_hint="iqair")
    aqi = int(aqi)
    # The free nearest_city payload reports only the US AQI and the dominant
    # pollutant code (mainus, e.g. "p2"); no raw concentrations are included,
    # so pm25/pm10/etc. stay None.
    return AirQualityResult(source="IQAir", location=location,
                            aqi=aqi, category=aqi_category(aqi))
