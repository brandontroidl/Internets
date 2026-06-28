"""Open-Meteo - air quality endpoint."""
from __future__ import annotations
from .._http import get_json
from ..base import AirQualityResult, aqi_category

_BASE = "https://air-quality-api.open-meteo.com/v1/air-quality"

async def fetch(lat: float, lon: float, location: str) -> AirQualityResult:
    data = await get_json(_BASE, params={"latitude": lat, "longitude": lon, "current": "us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,aerosol_optical_depth", "timezone": "auto"})
    c = data.get("current", {})
    aqi = c.get("us_aqi")
    aqi_int = int(aqi) if aqi is not None else None
    return AirQualityResult(source="Open-Meteo", location=location, aqi=aqi_int, category=aqi_category(aqi_int), pm25=c.get("pm2_5"), pm10=c.get("pm10"), o3=c.get("ozone"), no2=c.get("nitrogen_dioxide"), so2=c.get("sulphur_dioxide"), co=c.get("carbon_monoxide"), aod=c.get("aerosol_optical_depth"))
