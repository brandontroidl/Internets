# nifc - NIFC WFIGS current US wildfire incidents via ArcGIS

## Purpose

Serves the `wildfire` capability from the National Interagency Fire Center's
WFIGS "Incident Locations (Current)" layer, published as a public ArcGIS
FeatureServer. Keyless, US-only, and rank 1 for `wildfire` in
`_dispatch.py - DEFAULT_RELIABILITY` ahead of `firms`.

Package layout: `weather_providers/nifc/__init__.py` (provider class) and
`weather_providers/nifc/wildfire.py` (query and summarise).

## What it actually returns

Dispatch **incident records**, not satellite detections and not a fire
perimeter. The distinction from `firms` drives the whole design: NIFC records
have names and sometimes a size, and most of them are tiny. A metro-area query
routinely returns dozens of sub-acre dispatch stubs. `WildfireResult`
(`../base.md`) is filled as:

| Field | Source |
| --- | --- |
| `fire_count` | `len(features)` returned by the spatial query |
| `nearest_km` / `nearest_name` | closest feature by `haversine_km`, its `IncidentName` |
| `max_acres` | largest non-null `IncidentSize` |
| `sized_count` | how many features carry an `IncidentSize` at all |

`sized_count` exists because a bare count implies more measurement than exists;
`modules/weather.py - _format_wildfire()` renders it as
`46 active fire(s) nearby (1 sized)`.

## Registration and credentials

- Registered id `nifc` via `_reg("nifc", _f_nifc)`; `_f_nifc(cfg)` ignores `cfg`
  and always returns a provider.
- `NIFCProvider.requires_key = False`. Keyless government source: no secret
  name, no `INTERNETS_*` override, no `config.ini` entry.
- User-facing flag `-nifc`, asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_wildfire(lat, lon, location, **kw)`. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoint and request shape

```text
GET https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/
        WFIGS_Incident_Locations_Current/FeatureServer/0/query
    ?where=1=1
    &geometry=<lon>,<lat>&geometryType=esriGeometryPoint&inSR=4326
    &spatialRel=esriSpatialRelIntersects
    &distance=80&units=esriSRUnit_StatuteMile
    &outFields=IncidentName,IncidentSize,IncidentTypeCategory,POOState
    &returnGeometry=true&f=json
```

One request through `_http.get_json()` at the default 1 MiB cap
(`../http.md`). `where=1=1` means "no attribute filter" - the spatial buffer is
the only selection. Note the coordinate order in `geometry` is `lon,lat` (ArcGIS
x,y) while the returned feature geometry is likewise `{"x": lon, "y": lat}`, and
`fetch()` reads it back as `float(gy), float(gx)` to call `haversine_km(lat,
lon, ...)`. `_RADIUS_MI = 80` statute miles is the search radius.

## The acreage field choice (correctness-critical)

The in-code comment and
`tests/test_new_weather_capabilities.py -
TestNIFC.test_max_acres_reads_incident_size_not_discovery_acres` record a live
defect and its fix:

- `DailyAcres` is **not on this layer** - requesting it makes the query 400.
- `DiscoveryAcres` is the size at initial report and sits at a dispatch default
  of `0.01` on nearly every record. Reading it reported the 2690-acre SUMMIT
  fire as "Largest 0 acres".
- `IncidentSize` is the current size, and is `null` on most records.

So the code reads `IncidentSize`, counts how many records have one
(`sized_count`), and leaves `max_acres` at `None` when none do - covered by
`test_max_acres_none_when_no_incident_is_sized`. The formatter also special-cases
sub-acre values so a real 0.4-acre fire does not round to "0 acres".

## Parsing and error surface

ArcGIS answers **HTTP 200 with an `error` object** for a bad query, so a status
check is not enough:

```python
if isinstance(data, dict) and data.get("error"):
    raise HTTPError(f"NIFC: {msg}", status=None, provider_hint="nifc")
```

That is the first check. After it:

- Missing or empty `features` returns an empty `WildfireResult` with
  `fire_count=0` rather than raising - "no active fires within the radius" is
  valid data. Covered by `TestNIFC.test_no_fires_is_empty`.
- `lat`/`lon` are coerced once outside the loop; if coercion fails both become
  `None` and distance computation is skipped for every feature, leaving
  `nearest_km` and `nearest_name` empty while the count still reports.
- Per feature: non-dict entries are skipped, geometry with a missing `x`/`y` is
  skipped for distance, and an unparseable `IncidentSize` is skipped for sizing.
  A per-feature failure degrades that feature only.
- `nearest_name` is only assigned when a new nearest is found, so it always
  refers to the same feature as `nearest_km`.

`TestNIFC.test_nearest_and_count` pins count, nearest name, nearest distance,
and `max_acres` against a two-feature fixture.

## Coverage, latency, cadence

US and US territories - the WFIGS current-incident layer is the interagency
dispatch feed, so coverage stops at the US border and a query elsewhere returns
zero features (reported as "no fires nearby", not as a coverage failure).
Freshness is upstream: the layer is the *current* incident set, updated as
agencies file and revise records, and this module caches nothing.

## Failure behavior

Two paths raise: the ArcGIS `error` object above, and anything `_http` raises
(network, non-2xx, oversize, decode). Both carry `provider_hint="nifc"` so
`../dispatch.md` records the failure and falls through to `firms` if a key is
configured. An empty result is deliberately not a failure, which means the
dispatcher stops at NIFC even when a global provider might have detections
outside its coverage - see Findings.

## Concurrency, state, lifecycle

Stateless; no instance fields. Imported lazily in `_f_nifc()` during
`configure()` (`../init.md`). One awaited request per call.

## Security

No credential. The base URL is an HTTPS literal and every caller-influenced
value travels as a query parameter that `_http` encodes, including the
`geometry` string built from lat/lon. The one piece of upstream free text that
reaches IRC is `IncidentName`, which is `.strip()`ed here and then control-char
stripped and length-capped by `modules/weather.py - _sanitize()` at 40 chars.

## Tests

`tests/test_new_weather_capabilities.py - TestNIFC` is the best-covered parser
in this group: empty result, nearest and count, the `IncidentSize` regression,
and the all-unsized case. Plus `TestCapabilityDiscovery` and
`test_get_methods_accept_kwargs`.

## Findings

- **Questionable | `nifc/wildfire.py - fetch()`** - `outFields` requests
  `IncidentTypeCategory` and `POOState` and then never reads either. Since
  `where=1=1` applies no attribute filter, non-wildfire incident categories
  (for example prescribed fire) are counted as "active fire(s) nearby" while the
  field that could exclude them is fetched and discarded.
- **Questionable | `nifc/wildfire.py - fetch()`** - no `resultRecordCount` is
  set and `exceededTransferLimit` in the response is never checked, so a busy
  region silently truncates at the server's default page size and `fire_count`
  under-reports with no signal.
- **Questionable | `nifc/wildfire.py - fetch()`** - `fire_count = len(features)`
  counts entries the loop then skips (non-dict features), so the count can
  exceed the number of records actually examined.
- **Questionable | `nifc/wildfire.py - fetch()`** - returning an empty result
  outside US coverage means the dispatcher never falls through to `firms`, so a
  non-US query with FIRMS configured reports "no active fires" instead of
  querying the global source. Contrast `noaa_coops`, which raises to force
  fall-through.
