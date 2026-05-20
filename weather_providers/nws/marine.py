"""NWS — marine forecast (coastal and offshore zones).

Uses the NWS marine forecast zones API for wave/wind data.
Only works for US coastal locations.
"""
from __future__ import annotations
from .._http import get_json
from ..base import MarineResult

_HEADERS = {"User-Agent": "(Internets IRC Bot)", "Accept": "application/geo+json"}

async def fetch(lat: float, lon: float, location: str) -> MarineResult:
    # NWS doesn't have a structured marine current-conditions API like Open-Meteo.
    # We query the marine zone forecast for textual wave data.
    pts = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_HEADERS)
    zone_url = pts.get("properties", {}).get("forecastZone", "")
    if not zone_url or "/marine/" not in zone_url.lower():
        raise ValueError("NWS: location is not in a marine zone")
    data = await get_json(f"{zone_url}/forecast", headers=_HEADERS)
    periods = data.get("properties", {}).get("periods", [])
    # NWS marine forecasts are textual — extract what we can.
    # Return a minimal result; the text is in the first period's detailedForecast.
    if not periods:
        raise ValueError("NWS: no marine forecast data")
    return MarineResult(source="NWS", location=location)
