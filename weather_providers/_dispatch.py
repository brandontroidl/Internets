"""Capability-based weather dispatcher with health-scored fallback.

The dispatcher:
    1. Detects capabilities from each provider via ``hasattr``
    2. Maintains a per-capability reliability ordering
    3. Scores providers by health (success rate, latency, rate limits)
    4. Routes requests to the best healthy provider
    5. Falls back through the chain on failure

Usage::

    dispatcher = Dispatcher()
    dispatcher.register(openmeteo_provider, "openmeteo")
    dispatcher.register(weatherapi_provider, "weatherapi")

    result = await dispatcher.dispatch("hourly", lat, lon, location, hours=12)
"""

from __future__ import annotations

import time
import logging
from typing import Any

from ._health import ProviderHealth, health_registry
from ._http import HTTPError

log = logging.getLogger("internets.weather.dispatch")

# Method name for each capability.  Must match ``async def get_*`` on providers.
CAPABILITY_METHODS: dict[str, str] = {
    "current":     "get_weather",
    "forecast":    "get_forecast",
    "hourly":      "get_hourly",
    "alerts":      "get_alerts",
    "air_quality": "get_air_quality",
    "astronomy":   "get_astronomy",
    "historical":  "get_historical",
    "marine":      "get_marine",
    "nowcast":     "get_nowcast",
}

# Static reliability ordering ranked by scientific accuracy of the
# underlying numerical models / observation networks.  Lower = more
# accurate for that capability.  Providers not listed get rank 99.
#
# Ranking rationale (per capability):
#   • current/forecast: government + ECMWF-driven models beat
#     proprietary GFS-only blends.  NWS leads for US (NDFD + human
#     forecaster refinement).  Meteomatics + Open-Meteo + WeatherKit
#     blend ECMWF IFS / ICON / HRRR.  Tier-3 providers (WeatherAPI,
#     WeatherBit, Tomorrow.io) are GFS-derivative with less science
#     review.  Weatherstack/WWO use stale single-model inputs.
#   • hourly: high-resolution rapid-refresh wins (NWS HRRR, ICON-D2 via
#     Meteomatics, WeatherKit HRRR-blend, Pirate's HRRR pull).
#   • alerts: NWS CAP/IPAWS feed is authoritative for US; OWM/Pirate
#     piggyback on NWS + Meteoalarm.
#   • air_quality: CAMS (Copernicus Atmosphere Monitoring) from ECMWF
#     leads; Open-Meteo and OWM both consume CAMS directly.
#   • historical: ERA5 reanalysis (ECMWF) is the scientific gold
#     standard — Open-Meteo and Visual Crossing both expose ERA5.
#   • marine: Stormglass blends 7+ wave models; NWS WaveWatch III for
#     US waters; Open-Meteo serves WaveWatch III + GFS-Wave globally.
#   • nowcast: radar-blended products beat pure-model output —
#     Pirate Weather (MRMS+HRRR) and Meteomatics (RTMA+radar).
#   • astronomy: deterministic ephemeris — all equally accurate, so
#     ranked by data completeness (moon-phase + illumination first).
DEFAULT_RELIABILITY: dict[str, dict[str, int]] = {
    "current":     {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "visualcrossing": 5, "accuweather": 6,
                    "openweathermap": 7, "weatherapi": 8, "weatherbit": 9,
                    "pirateweather": 10, "tomorrowio": 11,
                    "worldweatheronline": 12, "weatherstack": 13},
    "forecast":    {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "visualcrossing": 5, "accuweather": 6,
                    "openweathermap": 7, "weatherbit": 8, "weatherapi": 9,
                    "pirateweather": 10, "tomorrowio": 11,
                    "worldweatheronline": 12, "weatherstack": 13},
    "hourly":      {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "pirateweather": 5, "visualcrossing": 6,
                    "openweathermap": 7, "weatherbit": 8, "weatherapi": 9,
                    "tomorrowio": 10, "accuweather": 11,
                    "worldweatheronline": 12, "stormglass": 13},
    "alerts":      {"nws": 1, "weatherkit": 2, "openweathermap": 3,
                    "pirateweather": 4, "accuweather": 5, "weatherbit": 6,
                    "visualcrossing": 7, "weatherapi": 8, "tomorrowio": 9},
    "air_quality": {"openmeteo": 1, "openweathermap": 2, "weatherbit": 3,
                    "weatherapi": 4, "tomorrowio": 5, "accuweather": 6},
    "astronomy":   {"openmeteo": 1, "weatherapi": 2, "worldweatheronline": 3},
    "historical":  {"openmeteo": 1, "visualcrossing": 2, "weatherbit": 3,
                    "weatherapi": 4, "worldweatheronline": 5, "weatherstack": 6},
    "marine":      {"stormglass": 1, "nws": 2, "openmeteo": 3,
                    "worldweatheronline": 4},
    "nowcast":     {"pirateweather": 1, "meteomatics": 2, "openmeteo": 3},
}


# ── Failure classification helpers ───────────────────────────────────

# Substring tokens we'll still accept as a rate-limit hint when the
# exception isn't an HTTPError (e.g. a provider that raised a custom
# exception before reaching the HTTP layer).  Kept narrow to avoid
# false positives on words like "iterate".
_RL_TOKEN_HINTS = ("429", "rate limit", "ratelimit", "too many requests",
                   "quota exceeded")


def _is_rate_limit_error(e: BaseException) -> bool:
    """Return True iff exception ``e`` indicates an upstream 429 / quota.

    Prefers structured signals (``HTTPError.is_rate_limit``, ``.status``)
    over string sniffing, but keeps a narrow substring fallback for
    provider-raised custom exceptions that never touched _http.
    """
    # Structured path — HTTPError carries explicit metadata.
    if isinstance(e, HTTPError):
        if e.is_rate_limit or e.status == 429:
            return True
        return False
    # aiohttp raises ClientResponseError for non-2xx if .raise_for_status
    # was called directly by a provider that bypassed our wrapper.
    status = getattr(e, "status", None)
    if status == 429:
        return True
    # Last-resort: substring sniff on the message AND the type name.
    msg = str(e).lower()
    if any(tok in msg for tok in _RL_TOKEN_HINTS):
        return True
    return "ratelimit" in type(e).__name__.lower()


def _redact(e: BaseException, limit: int = 160) -> str:
    """Return a one-line, truncated, key-redacted error string.

    Provider URLs frequently include ``?apikey=...`` or ``?appid=...``.
    We don't want those in warning logs.  This is a defensive scrub —
    HTTPError instances already avoid logging full URLs, but defence in
    depth is cheap.
    """
    s = str(e).replace("\n", " ").replace("\r", " ")
    # Redact common api-key query params.
    import re
    s = re.sub(
        r"(?i)\b(apikey|api_key|appid|key|token|secret|password)=[^&\s]+",
        r"\1=<redacted>", s,
    )
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


class _RegisteredProvider:
    """A provider with discovered capabilities and health tracking."""
    __slots__ = ("provider", "provider_id", "capabilities", "health", "reg_order")

    def __init__(self, provider: Any, provider_id: str, reg_order: int = 0) -> None:
        self.provider = provider
        self.provider_id = provider_id
        self.reg_order = reg_order
        self.health: ProviderHealth = health_registry.get(provider_id)

        # Auto-discover capabilities.
        self.capabilities: set[str] = set()
        for cap, method in CAPABILITY_METHODS.items():
            if hasattr(provider, method) and callable(getattr(provider, method)):
                self.capabilities.add(cap)


class Dispatcher:
    """Routes weather requests to the best available provider.

    Providers are scored by:
        1. Health score (success rate, latency, rate limits)
        2. User-configured priority (registration order from config)

    The dispatcher tries providers in scored order and falls back on failure.
    """

    def __init__(self) -> None:
        self._providers: dict[str, _RegisteredProvider] = {}
        self._next_order: int = 0

    def register(self, provider: Any, provider_id: str) -> set[str]:
        """Register a provider and return its discovered capabilities."""
        rp = _RegisteredProvider(provider, provider_id, self._next_order)
        self._next_order += 1
        self._providers[provider_id] = rp
        log.info("Dispatcher: registered %s (%s) — capabilities: %s",
                 getattr(provider, "name", provider_id),
                 provider_id,
                 ", ".join(sorted(rp.capabilities)) or "none")
        return rp.capabilities

    def unregister(self, provider_id: str) -> None:
        """Remove a provider."""
        self._providers.pop(provider_id, None)

    def clear(self) -> None:
        """Remove all providers."""
        self._providers.clear()
        self._next_order = 0

    @property
    def provider_ids(self) -> list[str]:
        """Return registered provider IDs."""
        return list(self._providers.keys())

    def capabilities(self) -> dict[str, list[str]]:
        """Return a map of capability → list of provider IDs that support it."""
        result: dict[str, list[str]] = {}
        for cap in CAPABILITY_METHODS:
            providers = [
                rp.provider_id
                for rp in self._providers.values()
                if cap in rp.capabilities
            ]
            if providers:
                result[cap] = providers
        return result

    def capability_matrix(self) -> str:
        """Human-readable capability matrix for status output."""
        lines: list[str] = []
        caps = self.capabilities()
        for cap in sorted(CAPABILITY_METHODS.keys()):
            providers = caps.get(cap, [])
            if providers:
                chain = " → ".join(
                    self.sort_chain(cap, providers))
                lines.append(f"  {cap}: {chain}")
            else:
                lines.append(f"  {cap}: (no providers)")
        return "\n".join(lines)

    def sort_chain(
        self, capability: str, provider_ids: list[str] | None = None
    ) -> list[str]:
        """Return the dispatch order for a capability — public API.

        Sort providers by scientific accuracy first, then health, then
        user-configured priority.  When ``provider_ids`` is None we
        sort every registered provider that supports the capability;
        otherwise we sort only the supplied subset.

        Order of tie-breaks:
          1. Static reliability rank — providers using the most
             scientifically accurate models (NWS, ECMWF-driven, ERA5,
             radar-blended nowcasts) lead.  This is the dominant key
             because accuracy of the underlying physics is what the
             user actually wants from "weather".
          2. Health score — among providers of comparable accuracy,
             prefer the one that's currently up and fast.
          3. Registration order — final tie-break from
             ``provider_priority`` in config.ini.
        """
        if provider_ids is None:
            provider_ids = [
                rp.provider_id
                for rp in self._providers.values()
                if capability in rp.capabilities
            ]
        reliability = DEFAULT_RELIABILITY.get(capability, {})

        def sort_key(pid: str) -> tuple[int, float, int]:
            rp = self._providers.get(pid)
            rank = reliability.get(pid, 99)
            score = rp.health.health_score if rp else 0.0
            reg = rp.reg_order if rp else 9999
            return (rank, -score, reg)

        return sorted(provider_ids, key=sort_key)

    # Back-compat shim — modules/weather.py used the private name
    # before this refactor.  Forward to the public method.  New code
    # should call ``sort_chain`` directly.
    def _sorted_for_capability(
        self, capability: str, provider_ids: list[str] | None = None
    ) -> list[str]:
        return self.sort_chain(capability, provider_ids)

    async def dispatch(
        self, capability: str, *args: Any, **kwargs: Any
    ) -> Any | None:
        """Dispatch a request to the best provider for a capability.

        Tries providers in scientific-accuracy-then-health order.
        Returns the first successful result, or None if all fail.

        Reserved kwargs (consumed here, not forwarded):
            force_provider: provider id (e.g. "nws") — restrict the
                dispatch chain to just that provider.  If it doesn't
                support the capability the call fails with None and
                no fallback to other providers happens (caller's
                explicit choice).
        """
        force_provider = kwargs.pop("force_provider", None)

        method_name = CAPABILITY_METHODS.get(capability)
        if method_name is None:
            log.error("Unknown capability: %s", capability)
            return None

        eligible = [
            pid for pid, rp in self._providers.items()
            if capability in rp.capabilities
        ]
        if not eligible:
            log.warning("No providers support capability '%s'", capability)
            return None

        if force_provider:
            if force_provider not in self._providers:
                log.warning("force_provider %r is not registered", force_provider)
                return None
            if force_provider not in eligible:
                log.warning("force_provider %r does not support '%s'",
                            force_provider, capability)
                return None
            chain = [force_provider]
        else:
            chain = self._sorted_for_capability(capability, eligible)

        for pid in chain:
            rp = self._providers[pid]
            method = getattr(rp.provider, method_name, None)
            if method is None:
                continue

            # Circuit-breaker gate (weather_providers/_health.py).  When a
            # provider has tripped open, skip it entirely so we don't burn
            # latency on a known-bad upstream during its cooldown.
            if not rp.health.is_callable():
                log.debug("%s: %s skipped — circuit open", pid, capability)
                continue

            # Quota counter — count every attempted upstream call so
            # quota_status() reflects reality even when the call fails.
            # Imported here (not at module top) to avoid the import-cycle
            # weather_providers/__init__.py ↔ _dispatch.py.
            try:
                from . import record_call as _record_call  # noqa: PLC0415
                _record_call(pid)
            except Exception:
                pass

            start = time.monotonic()
            try:
                result = await method(*args, **kwargs)
                latency = time.monotonic() - start
                rp.health.record_success(latency)
                log.debug("%s: %s succeeded (%.2fs)", pid, capability, latency)
                return result
            except Exception as e:
                latency = time.monotonic() - start
                etype = type(e).__name__
                is_rate_limit = _is_rate_limit_error(e)
                rp.health.record_failure(rate_limited=is_rate_limit)
                # Structured, single-line, grep-friendly failure log.
                # We deliberately *don't* log args/kwargs (lat/lon/loc
                # are not secret but URL params can include API keys,
                # so we keep it to type:msg only).
                log.warning(
                    "dispatch_fail provider=%s capability=%s err=%s:%s "
                    "rate_limited=%s latency=%.2fs",
                    pid, capability, etype, _redact(e), is_rate_limit, latency,
                )
                continue

        log.error("All providers failed for '%s'", capability)
        return None

    def get_provider(self, provider_id: str) -> Any | None:
        """Return a provider by ID, or None."""
        rp = self._providers.get(provider_id)
        return rp.provider if rp else None

    def health_summary(self) -> str:
        """Return health summary of all registered providers."""
        return health_registry.summary()
