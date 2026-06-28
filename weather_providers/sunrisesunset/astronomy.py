"""SunriseSunset.io — astronomy (sun/moon).  Times are 12-hour local strings."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AstronomyResult

_BASE = "https://api.sunrisesunset.io/json"


def _tof(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


async def fetch(lat, lon, location):
    # NOTE: param is "lng", not "lon".
    data = await get_json(_BASE, params={"lat": lat, "lng": lon})
    if not isinstance(data, dict) or data.get("status") != "OK":
        raise HTTPError("SunriseSunset: bad status in response",
                        status=None, provider_hint="sunrisesunset")
    r = data.get("results") or {}
    return AstronomyResult(
        source="SunriseSunset",
        location=location,
        sunrise=r.get("sunrise", ""),
        sunset=r.get("sunset", ""),
        day_length=r.get("day_length", ""),
        moonrise=r.get("moonrise", ""),
        moonset=r.get("moonset", ""),
        moon_phase=r.get("moon_phase", ""),
        moon_illumination=_tof(r.get("moon_illumination")),
    )
