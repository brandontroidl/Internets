# weatherapi - WeatherAPI.com provider package (key, widest key-based coverage)

## Purpose

WeatherAPI.com, a GFS-derived commercial API. It implements seven
capabilities, the most of any key-based provider in the stack, but ranks low
on the model-quality capabilities (8 for current, 9 for forecast and hourly)
and mid on the derived ones (3 for astronomy, 4 for historical, 8 for
alerts and air quality).

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `weatherapi` (`_reg("weatherapi", _f_weatherapi)`) |
| Factory | `__init__.py - _f_weatherapi()` |
| Secret name | `weatherapi_key` |
| Env override | `INTERNETS_WEATHERAPI_KEY` |
| Ini fallback | `[weather_providers] weatherapi_key` |
| Daily quota marker | 1000000 in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-weatherapi`, `-wapi` |

The factory logs `weatherapi: skipped (no weatherapi_key in secret store or
config.ini)` and returns `None` when unset. The key is sent as the `key`
query parameter over HTTPS; the location is always `q=lat,lon`.

## Capabilities

| Capability | Method | Module | Endpoint |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `/v1/current.json` |
| forecast | `get_forecast` | `forecast.py` | `/v1/forecast.json` |
| hourly | `get_hourly` | `hourly.py` | `/v1/forecast.json` |
| alerts | `get_alerts` | `alerts.py` | `/v1/forecast.json` |
| air_quality | `get_air_quality` | `air_quality.py` | `/v1/current.json` |
| astronomy | `get_astronomy` | `astronomy.py` | `/v1/astronomy.json` |
| historical | `get_historical` | `historical.py` | `/v1/history.json` |

Host `api.weatherapi.com`. Four capabilities are served by two endpoints with
different query parameters: `current.json` with `aqi=no` or `aqi=yes`, and
`forecast.json` with `alerts=no` or `alerts=yes`. No marine, nowcast, uv,
pollen, wildfire, space_weather or tides.

## Request shape

| Module | Distinguishing parameters |
| --- | --- |
| `current.py` | `aqi=no` |
| `air_quality.py` | `aqi=yes` |
| `forecast.py` | `days=min(days, 14)`, `aqi=no`, `alerts=no` |
| `hourly.py` | `days=max(1, ceil(hours / 24))`, `aqi=no`, `alerts=no` |
| `alerts.py` | `days=1`, `aqi=no`, `alerts=yes` |
| `astronomy.py` | `dt=date.today().isoformat()` |
| `historical.py` | `dt=target_date` (defaults to yesterday) |

No headers are set. All values arrive metric already, so the package does no
unit conversion apart from km to metres for visibility.

## Response mapping

- `current.fetch()` -> `WeatherResult` from `current`: `temp_c`,
  `condition.text`, `feelslike_c`, `humidity`, `wind_kph` (already km/h),
  `wind_dir` (already cardinal), `pressure_mb`, `vis_km * 1000`,
  `dewpoint_c`. This is the most completely populated `WeatherResult` of any
  provider here, so it rarely triggers gap-fill ([base.md](../base.md)).
- `forecast.fetch()` -> `WeatherResult` carrying both the current conditions
  and `forecast.forecastday[:days]` mapped to `ForecastDay` (`maxtemp_c`,
  `mintemp_c`, `day.condition.text`), with the weekday computed from the
  `date` string.
- `hourly.fetch()` -> `HourlyResult`. Hours are flattened across forecast
  days and filtered by `time_epoch >= time.time()`, which the comment
  identifies as the timezone-safe comparison (the local `time` string
  compared against `datetime.now()` would be off by the host or location
  offset). Collection stops at `hours` entries. `chance_of_rain` maps to
  `precip_chance`.
- `alerts.fetch()` -> `AlertsResult` from `alerts.alert[]`; severity is
  lowercased upstream text with `"unknown"` as the fallback; `desc`
  truncated to 300 characters.
- `air_quality.fetch()` -> `AirQualityResult`. `us-epa-index` is a 1-6 band
  and the local `_EPA` map converts it to a representative AQI number
  (1->25 ... 6->400) before `base.aqi_category()` labels it.
- `astronomy.fetch()` -> `AstronomyResult` with sunrise, sunset, moonrise,
  moonset, phase and illumination (`_tof()` coerces the string percentage to
  float, returning `None` on junk). `day_length` is not populated.
- `historical.fetch()` -> `HistoricalResult` from
  `forecast.forecastday[0].day`, raising `ValueError("No history data")` when
  the array is empty.

## Failure behavior

`HTTPError` propagates from every module. `historical.fetch()` additionally
raises `ValueError` on an empty payload, which the dispatcher logs as an
ordinary `dispatch_fail` and falls through
([dispatch.md](../dispatch.md)). Nothing here returns `None` or an empty
result, so there is no silent chain-stopping path except an alerts response
with zero alerts, which is a legitimate answer.

## Caching

None.

## Quirks and upstream limits

- The free tier is documented in `__init__.py` of the package only by its
  quota entry; `_DEFAULT_QUOTA_LIMITS` records 1000000, derived in the
  package header comment from the 1M-per-month free allowance rather than a
  per-day cap.
- `forecast.json` on the free plan returns fewer days than the `days=14`
  ceiling the module allows; the module simply slices whatever comes back.
- History depth on the free plan is limited to recent dates, so an old
  `target_date` returns an error rather than data. [unverified - not checked
  against upstream docs in this session]
- Alerts coverage is country-dependent; for the US it is a relay of the same
  NWS CAP feed that the `nws` provider reads first-hand.

## Tests

- `tests/test_new_weather_capabilities.py -
  TestTimezoneWindows.test_weatherapi_filters_past_by_epoch` pins the
  `time_epoch` filter, asserting that a past hour is excluded and the two
  future hours are kept in order.
- `tests/test_weather_flags.py` covers the `-weatherapi` / `-wapi` aliases
  and uses `weatherapi` in the `_validate_provider` success case.
- `tests/test_secret_store.py` and `tests/test_modules_base.py` use
  `weatherapi_key` as their credential-resolution fixture, which exercises
  the env-var to `[secrets]` to `[weather_providers]` precedence the factory
  depends on.

## Findings

- questionable | `weatherapi/astronomy.py - fetch()` | `dt` is
  `date.today()`, the bot host's date, not the queried location's. For a
  location far enough east or west the sun and moon times returned are for
  the wrong local day.
- questionable | `weatherapi/air_quality.py - fetch()` | `mapped =
  _EPA.get(int(idx)) if idx else None` uses truthiness, and `int()` is
  unguarded, so a string index raises `ValueError` from inside the provider
  rather than degrading.
- questionable | `weatherapi/hourly.py - fetch()` | Requests
  `ceil(hours / 24)` forecast days to satisfy an hour count, so a 48-hour
  request pulls two full days of hourly data (up to 48 entries kept out of
  48 fetched) but a 25-hour request fetches 48 hours to return 25.
- questionable | `weatherapi/alerts.py - fetch()` | Severity is passed
  through as free-form lowercased upstream text rather than being mapped to
  the `AlertEntry` vocabulary the way `nws/_codes.py - map_severity()` does,
  so an unexpected upstream label reaches the formatter unnormalised.
