"""Base types for the multi-provider weather system.

Every provider module implements ``WeatherProvider`` and returns normalized
dataclasses.  The registry in ``__init__.py`` handles fallback chains -
providers that don't support a given data type simply omit the method.

Data types
----------
WeatherResult / ForecastDay   - current conditions + daily forecast
HourlyResult  / HourlyEntry   - hourly forecast (temperature, precip, wind)
AlertsResult  / AlertEntry    - active weather alerts and warnings
AirQualityResult              - AQI index, PM2.5, ozone, pollutants
AstronomyResult               - sunrise, sunset, moon phase, illumination
HistoricalResult              - weather on a past date
MarineResult                  - wave height, swell, water temperature
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import asin, cos, log, radians, sin, sqrt
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


# Secondary current-conditions fields the formatter renders as "N/A" when
# missing.  Used by WeatherResult.has_gaps / fill_gaps for cross-provider
# gap-filling (temperature is the core, never gap-filled).
#
# ``feels_like_c`` and ``dewpoint_c`` are deliberately ABSENT.  Both are
# DERIVED from an observation's own temperature (plus humidity and wind), so
# importing them from a provider that measured a different temperature yields
# a line that contradicts itself.  Observed live at Yosemite: NWS reported
# 24.2C from the nearest station (2900m elevation) with no feels-like, and
# Open-Meteo's model grid reported 13.8C with a feels-like of 11.9C computed
# against ITS temperature - so the bot printed "Temperature 24.2C :: Feels
# like 11.3C" at 44% humidity and 6.6mph wind, which no apparent-temperature
# formula produces.  The same query for San Dimas erred the other way (24.4C
# shown against a borrowed 28.8C).
#
# A derived value must come from the SAME observation as the temperature it is
# derived from, so providers populate these natively or leave them None - the
# formatter already hides feels-like when it is missing or within 2 degrees.
_CURRENT_GAP_FIELDS = (
    "humidity", "wind_kph", "wind_dir",
    "pressure_mb", "visibility_m", "description",
)


def _missing(v: object) -> bool:
    """True if a field carries no value (None, or an empty wind-dir string)."""
    return v is None or v == ""


# ── Derived-field formulas (self-consistent: same observation's inputs) ─

def _magnus_dewpoint(temp_c: float, humidity: float) -> float:
    """Magnus formula dewpoint (Alduchov & Eskridge 1996 coefficients)."""
    a, b = 17.625, 243.04
    alpha = (a * temp_c) / (b + temp_c) + log(humidity / 100.0)
    return (b * alpha) / (a - alpha)


def _heat_index_c(temp_c: float, humidity: float) -> float:
    """NWS/Rothfusz regression heat index, in Celsius.

    Applied when T >= 27C and RH >= 40%.  Uses the Fahrenheit regression
    with the Rothfusz adjustments, converted back to Celsius.
    """
    t = temp_c * 9 / 5 + 32  # to Fahrenheit
    rh = humidity
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 0.00683783 * t * t
        - 0.05481717 * rh * rh
        + 0.00122874 * t * t * rh
        + 0.00085282 * t * rh * rh
        - 0.00000199 * t * t * rh * rh
    )
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * ((17 - abs(t - 95)) / 17) ** 0.5
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return (hi - 32) * 5 / 9


def _wind_chill_c(temp_c: float, wind_kph: float) -> float:
    """Environment Canada / NWS wind chill in Celsius.

    Applied when T <= 10C and wind > 4.8 kph.
    """
    return (
        13.12
        + 0.6215 * temp_c
        - 11.37 * wind_kph ** 0.16
        + 0.3965 * temp_c * wind_kph ** 0.16
    )


def _apparent_temp(temp_c: float, humidity: float | None,
                   wind_kph: float | None) -> float | None:
    """Compute apparent temperature from the observation's own values.

    Returns None when the inputs needed for either formula are missing.
    """
    w = wind_kph or 0.0
    if temp_c >= 27.0 and humidity is not None and humidity >= 40.0:
        return _heat_index_c(temp_c, humidity)
    if temp_c <= 10.0 and w > 4.8:
        return _wind_chill_c(temp_c, w)
    return temp_c


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

    def is_empty(self) -> bool:
        """True if the provider responded but carries no usable payload: no
        current temperature AND no forecast days.  Providers build results with
        ``.get()``, so a sparse upstream response yields a non-None result with
        everything None.  The dispatcher treats this like a None result and
        falls through to the next provider, so the preferred provider missing
        the data does not produce an all-N/A answer a fallback could serve."""
        return self.temperature is None and not self.forecast

    def has_gaps(self) -> bool:
        """True if any secondary current-conditions field is missing.  The
        dispatcher uses this to keep walking the provider chain and fill the
        gaps (e.g. NWS station obs often null dewpoint/pressure/visibility, or
        leave textDescription empty -> a blank Conditions field)."""
        return any(_missing(getattr(self, f)) for f in _CURRENT_GAP_FIELDS)

    def fill_gaps(self, other: "WeatherResult") -> "WeatherResult":
        """Return a copy with this result's MISSING secondary fields filled from
        ``other``, crediting both sources.  Temperature and forecast are never
        touched; description is filled ONLY when this result's is empty (NWS obs
        often null textDescription) and is never overwritten when present - the
        ``_missing(self)`` guard below guarantees that.  Returns self unchanged
        if ``other`` adds nothing."""
        upd = {f: getattr(other, f) for f in _CURRENT_GAP_FIELDS
               if _missing(getattr(self, f)) and not _missing(getattr(other, f))}
        if not upd:
            return self
        src = self.source if other.source in self.source else f"{self.source} + {other.source}"
        return replace(self, source=src, **upd)

    def derive_missing(self) -> "WeatherResult":
        """Compute feels_like_c and dewpoint_c from this result's own
        temperature + humidity + wind when the provider left them None.

        Called after gap-filling so the inputs (humidity, wind) are as
        complete as the chain can make them while the derived values still
        belong to this observation's temperature.  Returns self unchanged
        if both are already populated or temperature is missing."""
        if self.temperature is None:
            return self
        upd: dict[str, float | None] = {}
        if self.feels_like_c is None:
            fl = _apparent_temp(self.temperature, self.humidity, self.wind_kph)
            if fl is not None:
                upd["feels_like_c"] = round(fl, 1)
        if self.dewpoint_c is None and self.humidity is not None:
            upd["dewpoint_c"] = round(
                _magnus_dewpoint(self.temperature, self.humidity), 1)
        if not upd:
            return self
        return replace(self, **upd)


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

    def is_empty(self) -> bool:
        """True if the provider returned no hourly entries (fall through)."""
        return not self.hours


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
    aod: float | None          = None   # aerosol optical depth (550nm) - smoke proxy


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
    # Google Pollen - tree/grass/weed index (0-5 Universal Pollen Index)
    tree_index: float | None   = None
    grass_index: float | None  = None
    weed_index: float | None   = None
    # Pollen.com / IQVIA - overall index (0-12) + dominant allergens
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
    max_acres: float | None    = None   # largest nearby fire's current size
    # How many of ``fire_count`` incidents report a size at all.  NIFC's
    # current-incident layer is mostly small dispatch records with no size,
    # so "46 nearby" and "1 sized" are both true and both worth saying.
    # Detection-only sources (FIRMS) leave this 0 and carry no acreage.
    sized_count: int           = 0


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
    here MUST match the values in ``_dispatch.CAPABILITY_METHODS`` -
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
