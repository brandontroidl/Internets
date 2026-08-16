# pirateweather - Dark Sky compatible provider (path-embedded key, 5 capabilities)

## Purpose

Wraps Pirate Weather, a Dark Sky API clone. It is the top-ranked nowcast
provider (rank 1) and the only one in this group whose API key lives in the URL
*path* rather than a query parameter, which forces a redaction wrapper.
Registered id `pirateweather`, factory
`weather_providers/__init__.py - _f_pirateweather()`.

## Responsibilities / boundaries

Belongs here: the Dark Sky request shape (`units=si` plus an `exclude` list per
capability), icon-to-prose mapping, and key redaction on error. Not here:
transport ([_http.py](../http.md)), selection and health
([_dispatch.py](../dispatch.md)), result shapes ([base.py](../base.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `pirateweather` (`_reg("pirateweather", _f_pirateweather)`) |
| Class | `PirateWeatherProvider(api_key)` |
| `name` / `requires_key` | `"Pirate Weather"` / `True` |
| Quota limit | `10_000` per day (`_DEFAULT_QUOTA_LIMITS`) |

Single credential `pirateweather_key`, resolved by `__init__.py - _cred()`:
`secret_store.get("pirateweather_key")` (env `INTERNETS_PIRATEWEATHER_KEY`,
then `config.ini [secrets]`), falling back to
`config.ini [weather_providers] pirateweather_key`. Missing key logs at info
and the factory returns `None`, so the provider never registers.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 10 |
| forecast | `get_forecast` | 10 |
| hourly | `get_hourly` | 5 |
| alerts | `get_alerts` | 4 |
| nowcast | `get_nowcast` | 1 |

Flags: `-pirateweather`, `-pirate`, `-pw` (`modules/weather.py -
_PROVIDER_FLAGS`).

## Endpoint and request shape

```text
https://api.pirateweather.net/forecast/{KEY}/{lat},{lon}
    ?units=si&exclude=<blocks not needed for this capability>
```

Each endpoint module holds its own `_BASE` constant and its own `exclude` list,
so every capability is one full request that discards the blocks it does not
need: current excludes `minutely,hourly,daily,alerts`; forecast excludes
`minutely,hourly,alerts`; hourly excludes `minutely,daily,alerts`; alerts
excludes `minutely,hourly,daily`; nowcast excludes `hourly,daily,alerts`.

## Response parsing

`units=si` means metres per second for wind and kilometres for visibility, so
`current.fetch()` applies `base.ms_to_kph()` and multiplies visibility by 1000;
`humidity` is a 0-1 fraction scaled to 0-100. `feels_like_c` and `dewpoint_c`
come straight from `apparentTemperature` / `dewPoint`.

- `forecast.fetch()`: `daily.data[:days]`, `temperatureHigh` / `temperatureLow`,
  day name from `datetime.fromtimestamp(d["time"])` formatted `%A`.
- `hourly.fetch()`: `hourly.data[:hours]`; `precipIntensity` becomes
  `precip_mm`, `precipProbability` is scaled to 0-100.
- `alerts.fetch()`: `alerts[]`; `title` fills both `event` and `headline`,
  `description` truncated to 300 characters.
- `nowcast.fetch()`: `minutely.summary` plus `minutely.data`; `_intensity()`
  buckets `precipIntensity` into none / light / moderate / heavy at 0.1, 2.5
  and 7.6 mm; `precip_type` is forced to `"none"` when there is no precipitation.

`_codes.icon_to_desc()` maps the 13-entry Dark Sky `ICONS` table, passing an
unknown icon through and yielding `"Unknown"` for a missing one.

## Lifecycle

Constructed by the factory during `configure()`, holds only the key string,
lives until `dispatcher.clear()`. Endpoint modules are imported eagerly by the
package `__init__`.

## State

`PirateWeatherProvider._key` only. No caching, no disk, no module-level
mutable state.

## Concurrency

Stateless per call; every method is a single `await` on `safe_get_json()`. Safe
to call concurrently for different locations.

## Failure behavior

All errors arrive as `HTTPError` and are re-raised by
`_codes.safe_get_json()` with the key scrubbed, preserving `.status` and
`.is_rate_limit` so `_dispatch._is_rate_limit_error()` still classifies a 429
structurally. Empty upstream blocks produce a `WeatherResult` with
`temperature=None` and no forecast, which `WeatherResult.is_empty()` reports
and the dispatcher uses to fall through without recording a success.

## Security

The whole reason `safe_get_json()` exists: Pirate Weather offers no header
auth, so the key sits in the URL path and would otherwise appear in
`HTTPError`'s message (`"HTTP {status} for {url}"`) and in the
`provider_hint`, and from there in dispatcher warning logs. The wrapper
rebuilds the exception with `_redact_key()` applied to both, and raises
`from None` so the original (unredacted) exception is not chained into the
traceback. `_dispatch._redact()` would not help here, since it only strips
`key=`-style query parameters.

## Classes

`PirateWeatherProvider` - key holder plus five `async get_*` delegators, one
per endpoint module. Any added capability must use the
`get_<cap>(self, lat, lon, location, **kw)` shape from
`_dispatch.CAPABILITY_METHODS`.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `_codes.icon_to_desc(icon)` | Dark Sky icon to prose, passthrough on miss |
| `_codes._redact_key(s, key)` | Literal substring replace with `[REDACTED]` |
| `_codes.safe_get_json(url, key, **kw)` | `get_json` wrapper, scrubs then re-raises |
| `nowcast._intensity(mm)` | mm/h to none / light / moderate / heavy |
| `current.fetch(key, lat, lon, loc)` | `currently` block to `WeatherResult` |
| `forecast.fetch(..., days)` | `daily.data` to `ForecastDay` list |
| `hourly.fetch(..., hours)` | `hourly.data` to `HourlyEntry` list |
| `alerts.fetch(...)` | `alerts[]` to `AlertEntry` list |
| `nowcast.fetch(...)` | `minutely` to `NowcastResult` |

## Tests

`tests/test_weather_flags.py` pins `-pw` / `-pirate` / `-pirateweather` to this
id. `tests/test_dispatcher.py` asserts the id is a registered factory and part
of the known-provider set. No test exercises `safe_get_json()`'s redaction or
any `fetch()` in this package.

## Findings

- **questionable** | `pirateweather/{forecast,hourly,nowcast}.py` -
  `datetime.fromtimestamp()` without a tzinfo renders labels in the *bot host's*
  local zone, not the queried location's. `tests/test_new_weather_capabilities.py
  - TestTimezoneWindows` exists precisely for this class of defect but covers
  only weatherapi and openmeteo.
- **questionable** | `weather_providers/__init__.py - _DEFAULT_QUOTA_LIMITS` -
  the entry is `10_000` with the comment "10k/mo free tier", while
  `pirateweather/__init__.py`'s docstring says 20,000 calls/month. Both numbers
  cannot be right, and either way a monthly ceiling is stored in a field that
  `quota_status()` resets at UTC midnight, so the counter can never reach it.
- **questionable** | `pirateweather/_codes.py - safe_get_json()` - catches only
  `HTTPError`. A non-`HTTPError` exception raised below (or a subclass carrying
  extra attributes, such as `ResponseTooLargeError`'s `size` / `limit`, which
  the rebuild drops) escapes unredacted or degraded.
- **test-gap** | `pirateweather/*` - no coverage of key redaction, the
  `exclude` list per capability, or the nowcast intensity buckets.
