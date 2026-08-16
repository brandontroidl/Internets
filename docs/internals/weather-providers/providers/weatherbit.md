# weatherbit - WeatherBit.io provider package (key, metric SI units)

## Purpose

WeatherBit.io, a GFS-plus-station-observation commercial API, ranked 9 for
current, 8 for forecast and hourly, 6 for alerts, 7 for air quality and 3 for
historical. It implements six capabilities and is the provider whose unit
handling has produced a confirmed regression, so its m/s conversions are
worth reading carefully.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `weatherbit` (`_reg("weatherbit", _f_weatherbit)`) |
| Factory | `__init__.py - _f_weatherbit()` |
| Secret name | `weatherbit_key` |
| Env override | `INTERNETS_WEATHERBIT_KEY` |
| Ini fallback | `[weather_providers] weatherbit_key` |
| Daily quota marker | 50 in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-weatherbit`, `-wb` |

Missing key means `weatherbit: skipped` and no registration. The key is the
`key` query parameter over HTTPS.

## Capabilities

| Capability | Method | Module | Endpoint |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `/v2.0/current` |
| forecast | `get_forecast` | `forecast.py` | `/v2.0/forecast/daily` |
| hourly | `get_hourly` | `hourly.py` | `/v2.0/forecast/hourly` |
| alerts | `get_alerts` | `alerts.py` | `/v2.0/alerts` |
| air_quality | `get_air_quality` | `air_quality.py` | `/v2.0/current/airquality` |
| historical | `get_historical` | `historical.py` | `/v2.0/history/daily` |

Host `api.weatherbit.io`. No astronomy, marine, nowcast, uv, pollen,
wildfire, space_weather or tides.

## Request shape

`key`, `lat`, `lon`, plus `units=M` (metric) on current, forecast, hourly and
historical. The alerts and air-quality endpoints take no units parameter.
Range parameters: `days=min(days, 16)` on the daily forecast,
`hours=min(hours, 48)` on the hourly forecast, and `start_date` / `end_date`
on history where `end_date = target_date + 1 day`, because the endpoint is a
half-open range and a single day needs both bounds. No headers.

Under `units=M` WeatherBit reports wind in m/s and visibility in km, which is
why this package converts more than most.

## Response mapping

- `current.fetch()` -> `WeatherResult` from `data[0]`; raises
  `ValueError("WeatherBit returned no data")` on an empty array. `temp`,
  `app_temp` (feels-like), `rh` (humidity), `pres`, `dewpt` map directly;
  `wind_spd` goes through `base.ms_to_kph()`; `wind_cdir` is already a
  cardinal string; `vis` is km and is multiplied by 1000.
- `forecast.fetch()` -> `WeatherResult` with `temperature=None` and
  `description=""`; days come from `data[:days]` with the weekday derived
  from `valid_date`. High and low use `high_temp or max_temp` and
  `low_temp or min_temp` - WeatherBit reports both the daytime high/low and
  the 24-hour max/min, and the module prefers the former.
- `hourly.fetch()` -> `HourlyResult` from `data[:hours]`, timestamped from
  `timestamp_local` (falling back to `datetime`). `wind_spd` again through
  `ms_to_kph()`; `pop` maps straight to `precip_chance`.
- `alerts.fetch()` -> `AlertsResult`; `title` fills both `event` and
  `headline`, severity is lowercased upstream text with an `"unknown"`
  fallback, and start/end fall back from `onset`/`expires` to
  `effective`/`ends`.
- `air_quality.fetch()` -> `AirQualityResult` from `data[0]`, raising
  `ValueError` on an empty array. WeatherBit's `aqi` is already a US-EPA-style
  0-500 number, so it is coerced with `int()` and passed to
  `base.aqi_category()` without remapping - unlike OpenWeatherMap and
  WeatherAPI, which have to translate a banded index.
- `historical.fetch()` -> `HistoricalResult` from `data[0]`, raising
  `ValueError` on an empty array. `max_wind_spd` is m/s under `units=M` and
  goes through `ms_to_kph()`; `description` is always the empty string
  because the daily history payload carries no condition text.

## Failure behavior

Three modules raise `ValueError` on an empty `data` array rather than
returning a hollow result, so the dispatcher falls through to the next
provider ([dispatch.md](../dispatch.md)). This is the correct shape of the
pattern that `openweathermap/air_quality.py` gets wrong. Everything else
propagates as `HTTPError` from [http.md](../http.md); a 401/403 on a bad key
triggers `mark_auth_failure()` and opens the breaker.

## Caching

None.

## Quirks and upstream limits

- The package docstring claims "Free tier: 500 calls/day" while
  `_DEFAULT_QUOTA_LIMITS["weatherbit"]` is 50; the two disagree (see
  Findings). The quota entry is only a visibility marker either way -
  nothing enforces it.
- Hourly is 48 hours on the free plan and 120 on paid, which the module
  docstring records and the `min(hours, 48)` clamp reflects.
- Alerts, history and air quality are separate plan entitlements on
  WeatherBit; with a plain free key they can return 403, which trips the
  breaker for the whole provider including the capabilities that do work.
  [unverified - the entitlement matrix was not checked against upstream docs
  in this session]
- `history/daily` covers the previous calendar day onward; the module
  defaults `target_date` to yesterday.

## Tests

- `tests/test_provider_fixes.py - TestWeatherbitHistoricalWind` is a
  regression test for a confirmed defect: `max_wind_spd` was stored raw m/s
  into the km/h field, reporting roughly 3.6 times too low. It asserts
  10 m/s becomes 36 km/h.
- `tests/test_dispatcher.py` asserts `weatherbit` is present in
  `_PROVIDER_FACTORIES` and ranked for both current and forecast.
- `tests/test_weather_flags.py` covers the `-weatherbit` / `-wb` aliases.
- No test covers the other five modules' parsing or the m/s conversions in
  `current.py` and `hourly.py`, which are the same class of bug the
  historical test was written for.

## Findings

- questionable | `weatherbit/forecast.py - fetch()` |
  `d.get("high_temp") or d.get("max_temp")` uses truthiness, so a legitimate
  high of exactly 0.0 falls through to `max_temp`, a different quantity. The
  low has the same shape with `low_temp or min_temp`.
- doc-drift | `weatherbit/__init__.py` docstring vs
  `weather_providers/__init__.py - _DEFAULT_QUOTA_LIMITS` | The docstring
  says 500 calls/day, the quota table says 50; one of them is stale and an
  operator reading either alone gets a wrong picture of the headroom.
- questionable | `weatherbit/historical.py - fetch()` | `description` is
  hardcoded to `""`, so a WeatherBit historical answer always renders a blank
  conditions field even though other providers fill it; `HistoricalResult`
  has no gap-filling equivalent to `WeatherResult.fill_gaps()`.
- test-gap | `weatherbit/current.py - fetch()`, `weatherbit/hourly.py -
  fetch()` | Both convert `wind_spd` from m/s and neither has a test; the one
  place this conversion was omitted became the bug pinned by
  `tests/test_provider_fixes.py`.
