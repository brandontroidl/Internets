# openaq - global open air-quality measurements (v3), raw concentrations

## Purpose

Reads raw pollutant concentrations from the nearest OpenAQ monitoring location
and derives a US EPA AQI from PM2.5. Rank 3 in the `air_quality` chain
(`_dispatch.py - DEFAULT_RELIABILITY`), behind `airnow` and `waqi`.

## Responsibilities / boundaries

Belongs here: the two-call OpenAQ v3 sequence, sensor-id to pollutant mapping,
and normalization into `AirQualityResult`. Not here: geocoding, AQI category
text (`base.py - aqi_category()`), the PM2.5 to AQI curve (borrowed from
`purpleair/_codes.py - pm25_to_aqi()`), formatting, or provider selection
(see [../dispatch.md](../dispatch.md), [../base.md](../base.md)).

Single-capability specialist: implements `get_air_quality` **only**.
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery` asserts the
discovered capability set is exactly `{"air_quality"}`.

## Dependencies and dependents

Internal: `_http.py - get_json()`, `HTTPError`; `base.py - AirQualityResult`,
`aqi_category()`; `purpleair/_codes.py - pm25_to_aqi()` (a cross-package import,
the only one of its kind among the specialists).
External: `https://api.openaq.org/v3/locations` and
`/v3/locations/{id}/latest`.
Dependents: `__init__.py - _f_openaq()`, `_dispatch.py`, `modules/weather.py`.

## Lifecycle

Registered as id `openaq` by `_reg("openaq", _f_openaq)`. The factory resolves
`_cred(cfg, "openaq_key", "openaq_key")`: `secret_store.get("openaq_key")` (env
`INTERNETS_OPENAQ_KEY`, then `config.ini` `[secrets] openaq_key`, file mode
0600) with a legacy `[weather_providers] openaq_key` fallback. Missing key logs
`openaq: skipped (no openaq_key)` and returns `None` - the provider never
registers and the `-openaq` / `-oaq` flag is inactive
(`tests/test_weather_flags.py` pins both aliases).

## State

`OpenAQProvider._key`. `fetch()` builds two local dicts per call
(`sensor_param`, `pollutants`) and holds nothing across calls.

## Concurrency

Two sequential awaits per call - the second URL depends on the id from the
first, so they cannot be parallelized. That doubles the latency budget this
provider consumes from the dispatcher's per-call and whole-chain deadlines
(`_dispatch.py - _PER_CALL_BUDGET`, `_CHAIN_BUDGET`). No locks; no shared state.

## Failure behavior

Four explicit `HTTPError` raise sites, all `status=None`,
`provider_hint="openaq"`:

| Condition | Message |
| --- | --- |
| no `results` or non-dict first result | `no station within range` |
| first result has no `id` | `station has no id` |
| `latest` returned no `results` | `no recent measurements` |
| no measurement mapped to a known pollutant | `no usable pollutant readings` |

Transport, status, oversize, and JSON-decode failures propagate from
`get_json()`. Per-measurement problems are swallowed: a non-dict entry, an
unmapped sensor id, a `None` value, or a `float()` failure just skips that
reading (`continue`), so one bad sensor cannot fail the whole response.

## Security

The key travels as the `X-API-Key` **request header** (not a query parameter),
so it cannot leak through a URL in a log line or a referrer. `loc_id` is
interpolated into the second URL (`f"{_BASE}/{loc_id}/latest"`); it originates
from OpenAQ's own JSON, not from user input, and the base URL is a fixed
constant, so the path cannot be redirected off-host by a user. It is, however,
not type-checked before interpolation - a hostile or malformed upstream `id`
containing `/` or `..` would alter the path within `api.openaq.org`. Response
size capped by `_http.py`. No filesystem or subprocess access.

## Classes

### `OpenAQProvider` (`openaq/__init__.py`)

`name = "OpenAQ"`, `requires_key = True`. Stores the key as `_key`; the sole
method `get_air_quality(lat, lon, location, **kw)` delegates to
`air_quality.fetch()`. `**kw` is mandatory (dispatcher forwards kwargs;
`tests/test_new_weather_capabilities.py` asserts `VAR_KEYWORD`).

## Functions and methods

### `air_quality.fetch(key, lat, lon, location)`

Async; returns `AirQualityResult`, raises `HTTPError`. Algorithm:

1. GET `/v3/locations` with `coordinates="{lat},{lon}"`, `radius=25000` (metres,
   the v3 maximum), `limit=1`, `order_by=distance` - nearest station plus its
   sensor inventory.
2. Build `sensor_param: {sensor_id: parameter_name}` from `loc["sensors"]`,
   lower-casing the parameter name.
3. GET `/v3/locations/{id}/latest`. That endpoint returns only
   `{value, sensorsId}`, which is why step 2 exists - the join is the whole
   point of the two-call design.
4. Map each measurement back to an `AirQualityResult` field through
   `_PARAM_MAP` (`pm25`, `pm10`, `o3`, `no2`, `so2`, `co`); anything else is
   dropped.
5. Derive the AQI from PM2.5 only, then build the result.

### `air_quality._r(v)`

Rounds to one decimal or passes `None` through. Used for every concentration
except `pm25`, which is rounded inline.

## AQI scale semantics

OpenAQ serves **raw concentrations**, not an index. This module converts
**PM2.5 only** through `purpleair/_codes.py - pm25_to_aqi()`, which implements
the EPA piecewise-linear formula on the **2024 revised** PM2.5 breakpoints
(AQI 50 at 9.0 ug/m3, top band 325.4). So the reported `aqi` is a **US EPA
PM2.5 sub-index**, not a multi-pollutant AQI: a location whose dominant
pollutant is ozone will read low here and higher on `airnow` / `waqi`. When the
station has no PM2.5 sensor, `pm25_to_aqi(None)` returns `None`, `category`
becomes `""`, and the result carries concentrations with no index at all.
The station name is surfaced in the source label, e.g. `OpenAQ (Del Amo)`.

## Coverage and quota

Global, but station-based: coverage exists only within 25 km of a contributing
monitor. There is no entry for `openaq` in
`__init__.py - _DEFAULT_QUOTA_LIMITS`, so `quota_status("openaq")` reports
`limit=None` even though the upstream free tier is rate-limited.

## Implementation walk

1. Header construction and the locations GET - external I/O, credential
   injection.
2. `results` shape validation and the two raises - fail-closed on coverage gaps.
3. Sensor inventory loop - protocol processing, defensive `isinstance` and
   null-id skips.
4. Latest-measurements GET and empty check - external I/O plus error handling.
5. Measurement loop with `_PARAM_MAP` translation and `float()` coercion inside
   `try/except (TypeError, ValueError)` - parsing and input validation.
6. Empty-`pollutants` raise - fail-closed.
7. PM2.5 to AQI derivation, category lookup, source labelling, dataclass
   construction - business logic and formatting.

## Findings

- defect | `air_quality.fetch()` - `_PARAM_MAP` / `sensor_param` | The sensor's
  `parameter.units` field is read nowhere; only `parameter.name` is captured.
  Every value is stored as-is and `modules/weather.py - _format_aqi()`
  labels all of `pm25`/`pm10`/`o3`/`no2`/`co` as `ug/m3`. OpenAQ v3 reports
  gaseous species in the contributing network's native unit, which for many US
  and Asian feeds is ppm/ppb rather than ug/m3 [unverified against the live
  API], so a mixed-unit station yields a correct-looking number with the wrong
  unit printed beside it. The unit is present in the response and discarded.
- questionable | `air_quality.fetch()` | When the nearest station has no PM2.5
  sensor the function returns a result with `aqi=None`. `AirQualityResult` has
  no `is_empty()` (unlike `WeatherResult` / `HourlyResult` in
  [../base.md](../base.md)), so `_dispatch.py - Dispatcher.dispatch()` treats it
  as a success and stops the chain - the user sees `AQI N/A` even though the
  lower-ranked `openmeteo` (rank 4) or `iqair` (rank 5) would have returned a
  real index.
- questionable | `air_quality.fetch()` | `so2` is parsed and stored, but
  `modules/weather.py - _format_aqi()` renders `pm25`, `pm10`, `o3`,
  `no2`, and `co` and never `so2`. The field is dead on the output path.
- questionable | `openaq/air_quality.py` importing
  `..purpleair._codes.pm25_to_aqi` | A shared EPA conversion lives inside a
  provider-private `_codes` module of an unrelated provider; a maintainer
  deleting or re-scoping `purpleair` silently breaks `openaq`. The function
  belongs in `base.py` alongside `aqi_category()`.
- test-gap | `air_quality.fetch()` | `tests/` exercises only capability
  discovery and the `**kwargs` signature for this provider. There is no mocked
  test of the two-call sequence, the sensor-id join, or any of the four raise
  paths - by contrast `waqi` and `currentuvindex` both have parser tests in
  `tests/test_new_weather_capabilities.py`.
