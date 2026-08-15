# airnow - US EPA official AQI, air-quality-only specialist provider

## Purpose

Wraps the US EPA AirNow current-observation API so the dispatcher can serve
`.aqi` from the authoritative US regulatory monitor network. It is the rank-1
provider in the `air_quality` chain (`_dispatch.py - DEFAULT_RELIABILITY`).

## Responsibilities / boundaries

Belongs here: one HTTP call, dominant-pollutant selection, and normalization
into `AirQualityResult`. Deliberately not here: geocoding, formatting, provider
selection, retry (see [../dispatch.md](../dispatch.md)), and HTTP mechanics (see
[../http.md](../http.md)).

This is a **single-capability specialist**. It implements `get_air_quality`
only - no `get_weather`, `get_forecast`, `get_hourly`, or anything else. The
dispatcher discovers that by `hasattr` over `CAPABILITY_METHODS`, and
`tests/test_airnow_purpleair.py - TestCapabilityDiscovery.test_airnow_exposes_only_air_quality`
asserts the discovered set is exactly `{"air_quality"}`.

## Dependencies and dependents

Internal: `weather_providers/_http.py - get_json()`, `HTTPError`;
`weather_providers/base.py - AirQualityResult`, `aqi_category()`.
External: `https://www.airnowapi.org/aq/observation/latLong/current/`.
Dependents: `weather_providers/__init__.py - _f_airnow()` builds it,
`_dispatch.py - Dispatcher.dispatch()` calls it, `modules/weather.py` renders
the result via `_format_air_quality()`.

## Lifecycle

Registered as id `airnow` by `_reg("airnow", _f_airnow)`. The factory
`_f_airnow(cfg)` resolves the key through `_cred(cfg, "airnow_key",
"airnow_key")`: `secret_store.get("airnow_key")` (env `INTERNETS_AIRNOW_KEY`,
then `config.ini` `[secrets] airnow_key` at mode 0600) with a legacy fallback to
`[weather_providers] airnow_key`. With no key the factory logs
`airnow: skipped (no airnow_key)` and returns `None`, so the provider never
registers and its `-airnow` / `-an` flag (`modules/weather.py`) is inactive.
The sub-module `airnow.air_quality` is imported eagerly by
`airnow/__init__.py`; the package itself is imported lazily inside the factory.

## State

`AirNowProvider._key` only, set once in the constructor. `air_quality.fetch()`
is a pure function of its arguments plus the HTTP response. No caching, no
persistence.

## Concurrency

`fetch()` is a coroutine on the bot's event loop; the single `get_json()` await
is the only suspension point. No locks, no shared mutable state, so concurrent
`.aqi` calls are independent. The dispatcher caps each call with
`asyncio.wait_for` and increments the module-level quota counter under
`_quota_lock` (`__init__.py - record_call()`).

## Failure behavior

Everything raises `HTTPError`; nothing returns `None`. Three raise sites in
`air_quality.fetch()`:

- response is not a non-empty list - "no AQI coverage for this location"
- no observation carries a non-null `AQI` - "no AQI value in response"
- transport / status / oversize / JSON errors propagate from `get_json()`

Raising (rather than returning `None`) is deliberate: the module comment says it
mirrors the US-only NWS behavior so the dispatcher falls through to a global
provider instead of printing "AQI N/A".
`tests/test_airnow_purpleair.py - TestAirNowFetch.test_empty_list_raises_for_fallback`
pins the empty-list case.

## Security

The API key is sent as the `API_KEY` **query parameter**, not a header. It is
passed via `get_json(params=...)`, so it never appears in the URL string that
`_http.py` embeds in an `HTTPError` message, and `_dispatch.py - _redact()`
additionally scrubs `key=`/`api_key=` patterns from warning logs. The key still
reaches AirNow's own access logs and any TLS-terminating intermediary, which is
inherent to the upstream's design. Response size is capped by `_http.py`
(1 MiB default). No filesystem or subprocess access. `lat`/`lon` come from the
bot's geocoder, not raw user text.

## Classes

### `AirNowProvider` (`airnow/__init__.py`)

Thin adapter. `name = "AirNow"`, `requires_key = True` (asserted by
`tests/test_airnow_purpleair.py - TestCapabilityDiscovery.test_both_require_a_key`).
Constructor takes the API key and stores it as `_key`. The only method is
`get_air_quality(lat, lon, location, **kw)`, which delegates to
`air_quality.fetch()`. The `**kw` is required - the dispatcher forwards caller
kwargs, and `tests/test_new_weather_capabilities.py` enforces `VAR_KEYWORD` on
every new provider's `get_*`.

## Functions and methods

### `air_quality.fetch(key, lat, lon, location)`

Async. Returns `AirQualityResult`; raises `HTTPError`. One GET to
`_BASE` with `format=application/json`, `latitude`, `longitude`,
`distance=25` (miles), `API_KEY`.

AirNow returns a **list of per-pollutant observations** (O3, PM2.5, PM10), each
already an EPA sub-index - not a concentration. The function selects the
dominant pollutant with `max(..., key=AQI)`, which is the EPA convention for
reporting a single number.

## AQI scale semantics

AirNow reports the **US EPA AQI (0-500)** directly, per pollutant. There is no
conversion here: `aqi = int(dominant["AQI"])` is used verbatim. Because the
upstream values are already sub-indices, `pm25`/`pm10`/`o3`/`no2`/`so2`/`co` on
the result stay `None` - the dominant pollutant is instead surfaced in the
source label, e.g. `AirNow (PM2.5)`. The category prefers the upstream
`Category.Name` and falls back to `base.aqi_category()` when absent
(`tests/test_airnow_purpleair.py - TestAirNowFetch.test_falls_back_to_computed_category`).

## Coverage and quota

US only (including territories served by the monitor network); a 25-mile search
radius around the point. Upstream free tier is 500 requests per **hour** per key
(`airnow/__init__.py` docstring).

## Implementation walk

1. Build params and await `get_json()` - external I/O, key injection.
2. Validate the envelope shape (`isinstance(data, list)` and non-empty) -
   fail-closed on a coverage gap.
3. Select the max-AQI observation, skipping non-dict entries and null `AQI`
   values - protocol processing.
4. Raise if nothing survived the filter - error handling.
5. Coerce `AQI` to `int`, read `ParameterName` and `Category.Name` - parsing.
6. Build the source label and return the frozen dataclass - formatting.

## Findings

- questionable | `__init__.py - _DEFAULT_QUOTA_LIMITS["airnow"]` | The limit is
  set to `500` in a table the surrounding comment documents as a *per-day*
  counter that resets at UTC midnight, while AirNow's published free tier is
  500 requests per **hour**; the displayed quota therefore understates the real
  allowance by roughly 24x. The in-code comment acknowledges the mismatch
  ("shown as a soft marker") but the value is still rendered as a daily cap.
- questionable | `air_quality.fetch()` | A coverage gap raises `HTTPError`, and
  `_dispatch.py - Dispatcher.dispatch()` records that as a health **failure**
  (`ProviderHealth.record_failure`). Five non-US lookups inside 60 s therefore
  trip AirNow's circuit breaker for 60 s, suppressing it for a genuinely US
  query that arrives during the cooldown. Contrast `pollendotcom`, which
  returns `None` for the same "not my region" condition and takes no health
  penalty.
- test-gap | `air_quality.fetch()` | No test covers the `distance=25` radius or
  the `Category` dict being present but `Name` empty (the `or aqi_category(aqi)`
  branch is only exercised via a wholly absent `Category`).
