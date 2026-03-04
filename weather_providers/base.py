"""Base types for the multi-provider weather system.

Every provider module implements ``WeatherProvider`` and returns
``WeatherResult`` / ``ForecastDay`` dataclasses.  The normalized
structure lets the bot display weather from any source identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ForecastDay:
    """Single day in a multi-day forecast."""
    day_name: str              # e.g. "Monday"
    high_c: float | None       # Celsius
    low_c: float | None        # Celsius
    description: str           # e.g. "Partly Cloudy"


@dataclass(frozen=True, slots=True)
class WeatherResult:
    """Normalized current-weather response from any provider.

    All temperatures are in Celsius.  Wind is in km/h.  Pressure in
    millibars.  Visibility in metres.  ``forecast`` is optional — only
    populated when a forecast was explicitly requested.
    """
    source: str                        # e.g. "Open-Meteo", "WeatherAPI"
    temperature: float | None          # Celsius
    description: str                   # e.g. "Partly Cloudy"
    location: str                      # display name from geocoding
    feels_like_c: float | None  = None
    humidity: float | None      = None # percentage 0-100
    wind_kph: float | None      = None
    wind_dir: str               = ""   # cardinal, e.g. "NNW"
    pressure_mb: float | None   = None
    visibility_m: float | None  = None
    dewpoint_c: float | None    = None
    forecast: list[ForecastDay] = field(default_factory=list)


@runtime_checkable
class WeatherProvider(Protocol):
    """Interface every weather provider must implement.

    ``name``
        Human-readable provider name (e.g. "Open-Meteo").

    ``requires_key``
        True if the provider needs an API key to function.

    ``async get_weather(lat, lon, location, **kwargs) -> WeatherResult``
        Fetch current conditions.  Must raise on failure (the registry
        catches exceptions and falls through to the next provider).

    ``async get_forecast(lat, lon, location, days, **kwargs) -> WeatherResult``
        Fetch a multi-day forecast.  ``days`` defaults to 4.  The
        ``forecast`` field of the returned ``WeatherResult`` is populated.
    """
    name: str
    requires_key: bool

    async def get_weather(
        self, lat: float, lon: float, location: str, **kwargs: object
    ) -> WeatherResult: ...

    async def get_forecast(
        self, lat: float, lon: float, location: str,
        days: int = 4, **kwargs: object
    ) -> WeatherResult: ...
