# visualcrossing - Visual Crossing provider package (key, one Timeline endpoint)

## Purpose

Visual Crossing's Timeline Weather API, ranked 5 for current and forecast, 6
for hourly, 7 for alerts and 2 for historical (behind Open-Meteo). It is the
highest-ranked key-based provider for current conditions, and the only one in
this set where every capability is the same URL with a different path suffix
and `include=` list.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `visualcrossing` (`_reg("visualcrossing", _f_visualcrossing)`) |
| Factory | `__init__.py - _f_visualcrossing()` |
| Secret name | `visualcrossing_key` |
| Env override | `INTERNETS_VISUALCROSSING_KEY` |
| Ini fallback | `[weather_providers] visualcrossing_key` |
| Daily quota marker | 1000 in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-visualcrossing`, `-vc` |

Missing key means `visualcrossing: skipped` and no registration. The key is
the `key` query parameter over HTTPS.

## Capabilities

All five modules share the same `_BASE` constant and address the location as
the path segment `{lat},{lon}`:

```text
https://weather.visualcrossing.com/VisualCrossingWebServices
    /rest/services/timeline/{lat},{lon}{suffix}
```

| Capability | Method | Path suffix | `include=` |
| --- | --- | --- | --- |
| current | `get_weather` | `/today` | `current` |
| forecast | `get_forecast` | `/next{min(days,15)}days` | `days,current` |
| hourly | `get_hourly` | `/next24hours` | `hours` |
| alerts | `get_alerts` | `/today` | `alerts` |
| historical | `get_historical` | `/{date}/{date}` | `days` |

No air_quality, astronomy, marine, nowcast, uv, pollen, wildfire,
space_weather or tides.

## Request shape

Every call sends `unitGroup=metric`, `key`, `include`, and
`contentType=json`. No headers. Under `unitGroup=metric` the API returns
Celsius, km/h wind, hPa pressure, mm precipitation and km visibility, so the
only conversion the package performs is km to metres for visibility.

## Response mapping

- `current.fetch()` -> `WeatherResult` from `currentConditions`: `temp`,
  `conditions`, `feelslike`, `humidity`, `windspeed` (already km/h),
  `winddir` in degrees through `base.deg_to_card()`, `pressure`,
  `visibility * 1000`, `dew`.
- `forecast.fetch()` -> `WeatherResult` carrying both `currentConditions` and
  `days[:days]` mapped to `ForecastDay` (`tempmax`, `tempmin`, `conditions`),
  weekday derived from the `datetime` date string.
- `hourly.fetch()` -> `HourlyResult`. Hours are flattened across the returned
  days and filtered by `datetimeEpoch < time.time()`, the timezone-safe
  comparison the comment calls out; collection stops at `hours` entries.
  `precipprob` maps to `precip_chance`, `winddir` through `deg_to_card()`.
- `alerts.fetch()` -> `AlertsResult` from the `alerts` array: `event`,
  lowercased `severity` with an `"unknown"` fallback, `headline`, `onset` /
  `ends`, description truncated to 300 characters.
- `historical.fetch()` -> `HistoricalResult` from `days[0]`, raising
  `ValueError("No historical data")` when the array is empty. `target_date`
  defaults to yesterday. `windspeed` on a daily record is the day's maximum
  and is stored in `max_wind_kph`; `humidity` is the daily mean.

## Failure behavior

`HTTPError` propagates from all five modules. Only `historical.fetch()`
raises on an empty payload; the others build a result from whatever the
response contained, which for `current` means a `WeatherResult` with
`temperature=None` that `is_empty()` catches and the dispatcher falls through
on ([dispatch.md](../dispatch.md)). An empty `alerts` array is a legitimate
"no alerts" answer, not a fall-through. A 401/403 from a bad key triggers
`mark_auth_failure()`.

## Caching

None.

## Quirks and upstream limits

- Free tier is 1000 records per day, matching the quota marker. Visual
  Crossing bills by record rather than by request, so a 15-day forecast costs
  more of the daily allowance than a single-day query while
  `_dispatch.record_call()` counts both as one. [unverified - the record
  billing model was not checked against upstream docs in this session]
- `forecast` is clamped to 15 days by the module, below the package-wide
  `_MAX_FORECAST_DAYS = 16`.
- `hourly` requests `next24hours`, so any `hours` above 24 silently returns
  at most 24 entries even though `get_hourly()` accepts up to 48.
- The `include=current` mode returns `currentConditions` as the station or
  model value for the current hour, not a separate observation.
- ERA5 reanalysis backs the historical path, which is why it ranks 2 behind
  Open-Meteo, which exposes the same source.

## Tests

- `tests/test_weather_flags.py` covers the `-visualcrossing` / `-vc`
  aliases; `tests/test_dispatcher.py` includes it in the expected default
  provider ordering.
- No test parses a Visual Crossing payload. In particular the
  `datetimeEpoch` past-hour filter in `hourly.fetch()` has no counterpart to
  the WeatherAPI and Open-Meteo tests in
  `tests/test_new_weather_capabilities.py - TestTimezoneWindows`, even
  though it is the same class of timezone-window logic those tests were
  written to protect.

## Findings

- questionable | `visualcrossing/hourly.py - fetch()` | The endpoint is
  hardcoded to `next24hours` while the dispatcher permits `hours` up to 48
  (`weather_providers/__init__.py - get_hourly()` clamps at 48), so a request
  for more than a day silently returns a short answer rather than falling
  through to a provider that can serve it.
- questionable | `visualcrossing/hourly.py - fetch()` | The
  `if len(entries) >= hours: break` guard sits after the timestamp is
  formatted, so the last iteration parses a datetime it then discards; and
  when `datetimeEpoch` is missing the entry is kept regardless of age, so a
  payload without epochs yields past hours.
- questionable | `visualcrossing/historical.py - fetch()` | `windspeed` on a
  daily record is stored in `max_wind_kph`, and `humidity` in
  `avg_humidity`, without any check that the daily aggregation matches those
  field names; the mapping is plausible but the code makes an assumption the
  response does not label.
- test-gap | `visualcrossing/hourly.py - fetch()` | The past-hour epoch
  filter is untested, unlike the equivalent logic in `weatherapi/hourly.py`
  and `openmeteo/hourly.py`, both of which have regression tests.
