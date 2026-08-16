# tomorrowio - Tomorrow.io provider (v4 timelines, 5 capabilities)

## Purpose

Wraps the Tomorrow.io v4 API: realtime conditions, daily and hourly timelines,
events (alerts), and air quality read out of the realtime payload. Registered
id `tomorrowio`, factory `weather_providers/__init__.py - _f_tomorrowio()`.

## Responsibilities / boundaries

Belongs here: the v4 request shape, the numeric `weatherCode` table, and the
two entitlement workarounds (paid-only `/v4/events`, plan-gated air-quality
fields). Not here: transport ([_http.py](../http.md)), selection
([_dispatch.py](../dispatch.md)), result shapes ([base.py](../base.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `tomorrowio` (`_reg("tomorrowio", _f_tomorrowio)`) |
| Class | `TomorrowIOProvider(api_key)` |
| `name` / `requires_key` | `"Tomorrow.io"` / `True` |
| Quota limit | `500` per day (`_DEFAULT_QUOTA_LIMITS`) |

Credential `tomorrowio_key` via `__init__.py - _cred()`: `secret_store.get()`
first (env `INTERNETS_TOMORROWIO_KEY`, then `config.ini [secrets]`), then
`config.ini [weather_providers] tomorrowio_key`. No key means the factory
returns `None` and the provider is not registered.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 11 |
| forecast | `get_forecast` | 11 |
| hourly | `get_hourly` | 10 |
| alerts | `get_alerts` | 9 |
| air_quality | `get_air_quality` | 9 |

Flags: `-tomorrowio`, `-tomorrow`, `-tio`.

## Endpoints and request shape

```text
https://api.tomorrow.io/v4/weather/realtime   ?apikey=&location=lat,lon&units=metric
https://api.tomorrow.io/v4/weather/forecast   ?...&timesteps=1d      (forecast)
https://api.tomorrow.io/v4/weather/forecast   ?...&timesteps=1h      (hourly)
https://api.tomorrow.io/v4/events             ?apikey=&location=lat,lon
```

The key travels as the `apikey` query parameter passed through `get_json`'s
`params=`, so it never appears in `HTTPError`'s message (which formats the base
URL only). `air_quality` reuses `weather/realtime` rather than a dedicated
endpoint.

## Response parsing

- `current.fetch()`: `data.values`. `windSpeed` is metres per second
  (`base.ms_to_kph()`), `visibility` is kilometres (`base.km_to_m()`),
  `pressureSurfaceLevel` fills `pressure_mb`, and `temperatureApparent` /
  `dewPoint` are native. An unmapped `weatherCode` renders as `"Code {n}"`.
- `forecast.fetch()`: `timelines.daily[:days]`; day name from ISO `time` with
  `Z` rewritten to `+00:00`. The result's *current* temperature is
  `temperatureAvg` of day 0, not an observation.
- `hourly.fetch()`: `timelines.hourly[:hours]`; `precipitationIntensity` to
  `precip_mm`, `precipitationProbability` already 0-100. Hour labels are `%I %p`
  in UTC.
- `alerts.fetch()`: `data.events[]` to `AlertEntry`; `eventType` is preferred
  over `title` for the event name.
- `air_quality.fetch()`: `epaIndex` plus the six pollutant fields;
  `base.aqi_category()` supplies the label.

`_codes.CODES` maps the 26 documented numeric codes; `_codes` also re-exports
`deg_to_card`, `ms_to_kph`, `km_to_m` from [base.py](../base.md).

## Lifecycle, state, concurrency

Constructed once per `configure()`, holds only `_key`, no caches, no disk. Each
method is a single `await get_json(...)`, so calls are independent and safe to
run concurrently.

## Failure behavior

Two deliberate departures from the default "let it raise" pattern:

1. `alerts.fetch()` catches `HTTPError` with status 401 or 403 (the
   `/v4/events` endpoint is paid-tier only) and returns an empty
   `AlertsResult` rather than propagating, so a free-tier key does not look
   like an outage. Every other status still propagates and is counted by
   `_health`.
2. `air_quality.fetch()` raises a synthetic `HTTPError` when the AQI and all
   six pollutants are `None`, because `weather/realtime` returns those fields
   only on entitled plans. `AirQualityResult` has no `is_empty()`, so without
   the raise the dispatcher would return a hollow all-`None` result and never
   fall through. Both branches are covered by
   `tests/test_provider_fixes.py - TestTomorrowioAirQualityFallthrough`.

Otherwise `HTTPError` propagates to `_dispatch.Dispatcher.dispatch()`, which
records the failure, tags 429 through `HTTPError.is_rate_limit`, and calls
`mark_auth_failure()` on 401/403.

## Security

No secret in a URL path, no filesystem access, no user-supplied URL. The
`location` parameter is built from float lat/lon. Response size is bounded by
`_http`'s 1 MiB cap.

## Classes

`TomorrowIOProvider` - key holder with five `async get_*` delegators. Adding a
capability requires the `get_<cap>(self, lat, lon, location, **kw)` signature
from `_dispatch.CAPABILITY_METHODS`.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `current.fetch(key, lat, lon, loc)` | `weather/realtime` to `WeatherResult` |
| `forecast.fetch(..., days)` | `timelines.daily` to `ForecastDay` list |
| `hourly.fetch(..., hours)` | `timelines.hourly` to `HourlyEntry` list |
| `alerts.fetch(...)` | `/v4/events`, empty result on 401/403 |
| `air_quality.fetch(...)` | realtime AQ fields, raises when all `None` |

## Tests

`tests/test_provider_fixes.py - TestTomorrowioAirQualityFallthrough` covers
both air-quality branches with a stubbed `get_json`.
`tests/run_tests.py` constructs `TomorrowIOProvider("test-key")` to assert
protocol conformance and that `get_weather` / `get_forecast` are coroutines,
and uses `tomorrowio` in the `provider_priority` ordering test.
`tests/test_secret_store.py` uses `tomorrowio_key` as the worked example of the
config-to-secret-store migration. `tests/test_weather_flags.py` pins the three
aliases.

## Findings

- **defect** | `tomorrowio/alerts.py - fetch()` - the empty `AlertsResult`
  returned on 401/403 is a *successful* dispatch result: `AlertsResult` has no
  `is_empty()`, so `_dispatch.Dispatcher.dispatch()` returns it immediately and
  never tries the providers ranked below (gdacs 10, eccc 11, metno 12). A
  free-tier key therefore suppresses ECCC alerts for Canadian locations
  whenever the higher-ranked alert providers have already fallen through.
- **questionable** | `tomorrowio/forecast.py - fetch()` - `WeatherResult.
  temperature` is filled from `temperatureAvg` of the first forecast day, so a
  forecast response reports a daily mean where the field means "current
  temperature" everywhere else.
- **questionable** | `tomorrowio/forecast.py - fetch()` - `wc = v.get(
  "weatherCodeMax") or v.get("weatherCode")` treats a legitimate `0` as absent
  (`_codes.CODES[0]` is `"Unknown"`), and the current-condition line then guards
  with `if cc` rather than `is not None`, so code 0 renders as `"N/A"`.
- **questionable** | `tomorrowio/hourly.py - fetch()` - hour labels are UTC with
  no conversion to the queried location's zone, and past hours are not filtered.
- **test-gap** | `tomorrowio/{current,forecast,hourly,alerts}.py` - only the
  air-quality module has parsing tests; the alerts 401/403 degradation in
  particular is untested.
