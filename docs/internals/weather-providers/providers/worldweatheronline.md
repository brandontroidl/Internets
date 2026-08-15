# worldweatheronline - World Weather Online provider (6 capabilities, widest coverage)

## Purpose

Wraps World Weather Online's premium v1 API. It implements more capabilities
than any other provider in this group (current, forecast, hourly, astronomy,
historical, marine) while ranking low for the core two, so in practice it is a
fallback for astronomy, historical and marine. Registered id
`worldweatheronline`, factory
`weather_providers/__init__.py - _f_worldweatheronline()`.

## Responsibilities / boundaries

Belongs here: the `.ashx` request shapes, WWO's nested list-of-dicts value
format, and the per-capability parsing. Not here: transport
([_http.py](../http.md)), selection ([_dispatch.py](../dispatch.md)), result
shapes ([base.py](../base.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `worldweatheronline` |
| Class | `WorldWeatherOnlineProvider(api_key)` |
| `name` / `requires_key` | `"World Weather Online"` / `True` |
| Quota limit | `500` per day (`_DEFAULT_QUOTA_LIMITS`) |

Credential `worldweatheronline_key` via `__init__.py - _cred()`:
`secret_store.get()` (env `INTERNETS_WORLDWEATHERONLINE_KEY`, then
`config.ini [secrets]`), else `config.ini [weather_providers]`.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 12 |
| forecast | `get_forecast` | 12 |
| hourly | `get_hourly` | 12 |
| astronomy | `get_astronomy` | 4 (last of 4) |
| historical | `get_historical` | 5 |
| marine | `get_marine` | 4 (last of 4) |

Flags: `-worldweatheronline`, `-wwo`.

## Endpoints and request shape

```text
https://api.worldweatheronline.com/premium/v1/weather.ashx       (4 capabilities)
https://api.worldweatheronline.com/premium/v1/past-weather.ashx  (historical)
https://api.worldweatheronline.com/premium/v1/marine.ashx        (marine)
```

`weather.ashx` is reused for current, forecast, hourly and astronomy, each with
a different parameter set: current sends `num_of_days=0, fx=no, cc=yes`;
forecast sends `num_of_days=min(days,14), fx=yes, cc=yes`; hourly sends
`num_of_days=2, fx24=yes, tp=1` (one-hour timesteps, despite the module
docstring saying 3-hourly); astronomy sends `date=<today>, num_of_days=1,
fx=no, cc=no, showlocaltime=yes`. The key is the `key` query parameter passed
through `params=`, so it is absent from `HTTPError` messages, and
`_dispatch._redact()` would strip it from any log line that did contain it.

## Response parsing

WWO wraps scalars in single-element lists of dicts, so `_codes._val()` unwraps
`{"key": [{"value": x}]}` and `_codes._float()` coerces the all-strings payload
to floats, returning `None` on `TypeError` / `ValueError`. `_float` was
previously duplicated in all six endpoint modules and is now single-sourced in
`_codes.py`.

- `current.fetch()`: `data.current_condition[0]`. Wind is already km/h,
  `winddir16Point` is already a cardinal string, `visibility` is kilometres and
  is scaled to metres, `FeelsLikeC` and `DewPointC` are native.
- `forecast.fetch()`: `data.weather[:days]`; the day's description comes from
  the middle `hourly` entry.
- `hourly.fetch()`: iterates days then hours until `hours` entries exist; the
  `time` field ("0", "100", ... "2300") is zero-filled to four characters and
  converted to a 12-hour label.
- `astronomy.fetch()`: `weather[0].astronomy[0]`; sunrise, sunset, moonrise,
  moonset, phase and illumination. `day_length` is not populated.
- `historical.fetch()`: `past-weather.ashx` for a single date (defaulting to
  yesterday); `precip_mm` is the sum of hourly `precipMM` values, replacing an
  earlier version that reported `totalSnow_cm` into a millimetre field.
- `marine.fetch()`: `weather[0].hourly[len//2]`; `sigHeight_m` is the combined
  sea height, `winddir16Point` is used as the wave direction and
  `swellDir16Point` for the swell, per the `fix:` comment in the module.

## Lifecycle, state, concurrency

One instance per `configure()`, holding only `_key`; endpoint modules imported
eagerly. No caches, no disk, no module-level mutable state. Every method is a
single `await get_json(...)`.

## Failure behavior

Unlike the other providers in this group, three endpoints raise a bare
`ValueError("WWO: no ... data")` when the payload has no `weather` array:
`astronomy.fetch()`, `historical.fetch()`, `marine.fetch()`. The dispatcher
catches every exception and falls through, and `ValueError` is not in
`_dispatch._BUG_EXC_TYPES`, so it logs at warning rather than error. But it is
recorded as a *failure* by `_health`, which is the wrong signal for a location
the provider simply does not cover (see Findings). `current` and `forecast`
instead return sparse results whose `WeatherResult.is_empty()` is true, which
the dispatcher treats as no-data without penalizing health.

## Security

Key in `params=`, no path-embedded secret, no filesystem access, no
user-supplied URLs; lat/lon are floats interpolated into `q=`. Response size is
bounded by `_http`'s 1 MiB cap.

## Classes

`WorldWeatherOnlineProvider` - six `async get_*` delegators over `_key`. New
capabilities must match `_dispatch.CAPABILITY_METHODS` naming.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `_codes._val(obj, key)` | Unwrap WWO's `[{"value": x}]` nesting |
| `_codes._float(v)` | Coerce to float, `None` on failure |
| `current.fetch(key, lat, lon, loc)` | `current_condition[0]` to `WeatherResult` |
| `forecast.fetch(..., days)` | `data.weather` to `ForecastDay` list |
| `hourly.fetch(..., hours)` | Day and hour loops to `HourlyEntry` list |
| `astronomy.fetch(...)` | `astronomy[0]` to `AstronomyResult` |
| `historical.fetch(..., target_date)` | `past-weather.ashx` to `HistoricalResult` |
| `marine.fetch(...)` | `marine.ashx` mid-day hour to `MarineResult` |

## Tests

`tests/test_dispatcher.py` asserts the id is a registered factory in the
known-provider set. `tests/test_weather_flags.py` pins `-wwo` /
`-worldweatheronline`. No test exercises any `fetch()`, `_val()`, or `_float()`.

## Findings

- **questionable** | `worldweatheronline/{astronomy,historical,marine}.py -
  fetch()` - raising `ValueError` for "no data" makes an uncovered location
  (an inland query against `marine.ashx`, for instance) look like a provider
  outage to `_health.ProviderHealth.record_failure()`, which can trip the
  circuit breaker for a provider that is working correctly. The rest of the
  tree returns an empty dataclass instead; `stormglass/marine.py` was changed
  away from exactly this pattern (its `fix:` comment records the reason).
- **questionable** | `worldweatheronline/marine.py - fetch()` -
  `wave_period_s` and `swell_period_s` are both filled from
  `swellPeriod_secs`, so the two reported periods are always identical and the
  wave period is really the swell period.
- **questionable** | `worldweatheronline/_codes.py - _val()` - defined and
  exported but not called by any endpoint module in the package (each parses
  its own nesting inline). Dead helper. Likewise `current.py` imports
  `deg_to_card` and never uses it, since WWO supplies `winddir16Point` already
  as a cardinal string.
- **doc-drift** | `worldweatheronline/hourly.py` - the module docstring says
  "3-hourly intervals" while the request sends `tp=1` (one-hour timesteps).
- **questionable** | `worldweatheronline/current.py - fetch()` - the visibility
  scale is guarded by `if c.get("visibility")`, so an upstream `"0"` (a legal
  zero-visibility reading) is discarded as missing rather than converted.
- **test-gap** | `worldweatheronline/*` - six endpoint modules, none with
  parsing coverage; the marine direction and historical precipitation fixes
  recorded in the `fix:` comments have no regression test.
