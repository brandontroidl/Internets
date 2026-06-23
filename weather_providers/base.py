"""Base types for the multi-provider weather system.

Every provider module implements ``WeatherProvider`` and returns normalized
dataclasses.  The registry in ``__init__.py`` handles fallback chains —
providers that don't support a given data type simply omit the method.

Data types
----------
WeatherResult / ForecastDay   — current conditions + daily forecast
HourlyResult  / HourlyEntry   — hourly forecast (temperature, precip, wind)
AlertsResult  / AlertEntry    — active weather alerts and warnings
AirQualityResult              — AQI index, PM2.5, ozone, pollutants
AstronomyResult               — sunrise, sunset, moon phase, illumination
HistoricalResult              — weather on a past date
MarineResult                  — wave height, swell, water temperature
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Protocol, runtime_checkable


# ── Shared helpers (used by all provider endpoint sub-modules) ───────

_DIRS = ("N","NNE","NE","ENE","E","ESE","SE","SSE",
         "S","SSW","SW","WSW","W","WNW","NW","NNW")

def deg_to_card(deg: float | None) -> str:
    """Convert wind direction in degrees to 16-point cardinal abbreviation."""
    if deg is None: return ""
    return _DIRS[round(deg / 22.5) % 16]

def ms_to_kph(v: float | None) -> float | None:
    """Convert meters/second to km/h."""
    return v * 3.6 if v is not None else None

def km_to_m(v: float | None) -> float | None:
    """Convert kilometers to meters."""
    return v * 1000 if v is not None else None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers.

    Clamps ``sqrt(a)`` to 1.0 so float rounding on near-antipodal points
    can't push ``asin()`` out of domain (ValueError).  Shared by the
    providers that pick the nearest sensor/station/event.
    """
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * 6371.0 * asin(min(1.0, sqrt(a)))


# ── Current conditions + daily forecast ──────────────────────────────

@dataclass(frozen=True, slots=True)
class ForecastDay:
    """Single day in a multi-day forecast."""
    day_name: str
    high_c: float | None
    low_c: float | None
    description: str


@dataclass(frozen=True, slots=True)
class WeatherResult:
    """Normalized current-weather response from any provider."""
    source: str
    temperature: float | None
    description: str
    location: str
    feels_like_c: float | None  = None
    humidity: float | None      = None
    wind_kph: float | None      = None
    wind_dir: str               = ""
    pressure_mb: float | None   = None
    visibility_m: float | None  = None
    dewpoint_c: float | None    = None
    forecast: list[ForecastDay] = field(default_factory=list)


# ── Hourly forecast ──────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HourlyEntry:
    """Single hour in an hourly forecast."""
    time: str                              # e.g. "3 PM", "15:00"
    temp_c: float | None       = None
    description: str           = ""
    precip_mm: float | None    = None
    precip_chance: float | None = None     # 0-100
    humidity: float | None     = None
    wind_kph: float | None     = None
    wind_dir: str              = ""


@dataclass(frozen=True, slots=True)
class HourlyResult:
    """Hourly forecast from a weather provider."""
    source: str
    location: str
    hours: list[HourlyEntry] = field(default_factory=list)


# ── Weather alerts ───────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AlertEntry:
    """Single weather alert or warning."""
    event: str
    severity: str              # extreme / severe / moderate / minor / unknown
    headline: str
    start: str          = ""
    end: str            = ""
    description: str    = ""


@dataclass(frozen=True, slots=True)
class AlertsResult:
    """Active weather alerts for a location."""
    source: str
    location: str
    alerts: list[AlertEntry] = field(default_factory=list)


# ── Air quality ──────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AirQualityResult:
    """Air quality index and pollutant concentrations (μg/m³)."""
    source: str
    location: str
    aqi: int | None            = None   # US EPA AQI 0-500
    category: str              = ""     # Good / Moderate / Unhealthy / ...
    pm25: float | None         = None
    pm10: float | None         = None
    o3: float | None           = None
    no2: float | None          = None
    so2: float | None          = None
    co: float | None           = None
    aod: float | None          = None   # aerosol optical depth (550nm) — smoke proxy


# ── Astronomy ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AstronomyResult:
    """Sun and moon data for a location and date."""
    source: str
    location: str
    sunrise: str               = ""
    sunset: str                = ""
    day_length: str            = ""
    moonrise: str              = ""
    moonset: str               = ""
    moon_phase: str            = ""
    moon_illumination: float | None = None  # 0-100


# ── Historical weather ───────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HistoricalResult:
    """Weather data for a specific past date."""
    source: str
    location: str
    date: str                  = ""
    high_c: float | None       = None
    low_c: float | None        = None
    avg_c: float | None        = None
    description: str           = ""
    precip_mm: float | None    = None
    max_wind_kph: float | None = None
    avg_humidity: float | None = None


# ── Marine weather ───────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MarineResult:
    """Ocean and coastal weather conditions."""
    source: str
    location: str
    wave_height_m: float | None      = None
    wave_period_s: float | None      = None
    wave_direction: str              = ""
    swell_height_m: float | None     = None
    swell_period_s: float | None     = None
    swell_direction: str             = ""
    water_temp_c: float | None       = None
    wind_wave_height_m: float | None = None


# ── Precipitation nowcast ────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NowcastEntry:
    """Single time step in a precipitation nowcast."""
    time: str                          # e.g. "3:15 PM"
    precip_mm: float | None    = None  # mm in this interval
    precip_type: str           = ""    # "rain", "snow", "none"
    intensity: str             = ""    # "none", "light", "moderate", "heavy"


@dataclass(frozen=True, slots=True)
class NowcastResult:
    """Short-range precipitation nowcast (next 1-2 hours)."""
    source: str
    location: str
    summary: str               = ""    # e.g. "Rain starting in 15 minutes"
    entries: list[NowcastEntry] = field(default_factory=list)


# ── UV index ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UVResult:
    """UV index now + today's peak."""
    source: str
    location: str
    uv_index: float | None     = None   # current UV index
    uv_max: float | None       = None   # today's max UV index
    category: str              = ""     # Low / Moderate / High / Very High / Extreme


# ── Pollen ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PollenResult:
    """Pollen / allergy forecast.

    Three provider data models are normalised here; the formatter renders
    whichever fields a provider populated:

    * Open-Meteo (CAMS, Europe): per-species concentrations in grains/m³
      (``alder`` … ``ragweed``).
    * Google Pollen (global): tree/grass/weed Universal Pollen Index (0-5).
    * Pollen.com / IQVIA (US): a single overall index (0-12) + ``category``
      and the dominant ``triggers`` (allergen names).
    """
    source: str
    location: str
    # Open-Meteo CAMS per-species, grains/m³
    alder: float | None        = None
    birch: float | None        = None
    grass: float | None        = None
    mugwort: float | None      = None
    olive: float | None        = None
    ragweed: float | None      = None
    # Google Pollen — tree/grass/weed index (0-5 Universal Pollen Index)
    tree_index: float | None   = None
    grass_index: float | None  = None
    weed_index: float | None   = None
    # Pollen.com / IQVIA — overall index (0-12) + dominant allergens
    overall_index: float | None = None
    category: str              = ""
    triggers: tuple[str, ...]  = ()


def pollen_cat_12(idx: float | None) -> str:
    """Category for the IQVIA / Pollen.com 0-12 allergy index."""
    if idx is None:
        return ""
    if idx < 2.5:  return "Low"
    if idx < 4.9:  return "Low-Med"
    if idx < 7.3:  return "Medium"
    if idx < 9.7:  return "Med-High"
    return "High"


def pollen_cat_5(idx: float | None) -> str:
    """Category for Google's 0-5 Universal Pollen Index."""
    if idx is None:
        return ""
    levels = ("None", "Very Low", "Low", "Moderate", "High", "Very High")
    i = int(round(idx))
    return levels[i] if 0 <= i < len(levels) else ""


# ── Wildfire ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WildfireResult:
    """Active wildfire detections near a location."""
    source: str
    location: str
    fire_count: int            = 0      # fires within the search radius
    nearest_km: float | None   = None   # distance to nearest fire
    nearest_name: str          = ""     # named incident (if known)
    max_acres: float | None    = None   # largest nearby fire's size


# ── Space weather / aurora ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SpaceWeatherResult:
    """Geomagnetic activity and aurora visibility chance."""
    source: str
    location: str
    kp_index: float | None     = None   # planetary K index 0-9
    kp_category: str           = ""     # Quiet / Unsettled / Storm (G1-G5)
    aurora_pct: float | None   = None   # aurora probability at this lat/lon (0-100)


# ── Tides ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TideResult:
    """Next high/low tide from the nearest station."""
    source: str
    location: str
    station: str               = ""
    next_high_time: str        = ""
    next_high_m: float | None  = None
    next_low_time: str         = ""
    next_low_m: float | None   = None
    water_temp_c: float | None = None


# ── Helpers ──────────────────────────────────────────────────────────

_AQI_THRESHOLDS: list[tuple[int, str]] = [
    (50,  "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (500, "Hazardous"),
]


def aqi_category(aqi: int | None) -> str:
    """Return the US EPA AQI category for an index value."""
    if aqi is None:
        return ""
    for threshold, label in _AQI_THRESHOLDS:
        if aqi <= threshold:
            return label
    return "Hazardous"


def uv_category(uv: float | None) -> str:
    """Return the WHO UV-index exposure category."""
    if uv is None:
        return ""
    if uv < 3:   return "Low"
    if uv < 6:   return "Moderate"
    if uv < 8:   return "High"
    if uv < 11:  return "Very High"
    return "Extreme"


def kp_category(kp: float | None) -> str:
    """Return the NOAA geomagnetic activity label for a planetary K index."""
    if kp is None:
        return ""
    if kp < 5:   return "Quiet"
    if kp < 6:   return "Minor storm (G1)"
    if kp < 7:   return "Moderate storm (G2)"
    if kp < 8:   return "Strong storm (G3)"
    if kp < 9:   return "Severe storm (G4)"
    return "Extreme storm (G5)"


# ── Provider protocol ────────────────────────────────────────────────

@runtime_checkable
class WeatherProvider(Protocol):
    """Interface every weather provider must implement.

    Required (all providers):
        get_weather, get_forecast

    Optional (implement if the API supports it).  Method names listed
    here MUST match the values in ``_dispatch.CAPABILITY_METHODS`` —
    that's what the dispatcher uses with ``hasattr`` to discover which
    capabilities a provider supports:

        get_hourly, get_alerts, get_air_quality, get_astronomy,
        get_historical, get_marine, get_nowcast, get_uv, get_pollen,
        get_wildfire, get_space_weather, get_tides

    Providers that don't support an optional method simply omit it.
    The registry skips providers that lack the requested method.
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
