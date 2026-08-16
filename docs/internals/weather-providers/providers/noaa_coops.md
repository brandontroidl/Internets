# noaa_coops - NOAA CO-OPS tide predictions for the nearest US station

## Purpose

Serves the `tides` capability from NOAA's Center for Operational Oceanographic
Products and Services (CO-OPS). Keyless, US-only. It is rank 1 for `tides` in
`_dispatch.py - DEFAULT_RELIABILITY`, ahead of `tidecheck`.

Package layout: `weather_providers/noaa_coops/__init__.py` (provider class) and
`weather_providers/noaa_coops/tides.py` (fetch and parse).

## Responsibilities / boundaries

| In scope | Out of scope |
| --- | --- |
| Nearest tide-prediction station lookup | Currents, water level observations |
| First high and first low of today | Multi-day extremes, harmonic constants |
| Best-effort latest water temperature | Any non-US location (raises so dispatch falls through) |

## Registration and credentials

- Registered id `noaa_coops` via `_reg("noaa_coops", _f_noaa_coops)` in
  `weather_providers/__init__.py`.
- `_f_noaa_coops(cfg)` takes no credential and never returns `None`; the
  provider always registers.
- `NoaaCoopsProvider.requires_key = False`. No secret name, no `INTERNETS_*`
  override. CO-OPS is open.
- User-facing flags `-noaa_coops` and `-coops` (`modules/weather.py`
  `_PROVIDER_FLAGS`), asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_tides(lat, lon, location, **kw)`. Capability discovery in
`_dispatch.py - _RegisteredProvider` finds only `tides`; asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoints and request shape

Two API families on `api.tidesandcurrents.noaa.gov`:

```text
MDAPI  GET /mdapi/prod/webapi/stations.json?type=tidepredictions
DATA   GET /api/prod/datagetter?product=predictions&interval=hilo
                &datum=MLLW&units=metric&time_zone=lst_ldt&format=json
                &station=<id>&date=today
DATA   GET /api/prod/datagetter?product=water_temperature&units=metric
                &time_zone=lst_ldt&format=json&station=<id>&date=latest
```

All three go through `_http.get_json()`. The station list is fetched with
`max_bytes=8_000_000`, lifting the module default of 1 MiB
(`_http._MAX_RESPONSE_BYTES`) because the list carries roughly 3500 stations.

## Datum, units, timezone (correctness-critical)

- **Datum `MLLW`** - Mean Lower Low Water, the US tidal chart datum. Heights are
  therefore height above chart datum, not above a station zero or MSL, and are
  not comparable with `tidecheck` heights whose datum the code never states.
- **Units `metric`** - heights arrive in metres and land in `TideResult.next_high_m`
  and `next_low_m` unconverted; water temperature is degrees Celsius.
- **`time_zone=lst_ldt`** - local standard time with local daylight time applied,
  meaning the station's own clock. CO-OPS returns `"YYYY-MM-DD HH:MM"` with **no
  offset and no zone name**, and `tides.fetch()` stores that string verbatim.
  `modules/weather.py - _format_tides()` prints it as-is, so the rendered line
  carries a local wall-clock time with nothing identifying the zone.

## Parsing into `TideResult`

`tides.fetch()` (see `../base.md` for the dataclass):

1. `GET` the station list, reject a non-dict or empty `stations` with `HTTPError`.
2. Linear scan of every station, `base.haversine_km()` against `lat`/`lng`,
   keeping the minimum. Stations missing coordinates or with unparseable ones
   are skipped.
3. If the nearest is beyond `_MAX_KM = 150.0`, raise
   `HTTPError("NOAA CO-OPS: no tide coverage for this location")` so the
   dispatcher falls through to a global provider. This is how "US-only" is
   enforced - geographically, not by country code.
4. `GET` today's `hilo` predictions for that station id; empty predictions raise.
5. Walk `predictions` in order, taking the **first** `type == "H"` and the
   **first** `type == "L"`, breaking once both are set. Heights go through a
   local `_m()` that rounds to 2 dp and maps unparseable values to `None`.
6. `_water_temp(sid)` runs last: a separate `datagetter` call for the latest
   water temperature, rounded to 1 dp. It swallows `HTTPError` and any
   malformed payload and returns `None`, so a missing sensor never fails the
   tide answer.

`station` carries the CO-OPS station name (falling back to the id), which
`_format_tides()` prints as `Station <name>`.

## Coverage, latency, cadence

- Coverage is whatever CO-OPS publishes as `type=tidepredictions` stations
  within 150 km of the point: CONUS coasts, Alaska, Hawaii, Great Lakes, and US
  territories. No proximity-to-navigable-water check beyond distance, so an
  inland point up to 150 km from a coastal station still resolves to that
  station.
- Predictions are harmonic, computed ahead of time, so there is no observation
  latency. Water temperature is the latest sensor reading and is a live
  observation.

## Failure behavior

Every failure path raises `_http.HTTPError` with `provider_hint="noaa_coops"`:
no station list, no usable station, nearest station beyond 150 km, no
predictions for the station. The dispatcher records the failure and continues
down the `tides` chain (`../dispatch.md`). Water temperature failures are
absorbed, not raised.

## Concurrency, state, lifecycle

Stateless. The module holds only URL and threshold constants; the provider
instance holds no fields. Imported lazily inside `_f_noaa_coops()` at
`configure()` time (`../init.md`). All I/O is `await`ed through the shared
`_http` layer (`../http.md`); the three requests are sequential, so a call
costs three round trips.

## Security

No credential to leak. Both hosts are HTTPS literals; the only interpolated
value is `station=<id>`, and that id comes from NOAA's own station list and is
passed as a query parameter, so `_http` handles the encoding. Response strings
(`station` name, prediction timestamps) are attacker-influenceable only by NOAA
and are truncated and control-char stripped at render time by
`modules/weather.py - _sanitize()`.

## Tests

`tests/test_new_weather_capabilities.py` covers capability discovery
(`TestCapabilityDiscovery`) and the `**kwargs` contract
(`test_get_methods_accept_kwargs`). `tests/test_dispatcher.py` asserts the id is
in the registered set. `tests/test_weather_flags.py` covers the `-coops` alias.
There is **no mocked `tides.fetch()` test** - unlike `tidecheck`, which has one.

## Findings

- **Defective | `noaa_coops/tides.py - fetch()`** - `date=today` plus "first H,
  first L" means the fields named `next_high_time` / `next_low_time` hold the
  first extremes of the *calendar day*, which are in the past for most of the
  day. At 18:00 local the reported "next high" can be that morning's.
- **Questionable | `noaa_coops/tides.py - fetch()`** - the ~3500-station list is
  re-downloaded (up to 8 MB, `max_bytes=8_000_000`) on every `.tides` call. It
  is static reference data with no cache.
- **Questionable | `noaa_coops/tides.py - fetch()`** - nearest-by-great-circle
  ignores land. A point 140 km inland resolves to a coastal station and reports
  its tides as the location's.
- **Test-gap | `noaa_coops/tides.py - fetch()`** - no test exercises station
  selection, the 150 km cutoff, the H/L pick, or `_water_temp()` fallback.
