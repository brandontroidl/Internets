"""PurpleAir - air quality from the nearest outdoor community sensor."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AirQualityResult, aqi_category, haversine_km as _haversine_km
from ._codes import pm25_to_aqi, epa_correct

_BASE = "https://api.purpleair.com/v1/sensors"
_FIELDS = "latitude,longitude,pm2.5,humidity"
_BOX = 0.1  # bounding-box half-size in degrees (~11 km) around the point


async def fetch(key, lat, lon, location):
    data = await get_json(
        _BASE,
        headers={"X-API-Key": key},
        params={
            "fields": _FIELDS,
            "location_type": 0,          # outdoor sensors only
            "max_age": 3600,             # seen within the last hour
            "nwlng": lon - _BOX, "nwlat": lat + _BOX,
            "selng": lon + _BOX, "selat": lat - _BOX,
        },
    )
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        raise HTTPError("PurpleAir: no nearby sensor",
                        status=None, provider_hint="purpleair")
    idx = {name: i for i, name in enumerate(fields)}

    def _get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    # Pick the closest sensor that actually reported a PM2.5 value.
    best = None
    best_km = None
    for row in rows:
        slat, slon, pm = _get(row, "latitude"), _get(row, "longitude"), _get(row, "pm2.5")
        if slat is None or slon is None or pm is None:
            continue
        d = _haversine_km(lat, lon, slat, slon)
        if best_km is None or d < best_km:
            best, best_km = row, d
    if best is None:
        raise HTTPError("PurpleAir: no nearby sensor with PM2.5",
                        status=None, provider_hint="purpleair")

    pm_corr = epa_correct(_get(best, "pm2.5"), _get(best, "humidity"))
    aqi = pm25_to_aqi(pm_corr)
    # Provenance matters for crowdsourced data - surface the sensor distance
    # so users know how local the reading is (fits the 30-char source cap).
    source = f"PurpleAir ~{best_km:.0f}km" if best_km is not None else "PurpleAir"
    return AirQualityResult(
        source=source, location=location,
        aqi=aqi, category=aqi_category(aqi),
        pm25=round(pm_corr, 1) if pm_corr is not None else None,
    )
