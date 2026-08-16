# currentuvindex - keyless global UV index, current plus today's peak

## Purpose

Serves the current UV index and today's peak from currentuvindex.com. Rank 2 in
the `uv` chain (`_dispatch.py - DEFAULT_RELIABILITY`), behind `openmeteo` - the
`uv` capability has exactly these two providers, so this is the only fallback.

## Responsibilities / boundaries

Belongs here: one GET, the same-day peak selection, and normalization into
`UVResult`. Not here: geocoding, category text (`base.py - uv_category()`),
formatting (`modules/weather.py - _format_uv()`), or provider selection (see
[../dispatch.md](../dispatch.md), [../base.md](../base.md)).

Single-capability specialist: implements `get_uv` **only**.
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery` asserts the
discovered set is exactly `{"uv"}`.

## Dependencies and dependents

Internal: `_http.py - get_json()`, `HTTPError`; `base.py - UVResult`,
`uv_category()`; stdlib `datetime`. External:
`https://currentuvindex.com/api/v1/uvi`.
Dependents: `__init__.py - _f_currentuvindex()`, `_dispatch.py`,
`modules/weather.py - _format_uv()`.

## Lifecycle

Registered as id `currentuvindex` by
`_reg("currentuvindex", _f_currentuvindex)`. **Keyless**: the factory is a bare
`return CurrentUVIndexProvider()` with no `_cred()` call, no config read, and no
`return None` branch, so the provider registers unconditionally on every
install. `requires_key = False`. User-facing flags are `-currentuvindex` and
`-cuv` (`tests/test_weather_flags.py` pins both aliases). This is also the only
provider class in this group with **no constructor at all**.

## State

None. The provider object holds no fields; `uv.fetch()` builds two local lists
(`uvis`, `same_day`) per call and keeps nothing.

## Concurrency

One `await get_json()` per call. The peak scan is synchronous work over a
forecast list bounded by `_http.py`'s 1 MiB response cap. No locks, no shared
mutable state.

## Failure behavior

One `HTTPError` raise site: the envelope is not a dict or `data["ok"]` is falsy
- "currentuvindex: no UV data for this location", `status=None`,
`provider_hint="currentuvindex"`
(`tests/test_new_weather_capabilities.py -
TestCurrentUVIndex.test_not_ok_raises`). Transport, status, oversize, and JSON
errors propagate from `get_json()`. Nothing returns `None`.

`_parse_dt()` swallows `ValueError`/`TypeError` and returns `None`, which
degrades the peak selection to the all-forecast-points fallback rather than
failing the call. Missing `now.uvi` is not an error either: the result is
returned with `uv_index=None` and an empty category.

## Security

No credential is sent - no key, no token, no User-Agent requirement. `lat`/`lon`
are passed as `latitude`/`longitude` query parameters through
`get_json(params=...)`, never interpolated into the URL. Response size capped by
`_http.py`. No filesystem or subprocess access. The attack surface is limited to
whatever a hostile response body can do to the parser, and every field it
touches is either type-guarded (`isinstance` checks on the envelope and the
forecast list, `isinstance(entry, dict)` per entry) or fed to `datetime`
parsing inside a `try`.

Licensing is a real obligation here rather than a footnote: the data is CC-BY
and the package docstring says to credit currentuvindex.com. The
`source="currentuvindex"` field satisfies that, because
`modules/weather.py - _format_uv()` always appends `[{source}]` to the rendered
line. Removing or renaming the source label would break the licence terms, not
just the provenance display.

## Classes

### `CurrentUVIndexProvider` (`currentuvindex/__init__.py`)

`name = "currentuvindex"`, `requires_key = False`. No `__init__`. Sole method
`get_uv(lat, lon, location, **kw)` delegates to `uv.fetch(lat, lon, location)` -
note the key argument that every credentialed sibling passes is simply absent.
`**kw` is required because the dispatcher forwards kwargs
(`tests/test_new_weather_capabilities.py` asserts `VAR_KEYWORD`).

## Functions and methods

### `uv._parse_dt(s)`

Pure. Parses an ISO-8601 timestamp, rewriting a trailing `Z` to `+00:00` because
`datetime.fromisoformat` did not accept `Z` before Python 3.11. Returns `None`
on `ValueError`/`TypeError` rather than raising.

### `uv.fetch(lat, lon, location)`

Async; returns `UVResult`, raises `HTTPError`. GETs `_BASE` with `latitude` and
`longitude`, validates `ok`, then:

1. `uv_index = data["now"]["uvi"]`, `now_dt = _parse_dt(data["now"]["time"])`.
2. Walk `forecast` (only if it is a list), skipping non-dict entries and
   `uvi is None`. Every surviving value goes into `uvis`; values whose parsed
   timestamp falls on the **same calendar date as `now`** also go into
   `same_day`.
3. `candidates = same_day or uvis` - the same-day set when it is non-empty,
   otherwise every forecast point. The comment states the fallback exists for
   the case where the dates do not line up.
4. Append the current reading to `candidates` when present, so a "peak" can
   never come out below the value printed beside it.
5. `uv_max = max(candidates)` or `None` when the list is empty.
6. Return `UVResult(source="currentuvindex", uv_index=..., uv_max=...,
   category=uv_category(uv_index))`.

`tests/test_new_weather_capabilities.py -
TestCurrentUVIndex.test_uv_now_and_peak` pins the discriminating case: a
current 5.2, a same-day forecast 7.1, and a next-day 9.0 produce `uv_max == 7.1`
- the higher tomorrow value is correctly excluded.

## Index scale semantics

The value is the **WHO/WMO Global Solar UV Index** - an open-ended scale
starting at 0, typically topping out near 12 at sea level in the tropics. No
conversion happens here; the upstream number is used verbatim.

The category comes from `base.py - uv_category()` and is derived from the
**current** index only, never from the peak: `Low` below 3, `Moderate` below 6,
`High` below 8, `Very High` below 11, `Extreme` at or above 11
(`tests/test_new_weather_capabilities.py - TestHelpers.test_uv_category` pins
every boundary). `modules/weather.py - _format_uv()` renders
`UV index 5.2 (Moderate) :: Peak today 7.1 [currentuvindex]`, printing the peak
with no category of its own.

## Coverage and quota

Global and keyless. No entry in `__init__.py - _DEFAULT_QUOTA_LIMITS`, so
`quota_status("currentuvindex")` reports `limit=None`; the upstream publishes no
call cap for the free endpoint, and the only rate protection in play is the
bot's own per-nick command rate limit (`modules/weather.py`, via
`self.bot.rate_limited`).

## Implementation walk

1. GET with `latitude`/`longitude` - external I/O, no credential.
2. Envelope and `ok` validation, raise - fail-closed on a coverage gap.
3. Current reading and timestamp extraction via `_parse_dt` - parsing.
4. Forecast list type guard and per-entry loop with `isinstance` and null-`uvi`
   skips - defensive protocol processing.
5. Same-day partitioning by `dt.date() == now_dt.date()` - business logic.
6. `candidates` selection, current-value append, `max()` - peak computation.
7. `UVResult` construction with `uv_category(uv_index)` - normalization.

## Findings

- questionable | `uv.fetch()` | "Today" is whatever calendar date the upstream
  timestamps carry, compared with `dt.date() == now_dt.date()` on
  offset-aware datetimes without normalizing to a common zone. The API's
  timestamps are UTC (the docstring example ends in `Z`), so for a location
  whose local date differs from the UTC date - anywhere west of UTC in the
  evening, or east of UTC in the early morning - "Peak today" is the peak of the
  **UTC** day, not the user's day. The location's timezone is available from
  the geocoder but is neither passed to nor used by this provider.
- questionable | `uv.fetch()` | Neither `uv_index` nor the forecast `uvi` values
  are coerced to `float`; they are stored and compared as whatever JSON
  produced. A string `uvi` reaching `max()` raises `TypeError`, which
  `_dispatch.py` classifies via `_BUG_EXC_TYPES` and logs at ERROR as a provider
  code defect (correct behaviour, but attributed to this module rather than to
  the upstream). A string reaching `UVResult.uv_index` instead surfaces later,
  inside `modules/weather.py - _format_uv()`'s `f"{r.uv_index:.1f}"` - after the
  dispatcher's try/except has already returned, so it cannot fall through to
  `openmeteo`. Every sibling parser in this group coerces explicitly.
- questionable | `uv.fetch()` | A response with `ok: true` but no `now.uvi`
  returns `UVResult(uv_index=None, category="")`. `UVResult` has no
  `is_empty()` (see [../base.md](../base.md)), so `_dispatch.py` records a
  success and stops; the user gets `UV index N/A` even though this is the only
  fallback and `openmeteo` ahead of it in the chain has already been tried.
  The result is at least partly useful when `uv_max` is present, so the case is
  a judgement call rather than a clear defect.
- test-gap | `uv.fetch()` | No test covers the `same_day or uvis` fallback (a
  forecast whose dates never match `now`), an unparseable `now.time`, or a
  missing `now.uvi`. The one peak test exercises only the same-day branch.
