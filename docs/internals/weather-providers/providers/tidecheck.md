# tidecheck - TideCheck global tide extremes for the nearest station

## Purpose

Serves the `tides` capability from tidecheck.com. Key-gated, global, and the
fallback behind `noaa_coops`: rank 2 for `tides` in
`_dispatch.py - DEFAULT_RELIABILITY`. It is the provider that answers when
CO-OPS raises "no tide coverage" for a non-US coast.

Package layout: `weather_providers/tidecheck/__init__.py` (provider class) and
`weather_providers/tidecheck/tides.py` (fetch and parse).

## Responsibilities / boundaries

| In scope | Out of scope |
| --- | --- |
| Nearest-station resolution from lat/lon | Water temperature (left `None`) |
| First high and first low from the extremes list | Currents, water level observations |
| Tolerating three response shapes for the station | Datum or unit conversion |

## Registration and credentials

- Registered id `tidecheck` via `_reg("tidecheck", _f_tidecheck)` in
  `weather_providers/__init__.py`.
- `_f_tidecheck(cfg)` resolves the credential with
  `_cred(cfg, "tidecheck_key", "tidecheck_key")`. That is:
  1. `secret_store.get("tidecheck_key")`, which itself checks the environment
     variable `INTERNETS_TIDECHECK_KEY` first (`secret_store.ENV_PREFIX`), then
     the 0600 `config.ini[secrets]` store;
  2. falling back to `config.ini[weather_providers] tidecheck_key`
     (present in `config.ini.example`).
- With no key the factory logs `tidecheck: skipped (no tidecheck_key)` and
  returns `None`, so the provider never registers and `tides` degrades to
  `noaa_coops` only.
- `TideCheckProvider.requires_key = True`; the key is stored on the instance as
  `self._key`.
- User-facing flags `-tidecheck` and `-tc` (`modules/weather.py`
  `_PROVIDER_FLAGS`), asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_tides(lat, lon, location, **kw)`. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoints and request shape

```text
GET https://tidecheck.com/api/stations/nearest?lat=<lat>&lng=<lon>
GET https://tidecheck.com/api/station/<id>/tides
Header on both: X-API-Key: <key>
```

Note the parameter is `lng`, not `lon`. Both calls go through
`_http.get_json()` with the default 1 MiB cap and 10 s timeout
(`../http.md`). The key travels in a header, never in the URL or query string,
so it does not reach logs that record request paths.

## Datum, units, timezone (correctness-critical)

The code **states none of these and converts nothing**:

- `height` from the extremes list is assigned straight into
  `TideResult.next_high_m` / `next_low_m`, which `../base.md` documents as
  metres. Nothing in the request selects units and nothing validates them.
- The datum is unspecified. CO-OPS heights are MLLW; these are not, so the two
  providers' numbers are not interchangeable even though they populate the same
  field and print through the same `modules/weather.py - _format_tides()`.
- `time` is stored verbatim as a string. The test fixture in
  `tests/test_new_weather_capabilities.py - TestTideCheck` uses
  `"2026-06-22T18:00:00Z"`, that is UTC with an explicit offset - the opposite
  convention to `noaa_coops`, whose strings are station-local with no offset.
  The formatter prints whichever it gets, unlabelled.

## Parsing into `TideResult`

`tides.fetch(key, lat, lon, location)`:

1. `_station(data)` normalises the nearest-station response, which the docstring
   says may arrive as the station object directly, wrapped under a `station`
   key, or as the first element of a list. It returns `None` for anything else.
2. A missing or empty `id` raises
   `HTTPError("TideCheck: no tide station near this location")`, which is how
   the provider signals "inland, fall through" to the dispatcher.
3. The station name is read from the nearest response, and if blank, re-read
   from the `station` block of the tides response - the docstring notes the API
   populates it in only one of the two places.
4. `extremes` is coerced to a list; a non-list becomes `[]` rather than an error.
5. The loop takes the **first** entry with `type == "high"` (case-folded) and
   the **first** with `type == "low"`, breaking once both are set. Height is
   accepted only if it is already `int`/`float`; a numeric **string** height
   becomes `None`. Ordering correctness rests on the comment's claim that
   extremes are time-ordered - nothing sorts or filters by time.
6. `TideResult` is constructed once from locals. This is deliberate: the
   dataclass is `frozen=True`, and
   `TestTideCheck.test_builds_frozen_result_once` is a regression guard against
   the earlier post-construction attribute assignment that raised
   `FrozenInstanceError`.

`water_temp_c` is never set, so `_format_tides()` omits the water line for this
provider.

## Coverage, latency, cadence

Global coastal stations as published by TideCheck. Predictions are precomputed,
so there is no observation latency. "Far inland" is expressed by the upstream
nearest endpoint returning no station, not by a distance cutoff in this code -
unlike `noaa_coops`, which enforces a local 150 km limit.

## Failure behavior

Two raise sites, both `HTTPError` with `provider_hint="tidecheck"`: no station
id, and any error surfaced by `_http` (network, non-2xx, oversize, decode).
Everything else degrades quietly - an unusable extremes list yields a
`TideResult` with empty times, and `_format_tides()` then prints
`No tide data available`. `tests/test_new_weather_capabilities.py -
TestTideCheck.test_no_station_raises` covers the empty-response path.

## Concurrency, state, lifecycle

Stateless apart from `self._key`. Imported lazily in `_f_tidecheck()` during
`configure()` (`../init.md`). The two requests are sequential and awaited.

## Security

- The key is header-borne and never logged by this module.
- `_TIDES.format(id=sid)` interpolates a remote-supplied station id directly
  into the **URL path** with no validation or quoting. The value comes from
  tidecheck.com's own response, so this is trust in the upstream host rather
  than in user input, but a hostile or compromised response could steer the
  second request to another path on the same host.
- Station names reaching IRC are control-char stripped and truncated by
  `modules/weather.py - _sanitize()`.

## Tests

`tests/test_new_weather_capabilities.py - TestTideCheck` covers the happy path
(including the frozen-dataclass regression) and the no-station raise;
`TestCapabilityDiscovery` and `test_get_methods_accept_kwargs` cover the
contract. `tests/test_weather_flags.py` covers `-tc`.

## Findings

- **Questionable | `tidecheck/tides.py - fetch()`** - `height` is accepted only
  when already numeric, so an API returning `"1.8"` silently yields `None` while
  the time still renders. The parallel `noaa_coops` code coerces with `float()`.
- **Questionable | `tidecheck/tides.py - fetch()`** - "first high, first low"
  trusts an undeclared upstream ordering and never filters out past extremes,
  so a list beginning in the past reports a stale "next" tide (the same class of
  defect as `noaa_coops`'s `date=today`).
- **Questionable | `tidecheck/tides.py`** - no datum or unit is requested or
  recorded, yet the values populate `next_high_m` (metres) alongside CO-OPS
  MLLW heights.
- **Questionable | `tidecheck/tides.py - fetch()`** - `_TIDES.format(id=sid)`
  puts an unvalidated remote value into a URL path segment.
- **Test-gap | `tidecheck/tides.py - _station()`** - only the bare-dict shape is
  tested; the `station`-wrapped and list shapes the helper exists for are not.
