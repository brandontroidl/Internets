"""Multi-provider weather aggregation platform with capability-based dispatch.

Architecture:
    30 provider packages (sub-module per endpoint)
    ↓
    Dispatcher (capability discovery + accuracy-then-health routing)
    ↓
    Normalized dataclass responses

Usage::

    from weather_providers import configure, dispatch
    from weather_providers import get_weather, get_forecast, get_hourly
    from weather_providers import get_alerts, get_air_quality, get_astronomy
    from weather_providers import get_historical, get_marine, get_nowcast

    configure(config)                     # reads [weather_providers] from ConfigParser
    result = await get_weather(lat, lon, "New York, NY")
    hourly = await get_hourly(lat, lon, "Tokyo", hours=12)

Providers, ranked by scientific accuracy (see _dispatch.DEFAULT_RELIABILITY):
    NWS                - free, no key, US only (NDFD + HRRR + WaveWatch III)
    Meteomatics        - user/pass (premium ECMWF IFS / ICON / GFS blend)
    Apple WeatherKit   - Apple Dev (NWS + IBM TWC blend)
    Open-Meteo         - free, no key (ECMWF / ICON / GFS multi-model + CAMS AQ + ERA5)
    Visual Crossing    - key (ECMWF + ERA5 reanalysis)
    AccuWeather        - key (proprietary long-range models)
    OpenWeatherMap     - key (GFS + ECMWF + CAMS AQ)
    WeatherBit         - key (GFS + station obs)
    WeatherAPI.com     - key (GFS-derived)
    Pirate Weather     - key (Dark Sky compatible - HRRR + MRMS for US)
    Stormglass         - key (marine specialist - 7-model wave blend)
    Tomorrow.io        - key (proprietary nowcasting focus)
    World Weather Online - key (basic single-model)
    Weatherstack       - key (basic, plaintext HTTP - least preferred)

Air-quality-only providers (not part of the current/forecast ranking):
    AirNow             - key (US EPA official AQI - authoritative, US only)
    PurpleAir          - key (crowdsourced PM2.5 sensors - global, hyper-local)
"""

from __future__ import annotations

import logging
import threading
from configparser import ConfigParser
from datetime import datetime, timezone
from typing import Any

from .base import (
    WeatherResult, ForecastDay, WeatherProvider,
    HourlyResult, HourlyEntry, AlertsResult, AlertEntry,
    AirQualityResult, AstronomyResult, HistoricalResult, MarineResult,
    NowcastResult, NowcastEntry, aqi_category,
    UVResult, PollenResult, WildfireResult, SpaceWeatherResult, TideResult,
)
from ._dispatch import Dispatcher, CAPABILITY_METHODS
from ._health import health_registry, format_health_score
from ._http import HTTPError, ResponseTooLargeError

# Lazy imports - provider packages are imported only when needed.
_PROVIDER_FACTORIES: dict[str, Any] = {}


def _cred(cfg: ConfigParser, secret_name: str, ini_key: str) -> str:
    """Pull a provider credential: secret_store first, config.ini fallback.

    The fallback exists so the bot keeps working before the user runs
    ``python -m secret_store migrate``.  After migration the ini values
    are blanked out and only the secret store has them.
    """
    try:
        import secret_store  # noqa: PLC0415
        v = secret_store.get(secret_name)
        if v:
            return v
    except ImportError:
        pass
    return cfg.get("weather_providers", ini_key, fallback="").strip()

__all__ = [
    "WeatherResult", "ForecastDay", "WeatherProvider",
    "HourlyResult", "HourlyEntry", "AlertsResult", "AlertEntry",
    "AirQualityResult", "AstronomyResult", "HistoricalResult", "MarineResult",
    "NowcastResult", "NowcastEntry", "aqi_category",
    "UVResult", "PollenResult", "WildfireResult", "SpaceWeatherResult", "TideResult",
    "configure", "get_providers", "provider_capabilities", "provider_status",
    "get_weather", "get_forecast",
    "get_hourly", "get_alerts", "get_air_quality",
    "get_astronomy", "get_historical", "get_marine", "get_nowcast",
    "get_uv", "get_pollen", "get_wildfire", "get_space_weather", "get_tides",
    "dispatcher", "HTTPError", "ResponseTooLargeError",
    "format_health_score",
    "record_call", "quota_status",
]

log = logging.getLogger("internets.weather.providers")

_MAX_FORECAST_DAYS = 16


# ── Per-provider quota tracking ───────────────────────────────────────
# Operators care about staying under each upstream's free-tier ceiling.
# We track a *daily* counter per provider (resets at UTC midnight) and
# compare it to a per-provider limit.  Limits are best-effort - most
# vendors publish monthly or per-minute caps that don't translate
# directly to a "calls today" number, so the dispatcher just uses
# these for visibility, not enforcement.
#
# ``quota`` is module-level so it's visible to anything that imports
# the package.  Mutations are serialised under ``_quota_lock``.
#
# Schema:
#   quota = {
#       "openweathermap": {"day": "2026-05-19", "count": 17, "limit": 1000},
#       ...
#   }
#
# Default limits (per-day, in calls):
#   - WeatherAPI:        1_000_000  (1M/mo free tier ≈ 33k/day, we use 1M/mo)
#   - OpenWeatherMap:    60_000     (60/min free tier × 60min × 24h, capped)
#   - Tomorrow.io:       500        (500/day free tier)
#   - WeatherBit:        50         (50/day free tier)
#   - Visual Crossing:   1_000      (1000/day free tier)
#   - WeatherStack:      1_000      (1000/mo ≈ 33/day - use monthly value)
#   - AccuWeather:       50         (50/day free tier)
#   - World Weather Online: 500     (500/day free)
#   - Pirate Weather:    10_000     (10k/mo free tier)
#   - Stormglass:        10         (10/day free tier)
#   - NWS / Open-Meteo / Meteomatics / WeatherKit: None (free, no published cap)
_DEFAULT_QUOTA_LIMITS: dict[str, int | None] = {
    "weatherapi":           1_000_000,
    "openweathermap":       60_000,
    "tomorrowio":           500,
    "weatherbit":           50,
    "visualcrossing":       1_000,
    "weatherstack":         1_000,
    "accuweather":          50,
    "worldweatheronline":   500,
    "pirateweather":        10_000,
    "stormglass":           10,
    "nws":                  None,
    "openmeteo":            None,
    "meteomatics":          None,
    "weatherkit":           None,
    "airnow":               500,    # 500 req/hour upstream - shown as a soft marker
    "purpleair":            None,   # points-budget based, no fixed daily call cap
}

# Public module-level dict - operators / status pages can read this
# directly if they prefer not to round-trip through quota_status().
quota: dict[str, dict[str, Any]] = {}
_quota_lock = threading.Lock()


def _today_utc() -> str:
    """Current UTC date as ``YYYY-MM-DD``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _quota_entry_locked(provider_id: str) -> dict[str, Any]:
    """Return the quota entry for *provider_id*, creating / rolling it
    if needed.  Caller must hold ``_quota_lock``.
    """
    today = _today_utc()
    entry = quota.get(provider_id)
    if entry is None or entry.get("day") != today:
        entry = {
            "day": today,
            "count": 0,
            "limit": _DEFAULT_QUOTA_LIMITS.get(provider_id),
        }
        quota[provider_id] = entry
    return entry


def record_call(provider_id: str) -> None:
    """Increment today's quota counter for *provider_id*.

    NOTE: This is **not** called from the dispatcher automatically -
    callers (e.g. the dispatch agent's wiring) must invoke it once per
    upstream request.  Resets at midnight UTC.
    """
    if not provider_id:
        return
    with _quota_lock:
        entry = _quota_entry_locked(provider_id)
        entry["count"] = int(entry.get("count", 0)) + 1


def quota_status(provider_id: str) -> dict[str, Any]:
    """Return current quota usage for *provider_id*.

    Returns a dict with::

        {
            "used":      int   - calls made today (UTC)
            "limit":     int|None - daily cap (None = no published limit)
            "remaining": int|None - limit - used, None when limit is None
            "pct":       float - used / limit * 100 (0.0 when limit is None)
        }
    """
    with _quota_lock:
        entry = _quota_entry_locked(provider_id)
        used = int(entry.get("count", 0))
        limit = entry.get("limit")
    if isinstance(limit, int) and limit > 0:
        remaining = max(0, limit - used)
        pct = (used / limit) * 100.0
    else:
        limit = None
        remaining = None
        pct = 0.0
    return {"used": used, "limit": limit, "remaining": remaining, "pct": pct}

# ── Provider factories ────────────────────────────────────────────────

def _reg(pid, factory):
    _PROVIDER_FACTORIES[pid] = factory

def _f_openmeteo(cfg):
    from .openmeteo import OpenMeteoProvider
    return OpenMeteoProvider()

def _f_weatherapi(cfg):
    key = _cred(cfg, "weatherapi_key", "weatherapi_key")
    if not key:
        log.info("weatherapi: skipped (no weatherapi_key in secret store or config.ini)")
        return None
    from .weatherapi import WeatherAPIProvider
    return WeatherAPIProvider(key)

def _f_tomorrowio(cfg):
    key = _cred(cfg, "tomorrowio_key", "tomorrowio_key")
    if not key:
        log.info("tomorrowio: skipped (no tomorrowio_key)")
        return None
    from .tomorrowio import TomorrowIOProvider
    return TomorrowIOProvider(key)

def _f_weatherkit(cfg):
    try:
        import jwt as _  # noqa
    except ImportError:
        log.warning("weatherkit: skipped - PyJWT not installed "
                    "(pip install PyJWT cryptography)")
        return None
    t = _cred(cfg, "weatherkit_team_id",    "weatherkit_team_id")
    s = _cred(cfg, "weatherkit_service_id", "weatherkit_service_id")
    k = _cred(cfg, "weatherkit_key_id",     "weatherkit_key_id")
    f = _cred(cfg, "weatherkit_key_file",   "weatherkit_key_file")
    # We need all four of team_id / service_id / key_id / key_file.
    # `missing` is a count, not a list of names - CodeQL's
    # py/clear-text-logging-sensitive-data heuristic flags a list-comp
    # that binds the secret value to a tuple even when only the name
    # is logged, so we stay aggregate.
    missing = sum(1 for v in (t, s, k, f) if not v)
    if missing:
        log.info("weatherkit: skipped (%d of 4 required fields missing)", missing)
        return None
    from .weatherkit import WeatherKitProvider
    return WeatherKitProvider(t, s, k, f)

def _f_openweathermap(cfg):
    key = _cred(cfg, "openweathermap_key", "openweathermap_key")
    if not key:
        log.info("openweathermap: skipped (no openweathermap_key)")
        return None
    from .openweathermap import OpenWeatherMapProvider
    return OpenWeatherMapProvider(key)

def _f_weatherstack(cfg):
    key = _cred(cfg, "weatherstack_key", "weatherstack_key")
    if not key:
        log.info("weatherstack: skipped (no weatherstack_key)")
        return None
    from .weatherstack import WeatherstackProvider
    return WeatherstackProvider(key)

def _f_meteomatics(cfg):
    user = _cred(cfg, "meteomatics_username", "meteomatics_username")
    pw   = _cred(cfg, "meteomatics_password", "meteomatics_password")
    if not user or not pw:
        log.info("meteomatics: skipped (no meteomatics_username/password)")
        return None
    from .meteomatics import MeteomaticsProvider
    return MeteomaticsProvider(user, pw)

def _f_accuweather(cfg):
    key = _cred(cfg, "accuweather_key", "accuweather_key")
    if not key:
        log.info("accuweather: skipped (no accuweather_key)")
        return None
    from .accuweather import AccuWeatherProvider
    return AccuWeatherProvider(key)

def _f_visualcrossing(cfg):
    key = _cred(cfg, "visualcrossing_key", "visualcrossing_key")
    if not key:
        log.info("visualcrossing: skipped (no visualcrossing_key)")
        return None
    from .visualcrossing import VisualCrossingProvider
    return VisualCrossingProvider(key)

def _f_pirateweather(cfg):
    key = _cred(cfg, "pirateweather_key", "pirateweather_key")
    if not key:
        log.info("pirateweather: skipped (no pirateweather_key)")
        return None
    from .pirateweather import PirateWeatherProvider
    return PirateWeatherProvider(key)

def _f_nws(cfg):
    from .nws import NWSProvider
    return NWSProvider()

def _f_worldweatheronline(cfg):
    key = _cred(cfg, "worldweatheronline_key", "worldweatheronline_key")
    if not key:
        log.info("worldweatheronline: skipped (no worldweatheronline_key)")
        return None
    from .worldweatheronline import WorldWeatherOnlineProvider
    return WorldWeatherOnlineProvider(key)

def _f_stormglass(cfg):
    key = _cred(cfg, "stormglass_key", "stormglass_key")
    if not key:
        log.info("stormglass: skipped (no stormglass_key)")
        return None
    from .stormglass import StormglassProvider
    return StormglassProvider(key)

def _f_weatherbit(cfg):
    key = _cred(cfg, "weatherbit_key", "weatherbit_key")
    if not key:
        log.info("weatherbit: skipped (no weatherbit_key)")
        return None
    from .weatherbit import WeatherBitProvider
    return WeatherBitProvider(key)

def _f_airnow(cfg):
    key = _cred(cfg, "airnow_key", "airnow_key")
    if not key:
        log.info("airnow: skipped (no airnow_key)")
        return None
    from .airnow import AirNowProvider
    return AirNowProvider(key)

def _f_purpleair(cfg):
    key = _cred(cfg, "purpleair_key", "purpleair_key")
    if not key:
        log.info("purpleair: skipped (no purpleair_key)")
        return None
    from .purpleair import PurpleAirProvider
    return PurpleAirProvider(key)

# ── Specialist / single-capability providers (added this session) ─────

def _f_sunrisesunset(cfg):
    from .sunrisesunset import SunriseSunsetProvider
    return SunriseSunsetProvider()

def _f_currentuvindex(cfg):
    from .currentuvindex import CurrentUVIndexProvider
    return CurrentUVIndexProvider()

def _f_gdacs(cfg):
    from .gdacs import GdacsProvider
    return GdacsProvider()

def _f_eccc(cfg):
    from .eccc import ECCCProvider
    return ECCCProvider()

def _f_metno(cfg):
    from .metno import MetNoProvider
    return MetNoProvider()

def _f_nasapower(cfg):
    from .nasapower import NasaPowerProvider
    return NasaPowerProvider()

def _f_nifc(cfg):
    from .nifc import NIFCProvider
    return NIFCProvider()

def _f_swpc(cfg):
    from .swpc import SWPCProvider
    return SWPCProvider()

def _f_noaa_coops(cfg):
    from .noaa_coops import NoaaCoopsProvider
    return NoaaCoopsProvider()

def _f_waqi(cfg):
    key = _cred(cfg, "waqi_token", "waqi_token")
    if not key:
        log.info("waqi: skipped (no waqi_token)")
        return None
    from .waqi import WAQIProvider
    return WAQIProvider(key)

def _f_openaq(cfg):
    key = _cred(cfg, "openaq_key", "openaq_key")
    if not key:
        log.info("openaq: skipped (no openaq_key)")
        return None
    from .openaq import OpenAQProvider
    return OpenAQProvider(key)

def _f_iqair(cfg):
    key = _cred(cfg, "iqair_key", "iqair_key")
    if not key:
        log.info("iqair: skipped (no iqair_key)")
        return None
    from .iqair import IQAirProvider
    return IQAirProvider(key)

def _f_firms(cfg):
    key = _cred(cfg, "firms_key", "firms_key")
    if not key:
        log.info("firms: skipped (no firms_key)")
        return None
    from .firms import FirmsProvider
    return FirmsProvider(key)

def _f_tidecheck(cfg):
    key = _cred(cfg, "tidecheck_key", "tidecheck_key")
    if not key:
        log.info("tidecheck: skipped (no tidecheck_key)")
        return None
    from .tidecheck import TideCheckProvider
    return TideCheckProvider(key)

def _f_pollendotcom(cfg):
    # Keyless, but reverse-geocodes lat/lon → US ZIP via Nominatim, so it
    # needs the configured User-Agent.
    ua = _cred(cfg, "weather_user_agent", "weather_user_agent")
    from .pollendotcom import PollenDotComProvider
    return PollenDotComProvider(ua)

def _f_google_pollen(cfg):
    key = _cred(cfg, "google_pollen_key", "google_pollen_key")
    if not key:
        log.info("google_pollen: skipped (no google_pollen_key)")
        return None
    from .google_pollen import GooglePollenProvider
    return GooglePollenProvider(key)


_reg("nws",                 _f_nws)
_reg("meteomatics",         _f_meteomatics)
_reg("weatherkit",          _f_weatherkit)
_reg("openmeteo",           _f_openmeteo)
_reg("visualcrossing",      _f_visualcrossing)
_reg("accuweather",         _f_accuweather)
_reg("openweathermap",      _f_openweathermap)
_reg("weatherbit",          _f_weatherbit)
_reg("weatherapi",          _f_weatherapi)
_reg("pirateweather",       _f_pirateweather)
_reg("stormglass",          _f_stormglass)
_reg("tomorrowio",          _f_tomorrowio)
_reg("worldweatheronline",  _f_worldweatheronline)
_reg("weatherstack",        _f_weatherstack)
_reg("airnow",              _f_airnow)
_reg("purpleair",           _f_purpleair)
_reg("metno",               _f_metno)
_reg("waqi",                _f_waqi)
_reg("openaq",              _f_openaq)
_reg("iqair",               _f_iqair)
_reg("sunrisesunset",       _f_sunrisesunset)
_reg("currentuvindex",      _f_currentuvindex)
_reg("gdacs",               _f_gdacs)
_reg("eccc",                _f_eccc)
_reg("nasapower",           _f_nasapower)
_reg("nifc",                _f_nifc)
_reg("firms",               _f_firms)
_reg("swpc",                _f_swpc)
_reg("tidecheck",           _f_tidecheck)
_reg("noaa_coops",          _f_noaa_coops)
_reg("pollendotcom",        _f_pollendotcom)
_reg("google_pollen",       _f_google_pollen)


# ── Global dispatcher ────────────────────────────────────────────────

dispatcher = Dispatcher()


def configure(cfg: ConfigParser) -> None:
    """Build the provider registry from config.  Called on module load/reload."""
    dispatcher.clear()

    priority_str = cfg.get("weather_providers", "provider_priority",
                           fallback=cfg.get("weather_providers", "priority", fallback="")).strip()
    if priority_str:
        order = [p.strip().lower() for p in priority_str.split(",") if p.strip()]
        # provider_priority is an ORDERING preference (and a dispatch
        # tie-breaker), NOT an allowlist.  Append every other known provider
        # after the listed ones so providers added after this config file was
        # written still register (they simply sort last).  Without this, a
        # stale list silently disables whole capabilities - e.g. a config
        # predating the air-quality/wildfire/space-weather/tides providers
        # would never load them.
        order += [p for p in _PROVIDER_FACTORIES if p not in order]
    else:
        order = list(_PROVIDER_FACTORIES.keys())

    for pid in order:
        factory = _PROVIDER_FACTORIES.get(pid)
        if factory is None:
            log.warning("Unknown weather provider %r - skipping", pid)
            continue
        try:
            provider = factory(cfg)
            if provider is not None:
                caps = dispatcher.register(provider, pid)
                log.info("Registered %s: %s",
                         getattr(provider, "name", pid),
                         ", ".join(sorted(caps)) or "none")
        except Exception as e:
            log.warning("Failed to init %r: %s: %s", pid, type(e).__name__, e)

    if not dispatcher.provider_ids:
        from .openmeteo import OpenMeteoProvider
        dispatcher.register(OpenMeteoProvider(), "openmeteo")
        log.warning("No providers configured - falling back to Open-Meteo")

    log.info("Provider chain: %s", " → ".join(dispatcher.provider_ids))
    log.info("Capability matrix:\n%s", dispatcher.capability_matrix())


def get_providers() -> list[str]:
    """Return registered (i.e. active-key) provider IDs."""
    return dispatcher.provider_ids


def provider_capabilities(provider_id: str) -> set[str]:
    """Return the capability set for an active provider, or empty set."""
    return {
        cap for cap, pids in dispatcher.capabilities().items()
        if provider_id in pids
    }


def provider_status() -> list[dict]:
    """Snapshot every *known* provider (active + unconfigured).

    Each entry::

        {
            "id":           provider id (e.g. "openweathermap"),
            "registered":   bool (True if key configured and factory ran ok),
            "state":        "active" | "cold" | "failing" | "unconfigured",
            "calls":        int (recent API call count),
            "fails":        int (recent failure count),
            "success_rate": float (EMA, 0.0–1.0),
            "health_score": float (0.0–1.0),
            "quota":        dict (see quota_status() - used/limit/remaining/pct),
        }

    ``state`` decoder:
      - ``unconfigured`` - factory exists but no key in secret_store/config
      - ``cold``         - registered but no API calls have happened yet
      - ``failing``      - registered, calls have happened, success_rate ≤ 0.5
      - ``active``       - registered, calls have happened, success_rate > 0.5
        (i.e. the upstream API is currently accepting our credentials).
    """
    result: list[dict] = []
    registered = set(dispatcher.provider_ids)
    for pid in _PROVIDER_FACTORIES:
        if pid not in registered:
            result.append({
                "id": pid, "registered": False, "state": "unconfigured",
                "calls": 0, "fails": 0,
                "success_rate": 0.0, "health_score": 0.0,
                "quota": quota_status(pid),
            })
            continue
        h = health_registry.get(pid)
        if h.total_calls == 0:
            state = "cold"
        elif h.success_rate > 0.5:
            state = "active"
        else:
            state = "failing"
        result.append({
            "id": pid, "registered": True, "state": state,
            "calls": h.total_calls, "fails": h.total_failures,
            "success_rate": h.success_rate, "health_score": h.health_score,
            "quota": quota_status(pid),
        })
    return result


# ── Public dispatch functions ─────────────────────────────────────────
# All accept ``force_provider=<id>`` (e.g. "nws") to pin the dispatch
# chain to a single provider, bypassing the accuracy/health ordering.
# That kwarg is used by the user-facing `-p <name>` flag in
# modules/weather.py - passing None (the default) restores normal
# fallback behaviour.  Extra keyword arguments are forwarded to the
# selected provider's method untouched.

def _force_kw(force_provider: str | None, kw: dict) -> dict:
    if force_provider is not None:
        kw["force_provider"] = force_provider
    return kw

async def get_weather(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> WeatherResult | None:
    """Current conditions.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "current", lat, lon, location, **_force_kw(force_provider, kw))

async def get_forecast(
    lat, lon, location, days=4, *, force_provider: str | None = None, **kw,
) -> WeatherResult | None:
    """Multi-day forecast.  ``force_provider`` pins one provider."""
    days = max(1, min(days, _MAX_FORECAST_DAYS))
    return await dispatcher.dispatch(
        "forecast", lat, lon, location, days=days,
        **_force_kw(force_provider, kw))

async def get_hourly(
    lat, lon, location, hours=12, *, force_provider: str | None = None, **kw,
) -> HourlyResult | None:
    """Hourly forecast (≤48 h).  ``force_provider`` pins one provider."""
    hours = max(1, min(hours, 48))
    return await dispatcher.dispatch(
        "hourly", lat, lon, location, hours=hours,
        **_force_kw(force_provider, kw))

async def get_alerts(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> AlertsResult | None:
    """Active weather alerts.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "alerts", lat, lon, location, **_force_kw(force_provider, kw))

async def get_air_quality(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> AirQualityResult | None:
    """Air-quality / pollutants.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "air_quality", lat, lon, location, **_force_kw(force_provider, kw))

async def get_astronomy(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> AstronomyResult | None:
    """Sun/moon ephemeris.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "astronomy", lat, lon, location, **_force_kw(force_provider, kw))

async def get_historical(
    lat, lon, location, target_date="", *,
    force_provider: str | None = None, **kw,
) -> HistoricalResult | None:
    """Historical weather for ``target_date``.  ``force_provider`` pins one."""
    return await dispatcher.dispatch(
        "historical", lat, lon, location, target_date=target_date,
        **_force_kw(force_provider, kw))

async def get_marine(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> MarineResult | None:
    """Marine / wave conditions.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "marine", lat, lon, location, **_force_kw(force_provider, kw))

async def get_nowcast(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> NowcastResult | None:
    """Short-range precipitation nowcast.  ``force_provider`` pins one."""
    return await dispatcher.dispatch(
        "nowcast", lat, lon, location, **_force_kw(force_provider, kw))

async def get_uv(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> UVResult | None:
    """UV index now + today's peak.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "uv", lat, lon, location, **_force_kw(force_provider, kw))

async def get_pollen(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> PollenResult | None:
    """Airborne pollen (CAMS - Europe).  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "pollen", lat, lon, location, **_force_kw(force_provider, kw))

async def get_wildfire(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> WildfireResult | None:
    """Active wildfire detections nearby.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "wildfire", lat, lon, location, **_force_kw(force_provider, kw))

async def get_space_weather(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> SpaceWeatherResult | None:
    """Geomagnetic activity + aurora chance.  ``force_provider`` pins one provider."""
    return await dispatcher.dispatch(
        "space_weather", lat, lon, location, **_force_kw(force_provider, kw))

async def get_tides(
    lat, lon, location, *, force_provider: str | None = None, **kw,
) -> TideResult | None:
    """Next high/low tide from the nearest station.  ``force_provider`` pins one."""
    return await dispatcher.dispatch(
        "tides", lat, lon, location, **_force_kw(force_provider, kw))
