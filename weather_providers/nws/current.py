"""NWS Weather.gov - current conditions from nearest observation station.

https://api.weather.gov/
Free, no API key.  US locations only.  Requires User-Agent header.
"""
from __future__ import annotations
from ._scope import OutOfCoverage, nws_json as get_json
from ..base import WeatherResult
from ._codes import deg_to_card, ms_to_kph

_HEADERS = {"User-Agent": "(Internets IRC Bot, github.com/brandontroidl/Internets)", "Accept": "application/geo+json"}

async def _get_station(lat: float, lon: float) -> str:
    """Get the nearest observation station URL from the NWS points API."""
    data = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_HEADERS)
    obs_url = data.get("properties", {}).get("observationStations")
    if not obs_url:
        raise OutOfCoverage("NWS: no observation stations for this location")
    stations = await get_json(obs_url, headers=_HEADERS)
    features = stations.get("features", [])
    if not features:
        raise OutOfCoverage("NWS: empty station list")
    return features[0].get("id", "")

async def fetch(lat: float, lon: float, location: str) -> WeatherResult:
    station_url = await _get_station(lat, lon)
    data = await get_json(f"{station_url}/observations/latest", headers=_HEADERS)
    p = data.get("properties", {})
    def _val(key):
        v = p.get(key, {})
        return v.get("value") if isinstance(v, dict) else v
    temp = _val("temperature")
    wind_speed = _val("windSpeed")
    return WeatherResult(
        source="NWS", temperature=temp,
        description=(p.get("textDescription") or ""),
        location=location,
        humidity=_val("relativeHumidity"),
        wind_kph=wind_speed,
        wind_dir=deg_to_card(_val("windDirection")),
        pressure_mb=(_val("barometricPressure") / 100) if _val("barometricPressure") else None,
        visibility_m=_val("visibility"),
        dewpoint_c=_val("dewpoint"),
    )
