"""OpenAQ - air quality from the nearest monitoring location (v3 API).

Two calls: find the nearest location (with its sensor list), then pull
that location's latest measurements.  The latest endpoint returns only
{value, sensorsId}, so we map each measurement back to a pollutant via
the sensors[] list from the locations response.
"""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category
from ..purpleair._codes import pm25_to_aqi

_BASE = "https://api.openaq.org/v3/locations"
_RADIUS_M = 25000  # 25 km search radius (v3 max is 25000)

# OpenAQ parameter name -> AirQualityResult field.
_PARAM_MAP = {
    "pm25": "pm25",
    "pm10": "pm10",
    "o3": "o3",
    "no2": "no2",
    "so2": "so2",
    "co": "co",
}


async def fetch(key, lat, lon, location):
    headers = {"X-API-Key": key}
    # 1) Nearest monitoring location and its sensor inventory.
    loc_data = await get_json(
        _BASE,
        headers=headers,
        params={
            "coordinates": f"{lat},{lon}",
            "radius": _RADIUS_M,
            "limit": 1,
            "order_by": "distance",
        },
    )
    results = loc_data.get("results") or []
    if not results or not isinstance(results[0], dict):
        raise HTTPError("OpenAQ: no station within range",
                        status=None, provider_hint="openaq")
    loc = results[0]
    loc_id = loc.get("id")
    if loc_id is None:
        raise HTTPError("OpenAQ: station has no id",
                        status=None, provider_hint="openaq")

    # sensorsId -> OpenAQ parameter name (e.g. "pm25").
    sensor_param: dict[int, str] = {}
    for s in loc.get("sensors") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        pname = ((s.get("parameter") or {}).get("name") or "").lower()
        if sid is not None and pname:
            sensor_param[sid] = pname

    # 2) Latest measurement per sensor (raw concentration).
    latest_data = await get_json(
        f"{_BASE}/{loc_id}/latest",
        headers=headers,
    )
    measurements = latest_data.get("results") or []
    if not measurements:
        raise HTTPError("OpenAQ: no recent measurements",
                        status=None, provider_hint="openaq")

    pollutants: dict[str, float] = {}
    for m in measurements:
        if not isinstance(m, dict):
            continue
        val = m.get("value")
        pname = sensor_param.get(m.get("sensorsId"))
        field = _PARAM_MAP.get(pname) if pname else None
        if field is None or val is None:
            continue
        try:
            pollutants[field] = float(val)
        except (TypeError, ValueError):
            continue

    if not pollutants:
        raise HTTPError("OpenAQ: no usable pollutant readings",
                        status=None, provider_hint="openaq")

    # Derive AQI from PM2.5 (the standard single-number basis) when present.
    pm25 = pollutants.get("pm25")
    aqi = pm25_to_aqi(pm25)
    category = aqi_category(aqi) if aqi is not None else ""

    name = loc.get("name") or ""
    source = f"OpenAQ ({name})" if name else "OpenAQ"
    return AirQualityResult(
        source=source, location=location,
        aqi=aqi, category=category,
        pm25=round(pm25, 1) if pm25 is not None else None,
        pm10=_r(pollutants.get("pm10")),
        o3=_r(pollutants.get("o3")),
        no2=_r(pollutants.get("no2")),
        so2=_r(pollutants.get("so2")),
        co=_r(pollutants.get("co")),
    )


def _r(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None
