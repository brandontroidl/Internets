# openweathermap - OpenWeatherMap provider package (key, 2.5 plus OneCall 3.0)

## Purpose

OpenWeatherMap, ranked 7 for current, forecast and hourly, 3 for alerts and 6
for air quality. Notable for spanning two API generations: four of its five
capabilities sit on the free 2.5 endpoints, while alerts requires the
separately subscribed OneCall 3.0 API.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `openweathermap` (`_reg("openweathermap", _f_openweathermap)`) |
| Factory | `__init__.py - _f_openweathermap()` |
| Secret name | `openweathermap_key` |
| Env override | `INTERNETS_OPENWEATHERMAP_KEY` |
| Ini fallback | `[weather_providers] openweathermap_key` |
| Daily quota marker | 60000 in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-openweathermap`, `-owm` |

No key means the factory logs `openweathermap: skipped` and returns `None`.
The key is sent as the `appid` query parameter over HTTPS.

## Capabilities

| Capability | Method | Module | Endpoint |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `/data/2.5/weather` |
| forecast | `get_forecast` | `forecast.py` | `/data/2.5/forecast` |
| hourly | `get_hourly` | `hourly.py` | `/data/2.5/forecast` |
| alerts | `get_alerts` | `alerts.py` | `/data/3.0/onecall` |
| air_quality | `get_air_quality` | `air_quality.py` | `/data/2.5/air_pollution` |

Host `api.openweathermap.org`. No astronomy, historical, marine, nowcast, uv,
pollen, wildfire, space_weather or tides.

## Request shape

`lat`, `lon`, `appid`, and `units=metric` on the weather endpoints (the air
pollution and OneCall calls send no `units`). The alerts call adds
`exclude=minutely,hourly,daily` so only the `alerts` array comes back. No
custom headers.

## Response mapping

- `current.fetch()` -> `WeatherResult`. `weather[0].description` is
  title-cased; `main.temp`, `feels_like`, `humidity`, `pressure` map
  directly; `wind.speed` is m/s under `units=metric` and goes through
  `base.ms_to_kph()`; `wind.deg` through `base.deg_to_card()`; `visibility`
  is already metres. `dewpoint_c` is explicitly `None` because 2.5 does not
  report it, so `WeatherResult.derive_missing()` computes it from this
  observation's temperature and humidity ([base.md](../base.md)).
- `forecast.fetch()` -> `WeatherResult` with `temperature=None`. The 5-day /
  3-hour list is grouped by the date prefix of `dt_txt`, `high_c` / `low_c`
  are the max and min of the 3-hourly samples in that bucket, and the
  description is taken from the middle sample of the day.
- `hourly.fetch()` -> `HourlyResult` from the same endpoint. The module
  docstring carries a cadence warning: the free `/forecast` endpoint is
  3-hourly, so `hours` is interpreted as a count of 3-hour slices, and each
  `HourlyEntry.time` is the start of a 3-hour window. `pop` is a 0-1
  probability and is multiplied by 100; `rain.3h` supplies `precip_mm`.
- `alerts.fetch()` -> `AlertsResult`. OneCall alerts carry no structured
  severity, so `_classify()` keyword-matches the event name plus the `tags`
  array against `_SEVERITY_KEYWORDS` (extreme / severe / moderate / minor),
  defaulting to `"unknown"`. The comment records that this replaced a
  hardcoded `"moderate"` for every alert. `start` and `end` are Unix
  timestamps passed through as-is into the string fields.
- `air_quality.fetch()` -> `AirQualityResult`. OWM reports a 1-5 index, which
  `_codes.AQI_MAP` translates to representative US-AQI numbers
  (1->25, 2->75, 3->125, 4->175, 5->300) before `base.aqi_category()` labels
  them. Pollutant concentrations are passed through unchanged.

## Failure behavior

No module catches anything; `HTTPError` propagates and the dispatcher records
the failure. Two responses that are not errors upstream still matter here:

- `air_quality.fetch()` returns a bare `AirQualityResult(source, location)`
  when `list` is empty. `AirQualityResult` has no `is_empty()`, so the
  dispatcher treats it as a success and stops the chain (see Findings).
- A free-tier key on `/data/3.0/onecall` returns 401. The dispatcher calls
  `mark_auth_failure()` on 401/403, which opens the breaker for the whole
  provider - including its four working 2.5 capabilities - until the
  cooldown expires.

## Caching

None.

## Quirks and upstream limits

- Free tier is 60 calls/minute; `_DEFAULT_QUOTA_LIMITS` records 60000/day as
  a visibility marker, not an enforced cap, and the header comment in
  `__init__.py` explains the derivation.
- OneCall 3.0 is a separate subscription with its own 1000 calls/day free
  allowance; the package treats it as part of the same provider.
- The OWM air-quality index is its own 1-5 scale, not US EPA AQI, so the
  `aqi` field carries EPA-shaped numbers derived from a coarser scale.
- True 1-hour cadence is only available on OneCall 3.0; the free path cannot
  provide it, which is why the hourly module documents the mismatch instead
  of hiding it.

## Tests

- `tests/test_weather_flags.py` covers the `-openweathermap` / `-owm`
  aliases; `tests/test_dispatcher.py` includes it in the expected provider
  ordering; `tests/test_secret_store.py` uses `openweathermap_key` as its
  placeholder-skipping fixture.
- No test parses an OWM payload or exercises `_classify()` / `AQI_MAP`.

## Findings

- defect | `openweathermap/air_quality.py - fetch()` | An empty `list`
  returns a hollow `AirQualityResult` with every pollutant `None`. Because
  the class has no `is_empty()`, `_dispatch.dispatch()` records a success and
  returns it, so lower-ranked providers that do have data are never tried.
  This is the exact bug that was fixed for Tomorrow.io by raising instead of
  returning, and pinned by
  `tests/test_provider_fixes.py - TestTomorrowioAirQualityFallthrough`.
- questionable | `openweathermap/alerts.py - fetch()` | `start` and `end` are
  Unix epoch integers assigned to `AlertEntry.start` / `.end`, which every
  other provider fills with an ISO-8601 string; the formatter therefore
  receives two different time formats depending on which provider answered.
- questionable | `openweathermap/alerts.py - _classify()` | Keyword matching
  runs in list order, so "Severe Thunderstorm Watch" matches `severe` before
  `watch` is ever considered; the classification is a best-effort heuristic
  and is documented as such, but its result is presented as a severity equal
  in weight to NWS's CAP severity.
- questionable | `openweathermap/forecast.py - fetch()`,
  `openweathermap/hourly.py - fetch()` | Both key off `dt_txt` - the daily
  grouping uses `dt_txt[:10]` as the day bucket and the hourly label is
  `strftime("%I %p")` on the parsed naive string - while the response's
  `city.timezone` offset is never read. The implementation implies these
  timestamps are treated as location-local; if `dt_txt` is UTC, day
  boundaries and hour labels are shifted for locations far from UTC.
  [unverified - the upstream semantics of `dt_txt` were not checked against
  OpenWeatherMap's docs in this session] Contrast `openmeteo/hourly.py`,
  which aligns on `utc_offset_seconds`, and `weatherapi/hourly.py`, which
  filters on `time_epoch`; both have regression tests for exactly this.
- questionable | `openweathermap/air_quality.py - fetch()` | `AQI_MAP` labels
  a 1-5 European-style index with US EPA AQI numbers and categories, which
  makes an OWM answer look directly comparable to AirNow's real EPA AQI.
