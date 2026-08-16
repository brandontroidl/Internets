# weatherkit - Apple WeatherKit provider (JWT ES256 auth, 4 capabilities)

## Purpose

Wraps Apple's WeatherKit REST API. It is the only provider in the tree that
mints its own bearer credential: an ES256 JWT signed with an Apple Developer
`.p8` private key held on disk. Registered id `weatherkit`, factory
`weather_providers/__init__.py - _f_weatherkit()`.

## Responsibilities / boundaries

Belongs here: JWT construction and refresh, private-key permission and PEM
validation, the per-request URL, and normalization of Apple's four datasets
into the dataclasses in [base.py](../base.md). Not here: HTTP transport and
the byte cap ([_http.py](../http.md)), provider selection and failure
accounting ([_dispatch.py](../dispatch.md)), and IRC formatting
(`modules/weather.py`).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `weatherkit` (`__init__.py - _reg("weatherkit", _f_weatherkit)`) |
| Class | `WeatherKitProvider(team_id, service_id, key_id, key_file)` |
| `name` / `requires_key` | `"Apple Weather"` / `True` |
| Quota limit | `None` (`_DEFAULT_QUOTA_LIMITS["weatherkit"]`) |

Four credentials, each resolved by `__init__.py - _cred()` through
`secret_store.get()` (env var `INTERNETS_<NAME_UPPER>`, then `config.ini`
`[secrets]`) with a `config.ini [weather_providers]` fallback:
`weatherkit_team_id`, `weatherkit_service_id`, `weatherkit_key_id`,
`weatherkit_key_file`. So `INTERNETS_WEATHERKIT_TEAM_ID` and siblings override
the file. `weatherkit_key_file` is a filesystem *path*, not key material.

The factory first checks `import jwt` and returns `None` with a warning when
PyJWT is absent (extra `internets-irc[weatherkit]`, pinned
`PyJWT>=2.13.0` / `cryptography>=50.0.0` in `pyproject.toml`). It then counts
missing credentials and logs only the aggregate count, deliberately never the
names, to keep CodeQL's clear-text-logging heuristic from binding secret values
(comment in `_f_weatherkit()`).

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 3 |
| forecast | `get_forecast` | 3 |
| hourly | `get_hourly` | 3 |
| alerts | `get_alerts` | 2 |

Ranks from `_dispatch.DEFAULT_RELIABILITY`. User-facing flags for this provider:
`-weatherkit`, `-wk`, `-apple`, `-appleweather`, `-aw`
(`modules/weather.py - _PROVIDER_FLAGS`).

## Endpoint and request shape

One URL for every capability, built by `WeatherKitProvider._url()`:

```text
https://weatherkit.apple.com/api/v1/weather/en/{lat:.4f}/{lon:.4f}
```

The capability is selected purely by the `dataSets` query parameter:
`currentWeather` (current), `currentWeather,forecastDaily` (forecast),
`forecastHourly` (hourly), `weatherAlerts` (alerts). Auth is
`Authorization: Bearer <jwt>` from `WeatherKitProvider._headers()`. Language is
pinned to `en` in the path.

## Response parsing

- `current.fetch()`: `currentWeather` object. `humidity` is a 0-1 fraction and
  is multiplied by 100; `windSpeed` is already km/h; `visibility` is already
  metres; `pressure` is hPa. `temperatureApparent` and `temperatureDewPoint`
  populate `feels_like_c` / `dewpoint_c` natively, so
  `WeatherResult.derive_missing()` has nothing to compute.
- `forecast.fetch()`: `forecastDaily.days[:days]`, each mapped to a
  `ForecastDay`; `forecastStart` is parsed as ISO-8601 (with `Z` rewritten to
  `+00:00`) and rendered `%A`, falling back to the raw first 10 characters.
  Current temperature is taken from the same response's `currentWeather`.
- `hourly.fetch()`: `forecastHourly.hours[:hours]`; `precipitationChance` is a
  fraction scaled to 0-100; hour labels are `%I %p` in **UTC** (no local-zone
  conversion).
- `alerts.fetch()`: `weatherAlerts.alerts`; `_pick()` returns the first
  non-empty string among candidate keys, `_SEV` narrows `severity` to the
  five-value vocabulary of `base.AlertEntry`, and `_fetch_detail()` optionally
  GETs the alert's `detailsUrl` for a long body (truncated to 300 chars).

Condition codes are mapped by `_codes.readable()` over a 33-entry
`CONDITION_CODES` table; an unknown code passes through verbatim, and a missing
one becomes `"Unknown"`.

## Lifecycle

`configure()` calls the factory once per config load or module reload. The
constructor resolves the key path, asserts it is a file, and calls
`_read_private_key()` immediately so a bad path, bad mode, or non-PEM file
fails at startup rather than at the first lookup. A constructor raise is caught
by `configure()`, logged, and the provider is simply not registered. There is
no teardown; the object dies with the dispatcher on `clear()`.

## State

Instance state only: the three id strings, the resolved `Path` to the key, and
the JWT cache (`_token`, `_token_exp`). The private key text is never retained
between refreshes. No module-level state, no disk writes, no caching of weather
responses.

## Concurrency

All four methods are `async` but do no `await` before `_headers()`, which
performs a synchronous file read plus an ES256 signature on the event loop
about once an hour (`_JWT_LIFETIME = 55 * 60`, refreshed 60 s early). Nothing
guards `_token`; two coroutines racing a refresh both mint a token and the last
writer wins, which is harmless (either token is valid). Everything downstream
is `_http.get_json()`.

## Failure behavior

Transport, status, decode, and oversize failures surface as `HTTPError` from
[_http.py](../http.md); `_dispatch.Dispatcher.dispatch()` records the failure
and moves to the next provider. A 401/403 additionally calls
`health.mark_auth_failure()` so a revoked key stops burning one request per
dispatch. A key file whose mode drifts to group-readable raises
`PermissionError` from `_check_key_perms()` on the next refresh, which the
dispatcher treats as an ordinary provider failure. `alerts._fetch_detail()`
swallows its own `HTTPError` and returns `""` so one bad detail URL cannot fail
the whole alerts call.

## Security

- Key file must be mode 0600 or 0400 (`_ALLOWED_KEY_MODES`); anything else is
  refused with a message telling the operator to `chmod 600`. The check
  short-circuits on Windows (`os.name == "nt"`), matching `secret_store`.
- The PEM header must be PKCS#8 or SEC1 (`_VALID_KEY_HEADERS`); the error
  deliberately excludes file contents.
- Key residency is minimized: re-read per refresh, local reference deleted in a
  `finally` block, with the rationale (CPython cannot zero an immutable `str`)
  written into `_read_private_key()`'s docstring.
- The JWT claims are `iss=team_id`, `sub=service_id`, `iat`, `exp`, and headers
  `alg=ES256`, `kid=key_id`, `id="{team}.{service}"`. That is the layout Apple's
  WeatherKit REST auth expects [unverified against Apple's current
  documentation - the shape is asserted from the code only].
- No secret ever enters a URL, so `_http`'s `HTTPError` messages (which include
  the base URL, not `params`) cannot leak one.

## Classes

`WeatherKitProvider` - holds credentials and the token cache; four `async get_*`
methods that each build the shared URL, take fresh headers, and delegate to the
matching endpoint module. Extension constraint: any new capability must be an
`async get_<cap>(self, lat, lon, location, **kw)` matching
`_dispatch.CAPABILITY_METHODS`, or the dispatcher will not discover it.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `_check_key_perms(path)` | Refuse non-0600/0400 key files (POSIX only) |
| `_read_private_key(path)` | Perms check, read, PEM-header validate |
| `_make_jwt(team, service, kid, key)` | ES256 sign, 55-minute expiry |
| `WeatherKitProvider._headers()` | Cached bearer token, refresh 60 s early |
| `WeatherKitProvider._url(lat, lon)` | Per-request URL, 4-decimal coordinates |
| `alerts._pick(a, *keys)` | First non-empty string among candidate keys |
| `alerts._fetch_detail(url, headers)` | Best-effort detail body, `""` on failure |
| `_codes.readable(code)` | Condition-code to prose, passthrough on miss |

## Tests

`tests/test_weather_flags.py` pins every alias to `weatherkit` and asserts
`-aw` resolves to WeatherKit rather than AccuWeather
(`test_aw_is_weatherkit_not_accuweather`). `tests/test_dispatcher.py` asserts
the id is a known factory. `tests/run_tests.py` guards the
`[weatherkit]` extra's PyJWT / cryptography floors against
`requirements.txt`. There is no test of `_make_jwt`, `_check_key_perms`, or any
`fetch()` in this package.

## Findings

- **questionable** | `weatherkit/alerts.py - fetch()` - `event` is taken from
  `eventEndDateName` first, a field the module's own docstring does not list
  among the summary's fields and whose name reads as a date label rather than
  an event name; `name` or `description` look like the intended primaries.
- **questionable** | `weatherkit/alerts.py - _fetch_detail()` - sends the Apple
  bearer JWT to `detailsUrl`, a URL taken from the response body, with no host
  allowlist. TLS and Apple's control of the payload make this low risk today,
  but the credential is presented to whatever host the response names.
- **questionable** | `weatherkit/__init__.py - WeatherKitProvider._headers()` -
  synchronous key read plus ES256 signing run on the event loop inside an
  `async` call path; small (about once an hour) but it is blocking work in a
  coroutine.
- **questionable** | `weatherkit/hourly.py - fetch()` - hour labels are
  formatted from the UTC `forecastStart` with no conversion to the location's
  zone, so `%I %p` labels are UTC while the query is for an arbitrary point.
- **test-gap** | `weatherkit/*` - no test constructs `WeatherKitProvider`, mints
  a JWT, or exercises the permission and PEM-header guards; the only coverage is
  flag aliasing and dependency pinning.
