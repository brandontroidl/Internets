"""Pirate Weather - current conditions."""
from __future__ import annotations
from ..base import WeatherResult
# fix: key embedded in URL path leaks into HTTPError messages - use
# safe_get_json wrapper which redacts the key before re-raising.
from ._codes import deg_to_card, icon_to_desc, ms_to_kph, safe_get_json

_BASE = "https://api.pirateweather.net/forecast"

async def fetch(key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await safe_get_json(f"{_BASE}/{key}/{lat},{lon}", key,
                          params={"units": "si", "exclude": "minutely,hourly,daily,alerts"})
    c = data.get("currently", {})
    return WeatherResult(
        source="Pirate Weather", temperature=c.get("temperature"),
        description=icon_to_desc(c.get("icon")), location=location,
        feels_like_c=c.get("apparentTemperature"),
        humidity=(c["humidity"] * 100) if c.get("humidity") is not None else None,
        wind_kph=ms_to_kph(c.get("windSpeed")),
        wind_dir=deg_to_card(c.get("windBearing")),
        pressure_mb=c.get("pressure"),
        visibility_m=(c["visibility"] * 1000) if c.get("visibility") is not None else None,
        dewpoint_c=c.get("dewPoint"),
    )
