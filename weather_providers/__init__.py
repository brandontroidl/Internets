"""Multi-provider weather aggregation platform with capability-based dispatch.

Architecture:
    14 provider packages (sub-module per endpoint)
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
    NWS                — free, no key, US only (NDFD + HRRR + WaveWatch III)
    Meteomatics        — user/pass (premium ECMWF IFS / ICON / GFS blend)
    Apple WeatherKit   — Apple Dev (NWS + IBM TWC blend)
    Open-Meteo         — free, no key (ECMWF / ICON / GFS multi-model + CAMS AQ + ERA5)
    Visual Crossing    — key (ECMWF + ERA5 reanalysis)
    AccuWeather        — key (proprietary long-range models)
    OpenWeatherMap     — key (GFS + ECMWF + CAMS AQ)
    WeatherBit         — key (GFS + station obs)
    WeatherAPI.com     — key (GFS-derived)
    Pirate Weather     — key (Dark Sky compatible — HRRR + MRMS for US)
    Stormglass         — key (marine specialist — 7-model wave blend)
    Tomorrow.io        — key (proprietary nowcasting focus)
    World Weather Online — key (basic single-model)
    Weatherstack       — key (basic, plaintext HTTP — least preferred)
"""

from __future__ import annotations

import logging
from configparser import ConfigParser
from typing import Any

from .base import (
    WeatherResult, ForecastDay, WeatherProvider,
    HourlyResult, HourlyEntry, AlertsResult, AlertEntry,
    AirQualityResult, AstronomyResult, HistoricalResult, MarineResult,
    NowcastResult, NowcastEntry, aqi_category,
)
from ._dispatch import Dispatcher, CAPABILITY_METHODS
from ._health import health_registry

# Lazy imports — provider packages are imported only when needed.
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
    "configure", "get_providers", "get_weather", "get_forecast",
    "get_hourly", "get_alerts", "get_air_quality",
    "get_astronomy", "get_historical", "get_marine", "get_nowcast",
    "dispatcher",
]

log = logging.getLogger("internets.weather.providers")

_MAX_FORECAST_DAYS = 16

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
        log.warning("weatherkit: skipped — PyJWT not installed "
                    "(pip install PyJWT cryptography)")
        return None
    t = _cred(cfg, "weatherkit_team_id",    "weatherkit_team_id")
    s = _cred(cfg, "weatherkit_service_id", "weatherkit_service_id")
    k = _cred(cfg, "weatherkit_key_id",     "weatherkit_key_id")
    f = _cred(cfg, "weatherkit_key_file",   "weatherkit_key_file")
    missing = [name for name, val in [
        ("weatherkit_team_id", t), ("weatherkit_service_id", s),
        ("weatherkit_key_id", k), ("weatherkit_key_file", f),
    ] if not val]
    if missing:
        log.info("weatherkit: skipped (missing: %s)", ", ".join(missing))
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


# ── Global dispatcher ────────────────────────────────────────────────

dispatcher = Dispatcher()


def configure(cfg: ConfigParser) -> None:
    """Build the provider registry from config.  Called on module load/reload."""
    dispatcher.clear()

    priority_str = cfg.get("weather_providers", "provider_priority",
                           fallback=cfg.get("weather_providers", "priority", fallback="")).strip()
    if priority_str:
        order = [p.strip().lower() for p in priority_str.split(",") if p.strip()]
    else:
        order = list(_PROVIDER_FACTORIES.keys())

    for pid in order:
        factory = _PROVIDER_FACTORIES.get(pid)
        if factory is None:
            log.warning("Unknown weather provider %r — skipping", pid)
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
        log.warning("No providers configured — falling back to Open-Meteo")

    log.info("Provider chain: %s", " → ".join(dispatcher.provider_ids))
    log.info("Capability matrix:\n%s", dispatcher.capability_matrix())


def get_providers() -> list[str]:
    """Return registered (i.e. active-key) provider IDs."""
    return dispatcher.provider_ids


def provider_capabilities(provider_id: str) -> set[str]:
    """Return the capability set for an active provider, or empty set."""
    rp = dispatcher._providers.get(provider_id)
    return set(rp.capabilities) if rp else set()


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
        }

    ``state`` decoder:
      - ``unconfigured`` — factory exists but no key in secret_store/config
      - ``cold``         — registered but no API calls have happened yet
      - ``failing``      — registered, calls have happened, success_rate ≤ 0.5
      - ``active``       — registered, calls have happened, success_rate > 0.5
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
        })
    return result


# ── Public dispatch functions ─────────────────────────────────────────
# All accept an optional ``force_provider=<id>`` kwarg (consumed by the
# dispatcher) to restrict the chain to a single provider — used by the
# user-facing `-p <name>` flag in modules/weather.py.

async def get_weather(lat, lon, location, **kw) -> WeatherResult | None:
    return await dispatcher.dispatch("current", lat, lon, location, **kw)

async def get_forecast(lat, lon, location, days=4, **kw) -> WeatherResult | None:
    days = max(1, min(days, _MAX_FORECAST_DAYS))
    return await dispatcher.dispatch("forecast", lat, lon, location, days=days, **kw)

async def get_hourly(lat, lon, location, hours=12, **kw) -> HourlyResult | None:
    hours = max(1, min(hours, 48))
    return await dispatcher.dispatch("hourly", lat, lon, location, hours=hours, **kw)

async def get_alerts(lat, lon, location, **kw) -> AlertsResult | None:
    return await dispatcher.dispatch("alerts", lat, lon, location, **kw)

async def get_air_quality(lat, lon, location, **kw) -> AirQualityResult | None:
    return await dispatcher.dispatch("air_quality", lat, lon, location, **kw)

async def get_astronomy(lat, lon, location, **kw) -> AstronomyResult | None:
    return await dispatcher.dispatch("astronomy", lat, lon, location, **kw)

async def get_historical(lat, lon, location, target_date="", **kw) -> HistoricalResult | None:
    return await dispatcher.dispatch("historical", lat, lon, location, target_date=target_date, **kw)

async def get_marine(lat, lon, location, **kw) -> MarineResult | None:
    return await dispatcher.dispatch("marine", lat, lon, location, **kw)

async def get_nowcast(lat, lon, location, **kw) -> NowcastResult | None:
    return await dispatcher.dispatch("nowcast", lat, lon, location, **kw)
