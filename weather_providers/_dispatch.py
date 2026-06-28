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

import asyncio
import dataclasses
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
    "uv":          "get_uv",
    "pollen":      "get_pollen",
    "wildfire":    "get_wildfire",
    "space_weather": "get_space_weather",
    "tides":       "get_tides",
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
#     standard - Open-Meteo and Visual Crossing both expose ERA5.
#   • marine: Stormglass blends 7+ wave models; NWS WaveWatch III for
#     US waters; Open-Meteo serves WaveWatch III + GFS-Wave globally.
#   • nowcast: radar-blended products beat pure-model output -
#     Pirate Weather (MRMS+HRRR) and Meteomatics (RTMA+radar).
#   • astronomy: deterministic ephemeris - all equally accurate, so
#     ranked by data completeness (moon-phase + illumination first).
DEFAULT_RELIABILITY: dict[str, dict[str, int]] = {
    "current":     {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "visualcrossing": 5, "accuweather": 6,
                    "openweathermap": 7, "weatherapi": 8, "weatherbit": 9,
                    "pirateweather": 10, "tomorrowio": 11,
                    "worldweatheronline": 12, "metno": 13, "weatherstack": 14},
    "forecast":    {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "visualcrossing": 5, "accuweather": 6,
                    "openweathermap": 7, "weatherbit": 8, "weatherapi": 9,
                    "pirateweather": 10, "tomorrowio": 11,
                    "worldweatheronline": 12, "metno": 13, "weatherstack": 14},
    "hourly":      {"nws": 1, "meteomatics": 2, "weatherkit": 3,
                    "openmeteo": 4, "pirateweather": 5, "visualcrossing": 6,
                    "openweathermap": 7, "weatherbit": 8, "weatherapi": 9,
                    "tomorrowio": 10, "accuweather": 11,
                    "worldweatheronline": 12, "metno": 13, "stormglass": 14},
    "alerts":      {"nws": 1, "weatherkit": 2, "openweathermap": 3,
                    "pirateweather": 4, "accuweather": 5, "weatherbit": 6,
                    "visualcrossing": 7, "weatherapi": 8, "tomorrowio": 9,
                    "gdacs": 10, "eccc": 11, "metno": 12},
    "air_quality": {"airnow": 1, "waqi": 2, "openaq": 3, "openmeteo": 4,
                    "iqair": 5, "openweathermap": 6, "weatherbit": 7,
                    "weatherapi": 8, "tomorrowio": 9, "accuweather": 10,
                    "purpleair": 11},
    "astronomy":   {"sunrisesunset": 1, "openmeteo": 2, "weatherapi": 3,
                    "worldweatheronline": 4},
    "historical":  {"openmeteo": 1, "visualcrossing": 2, "weatherbit": 3,
                    "weatherapi": 4, "worldweatheronline": 5, "weatherstack": 6,
                    "nasapower": 7},
    "marine":      {"stormglass": 1, "nws": 2, "openmeteo": 3,
                    "worldweatheronline": 4},
    "nowcast":     {"pirateweather": 1, "meteomatics": 2, "openmeteo": 3, "metno": 4},
    # Air-quality-only / specialist capabilities added this session.
    "uv":          {"openmeteo": 1, "currentuvindex": 2},
    "pollen":      {"google_pollen": 1, "pollendotcom": 2, "openmeteo": 3},
    "wildfire":    {"nifc": 1, "firms": 2},
    "space_weather": {"swpc": 1},
    "tides":       {"noaa_coops": 1, "tidecheck": 2},
}


# ── Failure classification helpers ───────────────────────────────────

# Substring tokens we'll still accept as a rate-limit hint when the
# exception isn't an HTTPError (e.g. a provider that raised a custom
# exception before reaching the HTTP layer).  Kept narrow to avoid
# false positives on words like "iterate".
_RL_TOKEN_HINTS = ("429", "rate limit", "ratelimit", "too many requests",
                   "quota exceeded")

# Exception types that signal a bug in the PROVIDER's own code (constructing a
# frozen dataclass wrong, a missing attribute, a bad key/index) rather than a
# transient upstream failure.  The dispatcher still falls through on these (so
# one buggy provider can't take the bot down), but logs them at ERROR so the
# defect surfaces instead of hiding behind the normal "provider unavailable".
_BUG_EXC_TYPES = (TypeError, AttributeError, KeyError, IndexError, NameError,
                  dataclasses.FrozenInstanceError)


# ── End-to-end fallback-chain time budget ────────────────────────────
# The outer command handler caps a command at _CMD_TIMEOUT (60s, see
# internets.py).  A single slow provider - NWS, for instance, makes two
# to three sequential 10s HTTP hops - must not consume that whole budget
# and starve the healthy fallbacks queued behind it.  We bound the whole
# chain to _CHAIN_BUDGET (leaving headroom under the 60s command cap for
# formatting + IRC send) and cap each individual provider call at the
# smaller of _PER_CALL_BUDGET and the time still left in the chain, so
# one slow upstream can never eat the budget the fallbacks need.
_CHAIN_BUDGET = 45.0     # seconds - whole fallback chain
_PER_CALL_BUDGET = 30.0  # seconds - any single provider call (covers NWS multi-hop)


def _is_rate_limit_error(e: BaseException) -> bool:
    """Return True iff exception ``e`` indicates an upstream 429 / quota.

    Prefers structured signals (``HTTPError.is_rate_limit``, ``.status``)
    over string sniffing, but keeps a narrow substring fallback for
    provider-raised custom exceptions that never touched _http.
    """
    # Structured path - HTTPError carries explicit metadata.
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
    We don't want those in warning logs.  This is a defensive scrub -
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
        log.info("Dispatcher: registered %s (%s) - capabilities: %s",
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
        """Return the dispatch order for a capability - public API.

        Sort providers by scientific accuracy first, then health, then
        user-configured priority.  When ``provider_ids`` is None we
        sort every registered provider that supports the capability;
        otherwise we sort only the supplied subset.

        Order of tie-breaks:
          1. Static reliability rank - providers using the most
             scientifically accurate models (NWS, ECMWF-driven, ERA5,
             radar-blended nowcasts) lead.  This is the dominant key
             because accuracy of the underlying physics is what the
             user actually wants from "weather".
          2. Health score - among providers of comparable accuracy,
             prefer the one that's currently up and fast.
          3. Registration order - final tie-break from
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

    # Back-compat shim - modules/weather.py used the private name
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
            force_provider: provider id (e.g. "nws") - restrict the
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
            if not self._providers[force_provider].health.is_callable():
                log.warning("force_provider %r circuit is open (cooling down) - "
                            "skipping; try again shortly", force_provider)
                return None
            chain = [force_provider]
        else:
            chain = self._sorted_for_capability(capability, eligible)

        # Whole-chain deadline.  Capture once at the start so the time
        # already spent on earlier (slow) providers shrinks what later
        # ones are allowed - this is what keeps a brownout from starving
        # the healthy fallbacks behind it within the outer command timeout.
        deadline = time.monotonic() + _CHAIN_BUDGET

        # Current-capability gap-fill accumulator (see the success branch below).
        primary: Any = None
        merged = 0

        for pid in chain:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "dispatch_budget_exhausted capability=%s budget=%.0fs - "
                    "stopping chain before %s", capability, _CHAIN_BUDGET, pid)
                break

            rp = self._providers[pid]
            method = getattr(rp.provider, method_name, None)
            if method is None:
                continue

            # Circuit-breaker gate (weather_providers/_health.py).  When a
            # provider has tripped open, skip it entirely so we don't burn
            # latency on a known-bad upstream during its cooldown.
            if not rp.health.is_callable():
                log.debug("%s: %s skipped - circuit open", pid, capability)
                continue

            # Quota counter - count every attempted upstream call so
            # quota_status() reflects reality even when the call fails.
            # Imported here (not at module top) to avoid the import-cycle
            # weather_providers/__init__.py ↔ _dispatch.py.
            try:
                from . import record_call as _record_call  # noqa: PLC0415
                _record_call(pid)
            except Exception:
                pass  # nosec B110: best-effort cleanup

            # Cap this single call at whatever budget is left (never more
            # than _PER_CALL_BUDGET).  A hang now raises asyncio.TimeoutError,
            # caught below as a failure - so a brownout provider also trips
            # its breaker, instead of silently eating the chain budget.
            call_timeout = min(remaining, _PER_CALL_BUDGET)

            start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    method(*args, **kwargs), timeout=call_timeout)
                latency = time.monotonic() - start
                if result is None or (hasattr(result, "is_empty") and result.is_empty()):
                    # Provider responded but has no usable data for this location
                    # (a region it doesn't cover, or a sparse response with no
                    # core value) - fall through rather than returning an empty /
                    # all-N/A answer.  We do NOT record a success: a no-data
                    # result must not reset the breaker or mask a brownout, so it
                    # can't keep a degraded provider looking healthy.
                    log.debug("%s: %s no usable data - trying next", pid, capability)
                    continue
                # Real data: a success for the breaker / score.
                rp.health.record_success(latency)
                log.debug("%s: %s succeeded (%.2fs)", pid, capability, latency)
                # Gap-fill (current only): the first usable result is the primary.
                # If it is sparse (a secondary field the formatter shows as N/A,
                # e.g. NWS nulling dewpoint/pressure/visibility), keep walking the
                # chain and fill ONLY the missing fields from the next usable
                # provider(s), crediting both sources, rather than returning an
                # all-N/A answer.  Bounded to 3 contributors and the chain
                # deadline above.
                if capability != "current" or not hasattr(result, "has_gaps"):
                    return result
                primary = result if primary is None else primary.fill_gaps(result)
                merged += 1
                if not primary.has_gaps() or merged >= 3:
                    return primary
                continue
            except Exception as e:
                latency = time.monotonic() - start
                etype = type(e).__name__
                is_rate_limit = _is_rate_limit_error(e)
                rp.health.record_failure(rate_limited=is_rate_limit)
                if isinstance(e, HTTPError) and e.status in (401, 403):
                    # Deterministic auth/entitlement failure - trip the breaker
                    # now so we stop burning a request per dispatch on a known-
                    # bad key (it still re-probes after the cooldown).
                    rp.health.mark_auth_failure()
                if isinstance(e, _BUG_EXC_TYPES):
                    # Provider code defect, not an upstream outage - log loudly
                    # so it doesn't hide behind the normal fallthrough.
                    log.error(
                        "dispatch_bug provider=%s capability=%s err=%s:%s "
                        "(provider code defect - fix the provider)",
                        pid, capability, etype, _redact(e),
                    )
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

        # Chain exhausted.  A sparse-but-usable current primary we never fully
        # filled still beats nothing.
        if primary is not None:
            return primary
        log.error("All providers failed for '%s'", capability)
        return None

    def get_provider(self, provider_id: str) -> Any | None:
        """Return a provider by ID, or None."""
        rp = self._providers.get(provider_id)
        return rp.provider if rp else None

    def health_summary(self) -> str:
        """Return health summary of all registered providers."""
        return health_registry.summary()
