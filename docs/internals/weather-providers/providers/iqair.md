# iqair - IQAir / AirVisual nearest-city US AQI

## Purpose

Returns the US AQI for the IQAir monitoring station nearest a lat/lon. Rank 5 in
the `air_quality` chain (`_dispatch.py - DEFAULT_RELIABILITY`), behind `airnow`,
`waqi`, `openaq`, and `openmeteo` - it is a redundancy layer rather than a
primary source.

## Responsibilities / boundaries

Belongs here: one GET, envelope validation, and normalization into
`AirQualityResult`. Not here: geocoding, category text, formatting, or provider
selection (see [../dispatch.md](../dispatch.md), [../base.md](../base.md)).

Single-capability specialist: implements `get_air_quality` **only**.
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery` asserts the
discovered set is exactly `{"air_quality"}`. This is the shortest provider in
the group - 32 lines of parser.

## Dependencies and dependents

Internal: `_http.py - get_json()`, `HTTPError`; `base.py - AirQualityResult`,
`aqi_category()`. External: `https://api.airvisual.com/v2/nearest_city`.
Dependents: `__init__.py - _f_iqair()`, `_dispatch.py`, `modules/weather.py`.

## Lifecycle

Registered as id `iqair` by `_reg("iqair", _f_iqair)`. The factory resolves
`_cred(cfg, "iqair_key", "iqair_key")`: `secret_store.get("iqair_key")` (env
`INTERNETS_IQAIR_KEY`, then `config.ini` `[secrets] iqair_key` at mode 0600),
falling back to the legacy `[weather_providers] iqair_key`. No key logs
`iqair: skipped (no iqair_key)` and returns `None`, leaving the `-iqair` / `-iq`
flags inactive (`tests/test_weather_flags.py` pins both aliases).

## State

`IQAirProvider._key` only. `fetch()` is stateless - no caching of the resolved
"nearest city" between calls, so two lookups in the same city cost two upstream
requests.

## Concurrency

One `await get_json()` per call. No locks, no shared mutable state.

## Failure behavior

Two `HTTPError` raise sites, both `status=None`, `provider_hint="iqair"`:

- envelope is not a dict or `status != "success"` - "no AQI coverage for this
  location". The comment in `air_quality.py` states this branch intentionally
  collapses three distinct upstream conditions (no nearby station, bad key,
  quota exhausted) into one message, because all three mean "no usable reading
  now".
- `data.current.pollution.aqius` missing - "no AQI value in response".

`int(aqi)` is **not** wrapped in `try/except`: a non-numeric `aqius` raises
`ValueError`, which `_dispatch.py` catches as an ordinary provider failure
(`ValueError` is not in `_BUG_EXC_TYPES`, so it is logged at WARNING, not
ERROR). Transport, status, oversize, and JSON errors propagate from
`get_json()`. Nothing returns `None`.

## Security

The key is sent as the `key` **query parameter** via `get_json(params=...)`, so
it is not part of the URL string that `_http.py` embeds in `HTTPError` messages,
and `_dispatch.py - _redact()` scrubs the `key=` pattern from warning logs as a
second layer. Nested `.get()` chains with `or {}` defaults mean a hostile or
truncated response cannot raise `AttributeError` on the parse path. Response
size capped by `_http.py`. No filesystem or subprocess access.

## Classes

### `IQAirProvider` (`iqair/__init__.py`)

`name = "IQAir"`, `requires_key = True`. Constructor stores the key as `_key`.
Sole method `get_air_quality(lat, lon, location, **kw)` delegates to
`air_quality.fetch()`; `**kw` is required because the dispatcher forwards
kwargs (asserted in `tests/test_new_weather_capabilities.py`).

## Functions and methods

### `air_quality.fetch(key, lat, lon, location)`

Async; returns `AirQualityResult`, raises `HTTPError`. GETs `_BASE` with
`lat`, `lon`, `key`; checks `status == "success"`; walks
`data -> current -> pollution -> aqius` with `or {}` at each level; coerces to
`int`; returns an `AirQualityResult` with `source="IQAir"`, the passed
`location`, the parsed `aqi`, and `category=aqi_category(aqi)`.

## AQI scale semantics

`aqius` is IQAir's **US EPA AQI (0-500)** for the nearest city station - already
an index, so no conversion happens here. The category is always recomputed by
`base.py - aqi_category()`.

The free Community tier's `nearest_city` payload carries **no raw
concentrations**, so `pm25`/`pm10`/`o3`/`no2`/`so2`/`co` stay `None` and `.aqi`
via IQAir prints an index and nothing else. The payload does include `mainus`
(the dominant-pollutant code, e.g. `"p2"` for PM2.5) and `aqicn` (the China MEP
index); this module reads neither. Note the contrast with `airnow`, which
surfaces the dominant pollutant in the source label - IQAir's source label is
the bare constant `"IQAir"`, with no station or city name for provenance even
though `data.city` / `data.state` / `data.country` are present in the response.

## Coverage and quota

Global, resolved server-side to the nearest IQAir city station; the module has
no distance control and reports no distance, so a reading may originate tens of
kilometres away with nothing in the output to indicate it. Free Community tier;
no entry in `__init__.py - _DEFAULT_QUOTA_LIMITS`, so `quota_status("iqair")`
reports `limit=None` despite the tier being call-capped upstream. Quota
exhaustion arrives as `status != "success"` and is indistinguishable from a
coverage gap in the logs.

## Implementation walk

1. GET with `lat`/`lon`/`key` - external I/O, credential injection.
2. Envelope type plus `status == "success"` check - fail-closed validation
   covering coverage, auth, and quota in one branch.
3. Nested `or {}` traversal to `pollution` - defensive parsing.
4. `aqius` presence check and raise - error handling.
5. `int()` coercion (unguarded) and dataclass construction - parsing and
   formatting.

## Findings

- questionable | `air_quality.fetch()` | The single `status != "success"` branch
  merges a bad key, an exhausted quota, and a genuine coverage gap. A revoked
  key therefore never reaches `_dispatch.py`'s fast auth trip (which keys on
  `HTTPError.status in (401, 403)`), so the bot keeps spending one upstream
  request per `.aqi` dispatch until the generic five-failure breaker opens, and
  re-probes every 60 s afterwards. The upstream distinguishes the cases in its
  `status`/`data` payload; the module discards that detail.
- questionable | `air_quality.fetch()` | `aqi = int(aqi)` is the only unguarded
  numeric coercion among the four AQI providers documented here (`waqi` and
  `openaq` both wrap theirs). A malformed `aqius` surfaces as a bare
  `ValueError` rather than the module's uniform `HTTPError`.
- questionable | `air_quality.fetch()` | `source` is the constant `"IQAir"`
  although `data.city` and `data.country` are present, so the output carries no
  provenance for a station that may be far from the requested point. `airnow`,
  `waqi`, `openaq`, and `purpleair` all put station, pollutant, or distance in
  the source label.
- test-gap | `air_quality.fetch()` | `tests/` covers only capability discovery
  and the `**kwargs` signature. No mocked test exists for either raise path or
  for the nested `aqius` extraction, unlike `waqi`, whose parser is tested in
  `tests/test_new_weather_capabilities.py - TestWAQI`.
