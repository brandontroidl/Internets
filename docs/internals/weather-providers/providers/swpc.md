# swpc - NOAA SWPC geomagnetic Kp and OVATION aurora probability

## Purpose

Serves the `space_weather` capability from NOAA's Space Weather Prediction
Center. It is the **only** provider for that capability
(`_dispatch.py - DEFAULT_RELIABILITY["space_weather"] == {"swpc": 1}`), so if it
fails there is no fallback and `.space` reports
`space-weather data unavailable right now.`

Package layout: `weather_providers/swpc/__init__.py` (provider class) and
`weather_providers/swpc/space_weather.py` (fetch and parse).

## Responsibilities / boundaries

| In scope | Out of scope |
| --- | --- |
| Latest planetary K index | Solar wind, X-ray flux, proton events |
| Aurora probability at the caller's grid cell | Forecast Kp, 3-day outlook |
| Partial-result assembly when one product fails | Any location filtering (global grid) |

## Registration and credentials

- Registered id `swpc` via `_reg("swpc", _f_swpc)` in
  `weather_providers/__init__.py`. `_f_swpc(cfg)` ignores `cfg` entirely and
  always returns a provider.
- `SWPCProvider.requires_key = False`. Keyless government source: no secret
  name, no `INTERNETS_*` override, no `config.ini` entry.
- User-facing flag `-swpc` (`modules/weather.py` `_PROVIDER_FLAGS`), asserted in
  `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_space_weather(lat, lon, location, **kw)`. Note the method name
is `get_space_weather`, mapped from capability `space_weather` by
`_dispatch.CAPABILITY_METHODS`. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoints and request shape

Two static JSON products on `services.swpc.noaa.gov`, both plain GETs with no
query parameters and no headers:

```text
GET /json/planetary_k_index_1m.json
GET /json/ovation_aurora_latest.json     (max_bytes=2_000_000)
```

The aurora product lifts the `_http` default 1 MiB cap to 2 MB because the
OVATION nowcast is a 1-degree global grid - roughly 65000 `[lon, lat, pct]`
triples. Both calls go through `_http.get_json()` (`../http.md`).

## What it actually returns

Not a forecast product. `SpaceWeatherResult` (`../base.md`) carries three
fields, all filled here:

- `kp_index` - a single float, the most recent planetary K index sample.
- `kp_category` - the NOAA storm label from `base.kp_category()`
  (`Quiet` below 5, then `Minor storm (G1)` through `Extreme storm (G5)`).
- `aurora_pct` - the OVATION percentage at the grid cell nearest the caller.
  This is an aurora-visibility probability for that cell, not a Kp-derived
  estimate and not a viewing forecast.

## Parsing

`_latest_kp(data)` takes `data[-1]` of the array - it assumes the feed is
chronological and ends with the newest sample; nothing sorts by `time_tag`.
It prefers `estimated_kp` over `kp_index` (the 1-minute product publishes an
estimated value; the nominal field can lag), coerces to `float`, and returns
`None` on any shape or conversion failure. Preference order is pinned by
`tests/test_new_weather_capabilities.py -
TestSWPC.test_kp_and_nearest_aurora`, whose fixture has `kp_index: 2.0` and
`estimated_kp: 3.33` and expects 3.33.

`_aurora_pct(data, lat, lon)` maps the caller's position onto the grid:

```python
target_lon = round(lon) % 360   # grid longitude is 0..359
target_lat = round(lat)         # grid latitude is -90..90
dist = (g_lon - target_lon) ** 2 + (g_lat - target_lat) ** 2
```

Squared planar distance on integer degrees, minimum wins. Because the OVATION
grid is complete at 1-degree spacing, the exact cell is normally present and the
distance is zero, which is why the planar metric is adequate; it does not wrap
across the 0/359 longitude seam, so it would misbehave on a sparse grid. The
same test pins the mapping: longitude -80 becomes 280, and the fixture cell
`[280, 40, 42]` yields 42.

## Failure behavior and partial results

`fetch()` is written so one dead product does not lose the other:

1. Kp is attempted first; an `HTTPError` sets `kp_failed = True` and leaves
   `kp = None`.
2. Aurora is attempted next. If it raises **and** Kp already failed, a fresh
   `HTTPError("NOAA SWPC: space-weather data unavailable",
   provider_hint="swpc")` propagates so the dispatcher records the failure.
   If Kp succeeded, the aurora failure is absorbed and `aurora = None`.
3. Either field may therefore be `None` in the returned result.
   `modules/weather.py - _format_space()` omits a `None` field and prints
   `Space weather data unavailable` if both are missing - a case reachable only
   when both products returned 200 with unparseable bodies, since the
   both-failed HTTP path raises instead.

## Coverage, latency, cadence

Global: the OVATION grid spans the whole planet and Kp is a planetary index, so
`lat`/`lon` only select an aurora cell. The Kp product is sampled at 1-minute
resolution and the aurora file is the "latest" nowcast, both republished
continuously by SWPC; neither is cached locally, so every `.space` call
re-downloads the full aurora grid.

## Concurrency, state, lifecycle

Stateless; no instance fields. Imported lazily in `_f_swpc()` at `configure()`
time (`../init.md`). The two fetches are sequential `await`s, not gathered, so a
call costs two round trips and the aurora download dominates the latency.

## Security

No credential. Both URLs are HTTPS literals with no interpolation of any kind,
so there is no injection surface. The only values crossing into IRC are the two
numbers and the locally-computed category string, so `_format_space()` handles
no untrusted free text beyond `r.source`.

## Tests

`tests/test_new_weather_capabilities.py - TestSWPC` covers Kp field preference,
the category mapping, and nearest-cell aurora selection with a stub that
dispatches on URL substring. `TestCapabilityDiscovery` pins the capability set
to `{"space_weather"}`, and `test_get_methods_accept_kwargs` guards the
`**kwargs` contract - the file's own comment records that `swpc` was one of two
providers that shipped without it.

## Findings

- **Questionable | `swpc/space_weather.py - _latest_kp()`** - takes `data[-1]`
  without checking `time_tag`, so a feed that is ever republished out of order
  yields a stale or wrong "latest" Kp with no signal.
- **Questionable | `swpc/space_weather.py - fetch()`** - the ~2 MB aurora grid
  is downloaded per request with no cache, while OVATION refreshes on a fixed
  cadence. Consecutive `.space` calls pay the full transfer each time.
- **Questionable | `swpc/space_weather.py - _aurora_pct()`** - planar squared
  distance does not wrap the longitude seam. Harmless on the current dense grid,
  silently wrong if the product ever ships sparse coordinates.
- **Test-gap | `swpc/space_weather.py - fetch()`** - the partial-failure logic
  (Kp fails but aurora succeeds, and both fail so it raises) is the module's
  most intricate control flow and has no test.
