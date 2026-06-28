"""WAQI — air quality (nearest-station AQI, World Air Quality Index)."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category

_BASE = "https://api.waqi.info/feed/geo:{lat};{lon}/"


async def fetch(key, lat, lon, location):
    url = _BASE.format(lat=lat, lon=lon)
    data = await get_json(url, params={"token": key})
    # WAQI wraps everything in {"status": "ok"/"error", "data": {...}}.
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise HTTPError("WAQI: no AQI coverage for this location",
                        status=None, provider_hint="waqi")
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise HTTPError("WAQI: malformed response",
                        status=None, provider_hint="waqi")
    # data.aqi is an int, or "-" when the station has no current value.
    raw = payload.get("aqi")
    if raw is None or raw == "-":
        raise HTTPError("WAQI: no AQI value for this location",
                        status=None, provider_hint="waqi")
    try:
        aqi = int(raw)
    except (TypeError, ValueError):
        raise HTTPError("WAQI: non-numeric AQI value",
                        status=None, provider_hint="waqi")
    # iaqi values are AQI sub-indices (not µg/m3 concentrations), so the
    # pm25/pm10/etc. concentration fields stay None.  We surface the
    # reporting station name in the source label when available.
    city = (payload.get("city") or {}).get("name") if isinstance(
        payload.get("city"), dict) else None
    source = f"WAQI ({city})" if city else "WAQI"
    return AirQualityResult(source=source, location=location,
                            aqi=aqi, category=aqi_category(aqi))
