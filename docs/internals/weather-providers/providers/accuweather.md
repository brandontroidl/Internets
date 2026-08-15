# accuweather - AccuWeather provider package (key, location-key indirection)

## Purpose

AccuWeather's developer API, ranked 6 for current and forecast, 11 for hourly
and 5 for alerts. It is the only provider in this set whose endpoints are not
addressed by coordinates: every request needs an AccuWeather "location key",
so the package carries a geoposition lookup and an LRU cache in front of the
endpoint modules.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `accuweather` (`__init__.py - _reg("accuweather", _f_accuweather)`) |
| Factory | `__init__.py - _f_accuweather()` |
| Secret name | `accuweather_key` |
| Env override | `INTERNETS_ACCUWEATHER_KEY` (`secret_store.ENV_PREFIX`) |
| Ini fallback | `[weather_providers] accuweather_key` |
| Daily quota marker | 50 in `_DEFAULT_QUOTA_LIMITS` |
| User flag | `-accuweather`, `-acc` |

The factory calls `_cred(cfg, "accuweather_key", "accuweather_key")`, which
tries `secret_store.get()` first (env var, then the 0600 `[secrets]` section)
and falls back to `[weather_providers]` in config.ini. With no key the
factory logs `accuweather: skipped` and returns `None`, so the provider is
never registered. See `docs/internals/secret_store.md` for the store itself.

The key travels as the `apikey` query parameter. Every URL in the package is
`https://`, and each module carries a comment recording that it used to be
`http://`, which put the key in plaintext on the wire.

## Capabilities

| Capability | Method | Module | Endpoint |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `/currentconditions/v1/{key}` |
| forecast | `get_forecast` | `forecast.py` | `/forecasts/v1/daily/5day/{key}` |
| hourly | `get_hourly` | `hourly.py` | `/forecasts/v1/hourly/12hour/{key}` |
| alerts | `get_alerts` | `alerts.py` | `/alerts/v1/{key}` |

Host: `dataservice.accuweather.com`. No air_quality, astronomy, historical,
marine or nowcast method exists.

## Location-key resolution and caching

`AccuWeatherProvider._lk()` calls the module-level
`_get_location_key(key, lat, lon)`, which:

1. builds a cache key `f"{lat:.2f},{lon:.2f}"` (roughly 1 km buckets);
2. on a hit, moves the entry to the end of `_LOC_CACHE` and returns it;
3. on a miss, GETs
   `/locations/v1/cities/geoposition/search?apikey=...&q=lat,lon`, stores
   `data["Key"]`, and evicts the oldest entries past `_LRU_MAX = 512`.

`_LOC_CACHE` is an `OrderedDict` at module scope, so it is shared by every
`AccuWeatherProvider` instance and survives `configure()` re-registration. It
has no TTL. The comment records that it was previously an unbounded dict.

Consequence for the 50-calls-per-day free tier: a cache miss makes every
capability cost two upstream requests, and `_dispatch.py` counts one
`record_call()` per dispatch, not per HTTP request, so `quota_status()`
under-reports actual usage on misses.

## Request shape

- `current.py`: `apikey`, `details=true`.
- `forecast.py`: `apikey`, `metric=true`. The endpoint segment is hardcoded
  to `5day`; the comment records that the previous
  `"5day" if days <= 5 else "5day"` had two identical branches and that the
  10/15-day endpoints are paid.
- `hourly.py`: `apikey`, `metric=true`, `details=true`.
- `alerts.py`: `apikey` only.

## Response mapping

- `current.fetch()` -> `WeatherResult`. The response is a single-element
  list; the module takes `data[0]` (or the object itself if it is not a
  list) and raises `ValueError("No data")` on an empty payload. Values live
  under `.Metric.Value` for temperature, RealFeel, wind speed, visibility,
  pressure and dew point. `Visibility` is km and is multiplied by 1000 for
  `visibility_m`; `Wind.Direction.English` is already a cardinal string.
- `forecast.fetch()` -> `WeatherResult` with `temperature=None` and
  `description=""`; days come from `DailyForecasts[:days]` with
  `Temperature.Maximum/Minimum.Value` and `Day.IconPhrase`.
- `hourly.fetch()` -> `HourlyResult`. With `metric=true`,
  `Temperature.Value` is Celsius, `Rain.Value` is mm and `Wind.Speed.Value`
  is km/h. `PrecipitationProbability` maps straight to `precip_chance`.
- `alerts.fetch()` -> `AlertsResult`. `Description.Localized` is used for
  both `event` and `headline`; start, end and the description text come from
  `Area[0]`. Severity comes from `_severity_from_priority()`, which maps the
  integer `Priority` 1-5 onto extreme / severe / moderate / minor / minor;
  the comment records the previous version calling `.lower()` on an int and
  raising `TypeError`.

## Failure behavior

`ValueError` from `current.fetch()` and any `HTTPError` propagate to the
dispatcher. `ValueError` is not in `_dispatch._BUG_EXC_TYPES`, so it is
logged as an ordinary `dispatch_fail`; a `TypeError` or `KeyError` from a
shape change would be logged at ERROR as a provider defect
([dispatch.md](../dispatch.md)). A 401/403 from a bad or expired key makes
the dispatcher call `mark_auth_failure()`, which opens the breaker
immediately instead of burning one of the 50 daily calls per dispatch.

## Quirks and upstream limits

- Free tier is 50 calls per day, matching `_DEFAULT_QUOTA_LIMITS`. With the
  location-key hop this is effectively 25 cold answers per day.
- Daily forecast is capped at 5 days regardless of the `days` argument, and
  the hourly endpoint is the 12-hour one, so `hours` above 12 silently
  returns 12 entries.
- Location keys are place-level, so two nearby coordinates that resolve to
  the same city return identical data; the 2-decimal cache key deliberately
  leans on that.
- The package docstring records that HTTPS is used on all tiers and that
  falling back to `http://` would be a documented downgrade, not a fix.

## Tests

- `tests/test_weather_flags.py` covers the `-accuweather` / `-acc` aliases
  and asserts that `-aw` resolves to WeatherKit, not AccuWeather.
- `tests/test_dispatcher.py` includes `accuweather` in the expected default
  provider ordering.
- No test exercises the location-key cache, the priority-to-severity map, or
  any endpoint parsing.

## Findings

- questionable | `accuweather/__init__.py - _get_location_key()` | When the
  geoposition search returns no `Key`, the function returns `""` and the
  caller builds a URL ending in `/v1/`, spending a second upstream call to
  get a 400 or 404 instead of failing at the lookup.
- questionable | `accuweather/__init__.py - _LOC_CACHE` | Module-global with
  no TTL and no invalidation on `configure()`, so a location key stays cached
  for the process lifetime; correct in practice because keys are stable, but
  it also means a wrong key cached from a bad response is permanent.
- questionable | `_dispatch.DEFAULT_RELIABILITY["air_quality"]` lists
  `accuweather` at rank 10 although `AccuWeatherProvider` has no
  `get_air_quality`; capability discovery uses `hasattr`, so the entry is
  inert, but the table claims support that does not exist.
- test-gap | `accuweather/alerts.py - _severity_from_priority()` | The
  function exists because a `TypeError` crash was fixed here, and there is no
  regression test pinning the int-priority path.
