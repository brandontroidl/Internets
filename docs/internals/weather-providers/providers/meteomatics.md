# meteomatics - Meteomatics provider (parameter-path API, HTTP Basic auth)

## Purpose

Wraps Meteomatics' commercial API, the second-ranked provider for current,
forecast and hourly (behind NWS). It is the only provider in the tree that
authenticates with HTTP Basic and the only one whose request encodes the
requested variables, the validity time and the coordinates into the URL *path*.
Registered id `meteomatics`, factory
`weather_providers/__init__.py - _f_meteomatics()`.

## Responsibilities / boundaries

Belongs here: Basic-auth header construction, the parameter/time path grammar,
and the transposition of Meteomatics' parameter-major JSON into the
time-major dataclasses in [base.py](../base.md). Not here: transport
([_http.py](../http.md)), selection and health ([_dispatch.py](../dispatch.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `meteomatics` (`_reg("meteomatics", _f_meteomatics)`) |
| Class | `MeteomaticsProvider(username, password)` |
| `name` / `requires_key` | `"Meteomatics"` / `True` |
| Quota limit | `None` (`_DEFAULT_QUOTA_LIMITS`) |

Two credentials, `meteomatics_username` and `meteomatics_password`, each
resolved by `__init__.py - _cred()`: `secret_store.get()` first (env
`INTERNETS_METEOMATICS_USERNAME` / `INTERNETS_METEOMATICS_PASSWORD`, then
`config.ini [secrets]`), then `config.ini [weather_providers]`. Either one
missing means the factory returns `None`.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 2 |
| forecast | `get_forecast` | 2 |
| hourly | `get_hourly` | 2 |
| nowcast | *not implemented* | 2 (dead entry, see Findings) |

Flags: `-meteomatics`, `-mm`.

## Endpoints and request shape

```text
https://api.meteomatics.com/{validity}/{parameters}/{lat},{lon}/json
```

- current: `validity` is the current UTC instant (`%Y-%m-%dT%H:%M:%SZ`);
  `parameters` is the fixed six-variable list
  `t_2m:C,wind_speed_10m:kmh,wind_dir_10m:d,msl_pressure:hPa,
  relative_humidity_2m:p,dew_point_2m:C`.
- forecast: `validity` is `start--end:P1D` from today 00:00 UTC to
  `days` ahead, `parameters` is `t_max_2m_24h:C,t_min_2m_24h:C`.
- hourly: `validity` is `start--end:PT1H` from the current hour, `parameters`
  adds `precip_1h:mm` and `prob_precip_1h:p` to temperature and wind.

Auth is `Authorization: Basic base64(user:password)`, rebuilt per call by
`MeteomaticsProvider._auth()` and passed as `headers=`. Units are requested in
the parameter names themselves (`:kmh`, `:hPa`, `:C`, `:p`), so no client-side
conversion is needed.

## Response parsing

Meteomatics returns `data: [{parameter, coordinates: [{dates: [{date, value}]}]}]`,
which is parameter-major. Each module transposes it:

- `current.fetch()` takes `coordinates[0]["dates"][0]["value"]` per parameter
  into a flat `vals` dict, then maps by parameter name.
- `forecast.fetch()` builds `highs` / `lows` keyed by the date prefix
  (`date[:10]`), distinguishing the two series by testing for `"max"` or
  `"min"` in the parameter name, then emits `ForecastDay` per sorted date.
- `hourly.fetch()` builds `by_time[date][parameter]`, then emits `HourlyEntry`
  per sorted timestamp, labelling hours `%I %p` in UTC.

`base.deg_to_card()` converts `wind_dir_10m:d` to a cardinal string. No
condition-code table exists: current sets `description="Current"`, forecast and
hourly set `""`.

## Lifecycle, state, concurrency

One instance per `configure()`, holding the username and password strings. No
caches, no disk, no module state. Each method is a single
`await get_json(...)`; `_auth()` re-encodes the header on every call, which is
cheap and keeps no derived credential around.

## Failure behavior

Everything propagates as `HTTPError` from [_http.py](../http.md). A 401/403
(bad credentials or an expired trial account) additionally trips
`health.mark_auth_failure()` in `_dispatch.Dispatcher.dispatch()`. A response
with an empty `data` array yields a `WeatherResult` with `temperature=None` and
no forecast days, so `is_empty()` is true and the dispatcher falls through
without recording a success.

## Security

Credentials are never in the URL, so they cannot leak through `HTTPError`
messages (which format the base URL, not headers). They are held in memory as
plain strings for the process lifetime, which matches every other keyed
provider. Base64 is an encoding, not encryption: the header is protected only
by TLS, which is the standard HTTP Basic trade-off and is the scheme
Meteomatics offers.

## Classes

`MeteomaticsProvider` - credential holder, `_auth()` header builder, and three
`async get_*` delegators. Extension constraint: a new capability must match the
`get_<cap>(self, lat, lon, location, **kw)` naming in
`_dispatch.CAPABILITY_METHODS`.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `MeteomaticsProvider._auth()` | Build the Basic header per call |
| `current.fetch(headers, lat, lon, loc)` | Instant query to `WeatherResult` |
| `forecast.fetch(..., days)` | `P1D` min/max series to `ForecastDay` list |
| `hourly.fetch(..., hours)` | `PT1H` series to `HourlyEntry` list |

## Tests

`tests/test_dispatcher.py` asserts `meteomatics` is a registered factory in the
known-provider set and, indirectly, that it ranks ahead of lower-tier providers
for current. `tests/test_weather_flags.py` pins `-mm` / `-meteomatics`. No test
constructs the provider or exercises a `fetch()`.

## Findings

- **defect** | `_dispatch.DEFAULT_RELIABILITY["nowcast"]` vs
  `meteomatics/__init__.py - MeteomaticsProvider` - meteomatics is ranked 2 for
  nowcast, but the class implements no `get_nowcast`, so
  `_dispatch._RegisteredProvider.__init__`'s `hasattr` discovery never adds the
  `nowcast` capability and the rank is unreachable. Confirmed: the package
  imports only `current, forecast, hourly`, and the class defines exactly three
  `get_*` methods. The ranking comment ("Meteomatics (RTMA+radar)") documents an
  intent that was never implemented.
- **questionable** | `meteomatics/current.py - fetch()` - `description` is the
  literal string `"Current"`. Because `description` is one of
  `base._CURRENT_GAP_FIELDS` and `WeatherResult.fill_gaps()` only fills fields
  that are missing, this non-description permanently blocks gap-filling of the
  conditions text from a lower-ranked provider, and the formatter prints
  "Current" as the conditions. Meteomatics ranks 2, so it is reached often.
- **questionable** | `meteomatics/forecast.py - fetch()` - series are separated
  by substring-testing the parameter name for `"max"` / `"min"`. Any future
  parameter containing either substring would be silently misfiled.
- **questionable** | `meteomatics/hourly.py - fetch()` - the requested window
  starts at the current UTC hour (so no past hours arrive), but the labels are
  formatted from the UTC timestamps with no conversion to the queried
  location's zone.
- **test-gap** | `meteomatics/*` - no coverage of the parameter-major to
  time-major transposition, which is the only non-trivial logic in the package.
