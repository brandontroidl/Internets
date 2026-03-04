"""Multi-provider weather system with ordered fallback.

Usage::

    from weather_providers import configure, get_weather, get_forecast

    configure(config)          # reads [weather_providers] from ConfigParser
    result = await get_weather(lat, lon, "New York, NY")
    result = await get_forecast(lat, lon, "New York, NY", days=4)

Adding a new provider:
    1. Create ``weather_providers/myprovider.py``.
    2. Implement a class following the ``WeatherProvider`` protocol.
    3. Register it in ``_PROVIDER_FACTORIES`` below.
    4. Add a config key: ``myprovider_key = <key>`` (if required).
"""

from __future__ import annotations

import logging
from configparser import ConfigParser
from typing import Callable

from .base       import WeatherResult, ForecastDay, WeatherProvider
from .openmeteo  import OpenMeteoProvider
from .weatherapi import WeatherAPIProvider
from .tomorrowio import TomorrowIOProvider

__all__ = [
    "WeatherResult", "ForecastDay", "WeatherProvider",
    "configure", "get_weather", "get_forecast",
    "get_providers",
]

log = logging.getLogger("internets.weather.providers")

# ── Provider factories ────────────────────────────────────────────────
# Each factory returns a provider instance or None if not configured.
# The key name in config is ``<provider_id>_key``.

_PROVIDER_FACTORIES: dict[str, Callable[[ConfigParser], WeatherProvider | None]] = {}


def _register(provider_id: str, factory: Callable[[ConfigParser], WeatherProvider | None]) -> None:
    """Register a provider factory.  Called at module level below."""
    _PROVIDER_FACTORIES[provider_id] = factory


def _make_openmeteo(cfg: ConfigParser) -> WeatherProvider | None:
    """Open-Meteo is always available (no key)."""
    return OpenMeteoProvider()


def _make_weatherapi(cfg: ConfigParser) -> WeatherProvider | None:
    """WeatherAPI.com — only enabled if a key is configured."""
    key = cfg.get("weather_providers", "weatherapi_key", fallback="").strip()
    return WeatherAPIProvider(key) if key else None


def _make_tomorrowio(cfg: ConfigParser) -> WeatherProvider | None:
    """Tomorrow.io — only enabled if a key is configured."""
    key = cfg.get("weather_providers", "tomorrowio_key", fallback="").strip()
    return TomorrowIOProvider(key) if key else None


# Registration order = default priority (can be overridden via config).
_register("openmeteo",  _make_openmeteo)
_register("weatherapi", _make_weatherapi)
_register("tomorrowio", _make_tomorrowio)


# ── Active provider list ──────────────────────────────────────────────

_providers: list[WeatherProvider] = []


def configure(cfg: ConfigParser) -> None:
    """Build the ordered provider list from config.

    Reads ``[weather_providers]`` section.  The ``priority`` key is a
    comma-separated list of provider IDs controlling the fallback order.
    Providers whose keys are empty/missing are silently skipped.

    Example config::

        [weather_providers]
        priority = weatherapi, openmeteo, tomorrowio
        weatherapi_key = abc123
        tomorrowio_key = xyz789

    If ``priority`` is absent, the default order is used (Open-Meteo first
    since it requires no key).
    """
    global _providers
    _providers = []

    priority_str = cfg.get("weather_providers", "priority", fallback="").strip()
    if priority_str:
        order = [p.strip().lower() for p in priority_str.split(",") if p.strip()]
    else:
        order = list(_PROVIDER_FACTORIES.keys())

    for pid in order:
        factory = _PROVIDER_FACTORIES.get(pid)
        if factory is None:
            log.warning(f"Unknown weather provider '{pid}' in priority list — skipping")
            continue
        try:
            provider = factory(cfg)
            if provider is not None:
                _providers.append(provider)
                log.info(f"Weather provider registered: {provider.name}"
                         f"{' (key configured)' if provider.requires_key else ''}")
            else:
                log.debug(f"Weather provider '{pid}' skipped (no API key)")
        except Exception as e:
            log.warning(f"Failed to initialize weather provider '{pid}': {e}")

    if not _providers:
        # Always ensure at least Open-Meteo is available.
        _providers.append(OpenMeteoProvider())
        log.warning("No weather providers configured — falling back to Open-Meteo")

    log.info(f"Weather provider chain: {' → '.join(p.name for p in _providers)}")


def get_providers() -> list[WeatherProvider]:
    """Return the current ordered list of active providers."""
    return list(_providers)


async def get_weather(
    lat: float, lon: float, location: str, **kwargs: object
) -> WeatherResult | None:
    """Fetch current weather, trying each provider in order.

    Returns the first successful result, or None if all providers fail.
    """
    if not _providers:
        _providers.append(OpenMeteoProvider())

    for provider in _providers:
        try:
            result = await provider.get_weather(lat, lon, location, **kwargs)
            log.debug(f"Weather from {provider.name} for {location}")
            return result
        except Exception as e:
            log.warning(f"{provider.name} weather failed: {e}")
            continue

    log.error(f"All weather providers failed for {location} ({lat:.4f},{lon:.4f})")
    return None


async def get_forecast(
    lat: float, lon: float, location: str,
    days: int = 4, **kwargs: object
) -> WeatherResult | None:
    """Fetch forecast, trying each provider in order.

    Returns the first successful result, or None if all providers fail.
    """
    if not _providers:
        _providers.append(OpenMeteoProvider())

    for provider in _providers:
        try:
            result = await provider.get_forecast(lat, lon, location, days=days, **kwargs)
            log.debug(f"Forecast from {provider.name} for {location}")
            return result
        except Exception as e:
            log.warning(f"{provider.name} forecast failed: {e}")
            continue

    log.error(f"All forecast providers failed for {location} ({lat:.4f},{lon:.4f})")
    return None
