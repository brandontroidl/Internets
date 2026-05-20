"""Weatherstack — current conditions."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import WeatherResult
# fix: was http:// — leaked access_key in plaintext query string.
_B = "https://api.weatherstack.com"


def _check_envelope(data, provider_hint="api.weatherstack.com"):
    """Weatherstack signals failure via ``{"success": false, "error": {...}}``
    on a 200 OK response.  Translate that into the usual HTTPError so the
    dispatcher sees it like any other upstream failure."""
    # fix: previously no detection of the {"success":false,"error":...}
    # envelope — failures were silently treated as empty data.
    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("error") or {}
        code = err.get("code")
        info = err.get("info") or err.get("type") or "weatherstack error"
        raise HTTPError(
            f"Weatherstack API error code={code}: {info}",
            status=None,
            provider_hint=provider_hint,
            is_rate_limit=(code in (104, 105)),  # usage_limit_reached / function_access_restricted
        )


async def fetch(key, lat, lon, location):
    data = await get_json(f"{_B}/current", params={"access_key": key, "query": f"{lat},{lon}", "units": "m"})
    _check_envelope(data)
    c = data.get("current",{})
    desc_list = c.get("weather_descriptions",[])
    return WeatherResult(source="Weatherstack", temperature=c.get("temperature"), description=desc_list[0] if desc_list else "Unknown", location=location, feels_like_c=c.get("feelslike"), humidity=c.get("humidity"), wind_kph=c.get("wind_speed"), wind_dir=c.get("wind_dir",""), pressure_mb=c.get("pressure"), visibility_m=(c["visibility"]*1000) if c.get("visibility") is not None else None, dewpoint_c=None)
