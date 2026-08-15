# metno - MET Norway / Yr provider package (keyless, one shared timeseries)

## Purpose

api.met.no, the Norwegian Meteorological Institute's public API. Keyless like
Open-Meteo and NWS, but ranked last among the model providers (13 for
current, forecast and hourly; 12 for alerts; 4 for nowcast), so in practice it
serves as a keyless backstop and as the alerts/nowcast source for Norway and
the Nordics.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `metno` (`__init__.py - _reg("metno", _f_metno)`) |
| Factory | `__init__.py - _f_metno()` - constructs with no arguments |
| Credential | None. `MetNoProvider.requires_key = False` |
| Daily quota marker | Absent from `_DEFAULT_QUOTA_LIMITS` (resolves to None) |
| User flag | `-metno`, `-yr` |

No key, but a real `User-Agent` is mandatory: every module sends
`"Internets-IRC-Bot/2.x github.com/brandontroidl/Internets"` and
`current.py` records why ("api.met.no rejects missing/generic User-Agent with
403"). The string is hardcoded per module, not read from configuration.

## Capabilities

| Capability | Method | Module | Endpoint |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `locationforecast/2.0/compact` |
| forecast | `get_forecast` | `forecast.py` | `locationforecast/2.0/compact` |
| hourly | `get_hourly` | `hourly.py` | `locationforecast/2.0/compact` |
| alerts | `get_alerts` | `alerts.py` | `metalerts/2.0/current.json` |
| nowcast | `get_nowcast` | `nowcast.py` | `nowcast/2.0/complete` |

`tests/test_new_weather_capabilities.py -
TestCapabilityDiscovery.test_capabilities` pins exactly this set.

## Request shape

Every call is one GET with `lat` and `lon` rounded to four decimals
(`round(lat, 4)`), which is what MET Norway's terms require so their cache can
work. No units parameter exists: the API is SI throughout - Celsius, m/s,
hPa, mm.

Three of the five capabilities hit the same `locationforecast/2.0/compact`
document and parse different slices of it. There is no shared fetch, so a
`current` plus `forecast` reply is two identical HTTP requests.

## Response mapping

The compact document is `properties.timeseries[]`, each entry having
`data.instant.details` (point values) and `data.next_1_hours` /
`next_6_hours` (summary symbol and precipitation).

- `current.fetch()` -> `WeatherResult` from `timeseries[0]`.
  `wind_speed` is m/s and goes through `base.ms_to_kph()`;
  `air_pressure_at_sea_level` is already hPa; `dew_point_temperature` maps to
  `dewpoint_c`. `feels_like_c` and `visibility_m` are never populated, so the
  dispatcher's `derive_missing()` computes the apparent temperature from this
  observation's own values ([base.md](../base.md)). The symbol code
  (`partlycloudy_day`) is normalised by `_humanize()`, which strips the
  `_day` / `_night` / `_polartwilight` suffix and replaces underscores.
- `forecast.fetch()` -> `WeatherResult`. Timeseries entries are grouped by
  calendar date; `high_c` / `low_c` are the max and min of the instant
  temperatures in that group, and the representative symbol is the one
  nearest midday (hours 11-13), falling back to the first symbol seen. The
  first entry also supplies the result's current temperature and description.
- `hourly.fetch()` -> `HourlyResult` from the first `hours` timeseries
  entries, with `precip_mm` from `next_1_hours.details.precipitation_amount`.
- `alerts.fetch()` -> `AlertsResult` from the metalerts GeoJSON features.
  `_severity()` prefers the explicit `severity` property and otherwise parses
  the third field of `awareness_level` ("2; yellow; Moderate"). `start` /
  `end` come from `interval` or `time`, whichever is a list.
- `nowcast.fetch()` -> `NowcastResult` from the radar nowcast timeseries,
  with the same `_intensity()` bucketing as Open-Meteo's nowcast.

## Failure behavior

- `current.fetch()` returns a `WeatherResult` with `temperature=None` when
  `timeseries` is empty; `WeatherResult.is_empty()` then makes the dispatcher
  fall through without treating it as a success.
- `nowcast.fetch()` is the only module that inspects status codes: a 422
  (outside Nordic radar coverage) is re-raised as an `HTTPError` with
  `status=None`, and a `meta.radar_coverage` that is not `"ok"` raises the
  same way. Both are recorded by the dispatcher as provider failures.
- Everything else propagates unmodified from `_http.get_json()`.

## Caching

None in the package. Coordinate rounding exists to help MET Norway's own
cache, not ours.

## Quirks and upstream limits

- `metalerts` covers Norway only, so an alerts query anywhere else returns an
  empty feature list, which is an `AlertsResult` with zero entries rather than
  a fall-through.
- `nowcast/2.0` covers Nordic radar only.
- The compact locationforecast series is hourly for roughly the first two to
  three days and then coarser, so `hourly` requests near the 48-hour cap and
  multi-day `forecast` grouping both silently mix cadences.
- No published call quota; MET Norway rate-limits by User-Agent and expects
  conditional requests, which this package does not send.

## Tests

- `tests/test_new_weather_capabilities.py` pins the capability set and (via
  `_NEW_PROVIDERS`) that every `get_*` accepts `**kwargs`.
- `tests/run_tests.py - "MetNoProvider: multi-capability, no key"` asserts
  `requires_key is False` and that all five methods are coroutines.
- `tests/test_dispatcher.py - test_metno_ranked_for_its_capabilities` exists
  because metno was once registered but missing from every reliability map,
  which silently gave it rank 99.
- No test parses a real met.no payload shape.

## Findings

- defect | `metno/hourly.py - fetch()`, `metno/nowcast.py - fetch()` | The
  time label is built with
  `datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%I %p")`, which
  formats a UTC-aware datetime in UTC. Every other provider's hourly labels
  are location-local (Open-Meteo aligns on `utc_offset_seconds`, WeatherAPI
  and Visual Crossing use local strings), so a metno hourly reply is shifted
  by the location's UTC offset with no indication.
- questionable | `metno/nowcast.py - fetch()` | Being outside radar coverage
  is raised as an `HTTPError` and therefore recorded as a provider failure,
  which can trip metno's circuit breaker for a location it will never cover.
  `nws/_scope.py` solves the same problem the other way, by returning `None`
  so the dispatcher falls through without recording a failure.
- questionable | `metno/nowcast.py - fetch()` | `steps` defaults to 8 and
  `MetNoProvider.get_nowcast()` never forwards a caller-supplied value.
- questionable | `metno/current.py`, `forecast.py`, `hourly.py` | `_humanize()`
  is copy-pasted three times, and the same `_BASE` / `_HEADERS` constants are
  duplicated across all five modules; there is no package-level helper.
- questionable | `metno/forecast.py - fetch()` | The first group is a partial
  day (only the hours from now onward), so day one's high and low are the
  extremes of the remaining hours rather than of the whole day.
