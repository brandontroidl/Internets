# stormglass - Stormglass.io provider (marine specialist, one endpoint, 3 capabilities)

## Purpose

Wraps Stormglass.io, the top-ranked marine provider (rank 1). All three
capabilities hit the same `weather/point` endpoint with different `params`
lists, and every value arrives as a per-source dictionary that has to be
collapsed to one number. Registered id `stormglass`, factory
`weather_providers/__init__.py - _f_stormglass()`.

## Responsibilities / boundaries

Belongs here: the `weather/point` request shape, source selection over
Stormglass's multi-model values, and normalization into `MarineResult` /
`WeatherResult` / `HourlyResult`. Not here: transport
([_http.py](../http.md)), selection and health
([_dispatch.py](../dispatch.md)), result shapes ([base.py](../base.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `stormglass` (`_reg("stormglass", _f_stormglass)`) |
| Class | `StormglassProvider(api_key)` |
| `name` / `requires_key` | `"Stormglass"` / `True` |
| Quota limit | `10` per day (`_DEFAULT_QUOTA_LIMITS`) |

Credential `stormglass_key` via `__init__.py - _cred()`: `secret_store.get()`
(env `INTERNETS_STORMGLASS_KEY`, then `config.ini [secrets]`), else
`config.ini [weather_providers] stormglass_key`. The 10-per-day free tier is
the tightest quota in the tree, and it is one of the few limits in
`_DEFAULT_QUOTA_LIMITS` that is genuinely daily.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| marine | `get_marine` | 1 |
| hourly | `get_hourly` | 14 (last) |
| current | `get_weather` | unranked, effectively 99 |

`_dispatch.DEFAULT_RELIABILITY["current"]` has no `stormglass` entry, so
`Dispatcher.sort_chain()`'s `reliability.get(pid, 99)` puts it behind every
ranked provider for current conditions. That is confirmed by reading the
`current` table: it lists 14 ids, none of them `stormglass`. Given the 10-call
daily quota this is defensible, but it is an implicit ranking rather than a
declared one (see Findings). Flags: `-stormglass`, `-sg`.

## Endpoint and request shape

```text
https://api.stormglass.io/v2/weather/point?lat=&lng=&params=<comma list>
```

Auth is `Authorization: <raw key>` (no `Bearer` prefix), built per call by
`StormglassProvider._headers()`. Each module declares its own `_PARAMS`:

- `current._PARAMS`: airTemperature, humidity, pressure, windSpeed,
  windDirection, visibility.
- `hourly._PARAMS`: airTemperature, humidity, precipitation, windSpeed,
  windDirection.
- `marine._PARAMS`: waveHeight, wavePeriod, waveDirection, windWaveHeight,
  windWavePeriod, swellHeight, swellPeriod, swellDirection, waterTemperature.

No time range is sent, so the response is Stormglass's default forward window
and `hours[0]` is treated as "now".

## Response parsing

Stormglass returns each variable as `{"sg": x, "noaa": y, ...}`, one entry per
contributing model. `_codes._sg_val()` collapses that: it prefers the
Stormglass blended value `sg`, then `noaa`, `icon`, `meteo`, `meto`, `dwd`, and
finally falls back to the first value in the dict; a plain scalar passes
through and a missing key yields `None`. Every field in all three modules goes
through it.

- `current.fetch()`: `hours[0]`; wind is metres per second
  (`base.ms_to_kph()`), visibility is kilometres and is scaled to metres,
  `description` is the literal `"Current"`.
- `hourly.fetch()`: iterates `hours`, parses each ISO `time` (with `Z` rewritten
  to `+00:00`, assuming UTC when naive), skips entries older than
  `datetime.now(timezone.utc)`, and stops at `hours` entries. This is the only
  module in this group that filters past timestamps correctly against an
  aware UTC now.
- `marine.fetch()`: `hours[0]` into `MarineResult`, with `base.deg_to_card()`
  applied to the wave and swell bearings.

## Lifecycle, state, concurrency

One instance per `configure()`, holding only `_key`. No caches, no disk, no
module state. Each method is a single `await get_json(...)`.

## Failure behavior

An empty `hours` array returns an empty dataclass rather than raising:
`current.fetch()` returns a `WeatherResult` with `temperature=None` (so
`is_empty()` is true and the dispatcher falls through without recording a
success) and `marine.fetch()` returns a bare `MarineResult`. Both carry a
`fix:` comment recording that they previously raised `ValueError`, which made a
no-data location look like an outage. Everything else propagates as
`HTTPError`; 401/403 trips `mark_auth_failure()`, and a 429 (very reachable at
10 calls/day) is tagged by `HTTPError.is_rate_limit` and counted by
`_health.ProviderHealth.record_failure(rate_limited=True)`.

## Security

The key is a header value, never in a URL, so it cannot appear in `HTTPError`
messages. No filesystem access, no user-supplied URLs, response size bounded by
`_http`'s 1 MiB cap.

## Classes

`StormglassProvider` - key holder, `_headers()`, and three `async get_*`
delegators. `_headers()` is rebuilt per call and returns a fresh dict, so
callers cannot mutate shared state. New capabilities must match
`_dispatch.CAPABILITY_METHODS` naming.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `StormglassProvider._headers()` | `Authorization: <key>` (no scheme prefix) |
| `_codes._sg_val(d, key)` | Collapse multi-source dict to one value |
| `current.fetch(headers, lat, lon, loc)` | `hours[0]` to `WeatherResult` |
| `hourly.fetch(..., hours)` | Future hours to `HourlyEntry` list |
| `marine.fetch(...)` | `hours[0]` to `MarineResult` |

## Tests

`tests/test_dispatcher.py - test_stormglass_ranked_for_marine` asserts
`DEFAULT_RELIABILITY["marine"]["stormglass"] == 1`, and
`test_stormglass_registered_as_factory` asserts the factory exists.
`tests/test_weather_flags.py` pins `-sg` / `-stormglass`. No test exercises
`_sg_val()` or any `fetch()`.

## Findings

- **questionable** | `_dispatch.DEFAULT_RELIABILITY["current"]` vs
  `stormglass/__init__.py - StormglassProvider.get_weather()` - the provider
  implements `current` but is absent from the current ranking table, so it
  silently inherits rank 99 and sorts behind everything, including providers the
  ranking comment calls less accurate. Whether that is intended (a 10-call daily
  budget reserved for marine) is not recorded anywhere; the same table ranks
  stormglass explicitly for hourly (14), so the omission reads as an oversight
  rather than a decision.
- **questionable** | `stormglass/current.py - fetch()` - `description` is the
  literal `"Current"`. `description` is in `base._CURRENT_GAP_FIELDS`, so a
  non-empty placeholder blocks `WeatherResult.fill_gaps()` from importing a real
  conditions string from another provider. Same defect as
  `meteomatics/current.py`.
- **questionable** | `stormglass/hourly.py - fetch()` - when the timestamp fails
  to parse, the `except` branch sets `tm = ts` and falls through to append the
  entry, so an unparseable (possibly past) hour bypasses the future-only filter
  and is labelled with a raw ISO string.
- **questionable** | `stormglass/_codes.py - _sg_val()` - the final
  `list(obj.values())[0]` fallback picks an arbitrary model's value in dict
  order with no record of which source supplied it, so two fields in one result
  can come from different models without the output saying so.
- **questionable** | `stormglass/{current,hourly,marine}.py` - three separate
  requests to the same `weather/point` endpoint means a `.weather` plus
  `.marine` pair costs two of the ten daily calls; no response caching exists
  anywhere in the package.
- **test-gap** | `stormglass/*` - the multi-source collapse, the empty-`hours`
  fallbacks, and the past-hour filter are untested.
