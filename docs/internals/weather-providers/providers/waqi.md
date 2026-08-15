# waqi - World Air Quality Index (aqicn.org), nearest-station index

## Purpose

Serves a single AQI number for the station nearest a lat/lon, worldwide. Rank 2
in the `air_quality` chain (`_dispatch.py - DEFAULT_RELIABILITY`), directly
behind `airnow` - it is the global fallback that catches every non-US lookup
`airnow` rejects.

## Responsibilities / boundaries

Belongs here: one GET, envelope validation, and normalization into
`AirQualityResult`. Not here: geocoding, category text (`base.py -
aqi_category()`), formatting, or provider ordering
(see [../dispatch.md](../dispatch.md), [../base.md](../base.md)).

Single-capability specialist: implements `get_air_quality` **only**.
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery` asserts the
discovered set is exactly `{"air_quality"}`.

## Dependencies and dependents

Internal: `_http.py - get_json()`, `HTTPError`; `base.py - AirQualityResult`,
`aqi_category()`. External: `https://api.waqi.info/feed/geo:{lat};{lon}/`.
Dependents: `__init__.py - _f_waqi()`, `_dispatch.py`, `modules/weather.py`.

## Lifecycle

Registered as id `waqi` by `_reg("waqi", _f_waqi)`. The factory resolves
`_cred(cfg, "waqi_token", "waqi_token")` - note the credential name is
`waqi_token`, not `waqi_key`: `secret_store.get("waqi_token")` (env
`INTERNETS_WAQI_TOKEN`, then `config.ini` `[secrets] waqi_token` at mode 0600),
with a legacy `[weather_providers] waqi_token` fallback. No token logs
`waqi: skipped (no waqi_token)` and returns `None`. The user-facing flag is
`-waqi` with no short alias (`tests/test_weather_flags.py`).

## State

`WAQIProvider._key` only. `fetch()` is stateless.

## Concurrency

One `await get_json()` per call, on the bot's event loop. No locks, no shared
mutable state.

## Failure behavior

Four `HTTPError` raise sites, all `status=None`, `provider_hint="waqi"`:

- envelope is not a dict or `status != "ok"` - "no AQI coverage for this
  location". WAQI signals an unknown station or a bad token inside a 200
  response body, so this branch covers both.
  (`tests/test_new_weather_capabilities.py - TestWAQI.test_error_status_raises`)
- `data` is not a dict - "malformed response"
- `data.aqi` is `None` or the literal string `"-"` (WAQI's "station reporting
  nothing right now" sentinel) - "no AQI value for this location"
  (`TestWAQI.test_dash_aqi_raises`)
- `int(raw)` raises - "non-numeric AQI value"

Nothing returns `None`, so every one of those conditions is recorded by
`_dispatch.py` as a health failure.

## Security

The token is sent as the `token` **query parameter** via `get_json(params=...)`,
so it is absent from the URL string embedded in `HTTPError` messages, and
`_dispatch.py - _redact()` scrubs `token=` from warning logs as a second layer.
`lat`/`lon` are interpolated into the URL path by
`_BASE.format(lat=lat, lon=lon)` - they come from the bot's geocoder as numbers,
not from raw user text, but this is the one specialist that puts caller-supplied
values into the URL **path** rather than into `params`. Response size capped by
`_http.py`. No filesystem or subprocess access.

## Classes

### `WAQIProvider` (`waqi/__init__.py`)

`name = "WAQI"`, `requires_key = True`. Constructor stores the token as `_key`.
Sole method `get_air_quality(lat, lon, location, **kw)` delegates to
`air_quality.fetch()`; `**kw` is required because the dispatcher forwards
kwargs (asserted in `tests/test_new_weather_capabilities.py`).

## Functions and methods

### `air_quality.fetch(key, lat, lon, location)`

Async; returns `AirQualityResult`, raises `HTTPError`. Formats the geo URL,
GETs it with `token`, unwraps the `{"status": ..., "data": ...}` envelope,
coerces `data.aqi` to `int`, reads `data.city.name` for provenance, and returns
`AirQualityResult(source=..., location=..., aqi=..., category=aqi_category(aqi))`.
`tests/test_new_weather_capabilities.py - TestWAQI.test_ok` pins the happy path
(AQI 42 to category "Good", `"WAQI"` present in the source label).

## AQI scale semantics

`data.aqi` is the **US EPA AQI (0-500)** - aqicn.org converts each contributing
network's native reading to the EPA scale before publishing, so no conversion
happens here. The category is always recomputed locally by
`base.py - aqi_category()`; WAQI's own category text is not read.

The response also carries `data.iaqi`, a per-pollutant map. Those are **AQI
sub-indices, not ug/m3 concentrations**, so the module deliberately ignores
them and leaves `pm25`/`pm10`/`o3`/`no2`/`so2`/`co` as `None` - printing an
index value under a `ug/m3` label would be wrong. The comment in
`air_quality.py` states this explicitly. Consequence: `.aqi` via WAQI shows an
index and nothing else.

## Coverage and quota

Global, wherever a contributing station reports. The station is chosen by WAQI
server-side from the geo coordinates; the module exercises no distance control
and surfaces no distance to the user, only the station's city name. No entry in
`__init__.py - _DEFAULT_QUOTA_LIMITS`, so `quota_status("waqi")` reports
`limit=None`; the upstream free token is rate-limited by request rate rather
than a daily count.

## Implementation walk

1. URL formatting from `lat`/`lon` and the GET with the token - external I/O,
   credential injection.
2. Envelope type and `status` check - fail-closed validation that also catches
   auth and unknown-station errors returned with HTTP 200.
3. `data` dict check - defensive shape validation.
4. `aqi` sentinel handling (`None` / `"-"`) - protocol processing.
5. `int()` coercion inside `try/except (TypeError, ValueError)` - input
   validation, converted to the module's uniform `HTTPError`.
6. Nested `city.name` extraction with an explicit `isinstance` guard -
   defensive parsing.
7. Dataclass construction - formatting.

## Findings

- questionable | `air_quality.fetch()` | A bad or revoked token produces
  `status: "error"` in a 200 response and is therefore raised as a generic
  `HTTPError(status=None)`. `_dispatch.py - Dispatcher.dispatch()` reserves its
  `mark_auth_failure()` fast-trip for `HTTPError.status in (401, 403)`, so a
  permanently broken WAQI token burns one upstream request on every `.aqi`
  dispatch until the ordinary five-consecutive-failure breaker trips, and
  re-probes every 60 s thereafter. The upstream distinguishes the case
  (`data` is a diagnostic string such as `"Unknown station"`) but the module
  collapses it into the coverage-gap message.
- questionable | `air_quality.fetch()` | A coverage gap raises, and the
  dispatcher scores a raise as a health failure. WAQI is the global fallback,
  so repeated lookups in a station-free region degrade its health score and can
  push it behind `openaq`/`openmeteo` for locations where it does work. Same
  design tension noted in [airnow.md](airnow.md).
- questionable | `_BASE.format(lat=lat, lon=lon)` | Coordinates are placed in
  the URL path via `str.format` rather than passed as `params`. `_http.py`
  applies no path quoting, so correctness depends entirely on the caller having
  already coerced both to numbers. Every other specialist in this group passes
  coordinates as query parameters.
