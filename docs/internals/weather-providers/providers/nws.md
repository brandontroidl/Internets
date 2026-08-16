# nws - National Weather Service provider package (keyless, US only, multi-hop)

## Purpose

api.weather.gov, ranked 1 for `current`, `forecast`, `hourly` and `alerts`
and 2 for `marine` in `_dispatch.DEFAULT_RELIABILITY`. It is the head of most
dispatch chains, so its two distinguishing behaviours matter more than any
other provider's: it needs several sequential HTTP hops to answer one
question, and it must be able to say "not my region" without looking like an
outage.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `nws` (`__init__.py - _reg("nws", _f_nws)`) |
| Factory | `__init__.py - _f_nws()` - constructs with no arguments |
| Credential | None. `NWSProvider.requires_key = False` |
| Daily quota marker | `None` in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-nws` |

No key; NWS instead asks clients to identify themselves, so every module
sends a hardcoded `User-Agent` plus `Accept: application/geo+json`.

## Capabilities

| Capability | Method | Module | Upstream hops |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | 3 |
| forecast | `get_forecast` | `forecast.py` | 2 |
| hourly | `get_hourly` | `hourly.py` | 2 |
| alerts | `get_alerts` | `alerts.py` | 1 |
| marine | `get_marine` | `marine.py` | 2 |

Not implemented: air_quality, astronomy, historical, nowcast, uv, pollen,
wildfire, space_weather, tides. `tests/test_weather_flags.py` asserts that
`_validate_provider(..., "nws", "air_quality")` is rejected.

Every method wraps its module call in `_scope.none_if_uncovered()`, so all
five share one coverage contract.

## Multi-hop request chains

All hops go through `_scope.nws_json()`, a thin wrapper over
`_http.get_json()` (see [http.md](../http.md)).

```text
current  : GET /points/{lat:.4f},{lon:.4f}
           -> properties.observationStations  (a URL)
           GET {observationStations}
           -> features[0].id                  (a station URL)
           GET {station}/observations/latest
forecast : GET /points/{lat},{lon} -> properties.forecast      -> GET it
hourly   : GET /points/{lat},{lon} -> properties.forecastHourly-> GET it
marine   : GET /points/{lat},{lon} -> properties.forecastZone
           (must contain "/marine/") -> GET {zone}/forecast
alerts   : GET /alerts/active?status=actual&{point= | area=}
```

Coordinates are formatted to four decimals, which is what the points API
accepts. Each hop is a separate 10-second HTTP call; `_dispatch.py` caps a
single provider call at `_PER_CALL_BUDGET = 30s` and cites this chain as the
reason that budget exists.

`alerts` takes an optional `area` kwarg (a USPS state code). `point` and
`area` are mutually exclusive upstream, so the module sends exactly one:
`{"area": area} if area else {"point": ...}`. `modules/weather.py` sets it
when the user's query was a bare US state name, because a single geocoded
point inside a state misses the coastal warnings that state-wide questions
are really asking about. The kwarg reaches every alert provider through
`**kw`; NWS is the only one that reads it.

## Coverage handling (`_scope.py`)

NWS signals "outside the US" three different ways, and none of them means the
request was malformed:

| Signal | Where | Becomes |
| --- | --- | --- |
| HTTP 400 (`point` out of bounds) | `/alerts/active` | `OutOfCoverage` |
| HTTP 404 (data unavailable) | `/points/{lat},{lon}` | `OutOfCoverage` |
| 200 with no station / URL / zone | any hop | `OutOfCoverage` raised directly |

`nws_json()` maps statuses in `_NO_DATA_STATUSES = {400, 404}` to
`OutOfCoverage` and re-raises everything else, so 401/403/429/5xx stay real
failures. `none_if_uncovered()` converts `OutOfCoverage` to a `None` result,
which the dispatcher treats as "no usable data - try the next provider"
without recording a failure or a success ([dispatch.md](../dispatch.md)). The
module docstring records the incident that forced this: a non-US query
(`Spain`) logged `dispatch_fail` for nws and dinged its circuit breaker.
The docstring also records why a hardcoded US bounding box was rejected:
upstream stays the authority on its own coverage.

## Response mapping

- `current.fetch()` -> `WeatherResult`. Observation properties are unwrapped
  by a local `_val()` that reads `{"value": ...}` objects. `feels_like_c`
  takes `heatIndex`, falling back to `windChill`; NWS populates at most one
  and nulls both in mild conditions. `barometricPressure` is Pascals and is
  divided by 100 for `pressure_mb`. `visibility` is already metres.
  `textDescription` is often empty, and station observations frequently null
  dewpoint, pressure and visibility, which is precisely the case
  `WeatherResult.has_gaps()` / `fill_gaps()` exist for.
- `forecast.fetch()` -> `WeatherResult` with `temperature=None` and
  `description=""`; only `forecast` is populated. NWS periods alternate
  daytime and nighttime, so the loop takes each `isDaytime` period as the
  high and the period after it (if `isDaytime is False`) as the low, then
  skips both. `day_name` is the period's own `name` ("Tonight", "Monday"),
  not a computed weekday. Fahrenheit is converted by `_f_to_c()` when
  `temperatureUnit == "F"`.
- `hourly.fetch()` -> `HourlyResult` from the first `hours` periods.
  `windSpeed` arrives as text ("15 mph") and `_parse_wind()` takes the
  leading number and multiplies by 1.609. `windDirection` is already a
  cardinal string and is passed through unchanged (no `deg_to_card`).
- `alerts.fetch()` -> `AlertsResult`; severity through
  `_codes.map_severity()` (anything outside extreme/severe/moderate/minor
  becomes `"unknown"`), description truncated to 300 characters.
- `marine.fetch()` -> `MarineResult` carrying only `source` and `location`.
  NWS marine forecasts are prose, and the module never parses them.

## Failure behavior

Only `OutOfCoverage` is caught. Everything else - timeouts, 5xx, decode
errors - propagates as `HTTPError` and is recorded as a failure by the
dispatcher, which is what feeds the circuit breaker in `_health.py`. Because
each capability makes several hops, a failure on hop 2 or 3 costs the latency
of the hops already completed.

## Caching

None, at any layer. `/points` is stable per coordinate and is re-fetched on
every forecast, hourly and marine call.

## Quirks and upstream limits

- US and territories only; unlimited calls, no quota entry.
- Coordinates are truncated to four decimals, which is also what upstream
  uses to bucket its own caching.
- `forecast` periods are 12-hour day/night blocks, so a query made in the
  evening starts with a night period that the loop skips.
- Observation `windSpeed` is passed straight into `wind_kph` with no unit
  check; api.weather.gov reports `wmoUnit:km_h-1` for that field, so the
  value is treated as km/h.

## Tests

- `tests/test_dispatcher.py - test_nws_is_top_for_us_capabilities` asserts
  rank 1 across the US capabilities; the same file covers registration order
  and chain sorting with stub providers.
- `tests/test_weather_flags.py` covers the `-nws` flag, the provider list
  ordering (nws before openmeteo) and the air_quality rejection.
- No test exercises the points -> station -> observation chain, the
  `OutOfCoverage` mapping, or the day/night pairing in `forecast.fetch()`.

## Findings

- defect | `nws/marine.py - fetch()` | Returns a `MarineResult` with every
  data field `None` after confirming the zone exists. `MarineResult` has no
  `is_empty()`, so `_dispatch.dispatch()` accepts it as a success and stops
  the chain; with `stormglass` unconfigured (it needs a key) NWS is rank 2 and
  Open-Meteo (rank 3, which does return waves) is never reached, turning a
  usable marine answer into an empty one for US coastal points.
- questionable | `nws/current.py`, `forecast.py`, `hourly.py`, `marine.py`,
  `alerts.py` | Five separate `_HEADERS` constants with three different
  User-Agent strings: `current.py` uses
  `"(Internets IRC Bot, github.com/brandontroidl/Internets)"`, `alerts.py`
  uses a different contact form, and the rest send bare
  `"(Internets IRC Bot)"` with no contact - the thing the NWS policy asks
  for. The configured `weather_user_agent` secret is not consulted by any of
  them.
- questionable | `nws/current.py - fetch()` | `_val("barometricPressure")` is
  called twice in one expression, and the guard is truthiness rather than
  `is not None`, so a genuine 0 reading would map to `None`.
- questionable | `nws/current.py` | Imports `ms_to_kph` from `._codes` and
  never uses it; the observation wind speed is passed through unconverted and
  the response's `unitCode` is never read, so a station reporting m/s would
  be reported as km/h.
- test-gap | `nws/_scope.py - nws_json()`, `none_if_uncovered()` | The
  400/404 to `OutOfCoverage` to `None` path is the mechanism that keeps
  non-US queries from tripping the breaker and has no test.
