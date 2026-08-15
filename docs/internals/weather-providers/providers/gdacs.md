# gdacs - GDACS global disaster alerts filtered to a 1000 km radius

## Purpose

Serves the `alerts` capability from the Global Disaster Alert and Coordination
System, a joint UN / European Commission service. Keyless and worldwide, but
ranked 10 of 12 for `alerts` in `_dispatch.py - DEFAULT_RELIABILITY`, behind
NWS and the commercial providers - it is the international backstop for
`.alerts`, not the primary source.

Package layout: `weather_providers/gdacs/__init__.py` (provider class) and
`weather_providers/gdacs/alerts.py` (fetch, filter, map).

## What it actually returns

**Disaster events, not meteorological warnings.** The hazard vocabulary is
GDACS' six two-letter codes, expanded by the local `_EVENT_TYPES` table:

| Code | Rendered event |
| --- | --- |
| `EQ` / `TC` | Earthquake / Tropical Cyclone |
| `FL` / `DR` | Flood / Drought |
| `VO` / `WF` | Volcano / Wildfire |

An unmapped code falls back to the event `name`, then the raw code, then the
literal `"Disaster"`. So a `.alerts` answer served by GDACS can be an earthquake
or a volcano - it is not the same class of information as an NWS watch or
warning, even though both arrive as `AlertsResult` / `AlertEntry`
(`../base.md`) and print through the same `modules/weather.py -
_format_alerts()`.

## Registration and credentials

- Registered id `gdacs` via `_reg("gdacs", _f_gdacs)`; `_f_gdacs(cfg)` ignores
  `cfg` and always returns a provider.
- `GdacsProvider.requires_key = False`. Keyless: no secret name, no
  `INTERNETS_*` override, no `config.ini` entry.
- User-facing flag `-gdacs`, asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_alerts(lat, lon, location, **kw)` - the same capability the
mainstream providers implement, not a hazard-specific method. Discovery
asserted in `tests/test_new_weather_capabilities.py -
TestCapabilityDiscovery`.

## Endpoint and request shape

```text
GET https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH
Header: Accept: application/json
```

No query parameters at all. The endpoint returns the **entire current global
event list** as a GeoJSON `FeatureCollection`; all location filtering happens
client-side. One request through `_http.get_json()` at the default 1 MiB cap
(`../http.md`) - the cap is not lifted here, unlike `noaa_coops` (8 MB) and
`swpc` (2 MB), both of which also pull whole-world datasets.

## Filtering and mapping

`fetch(lat, lon, location)`:

1. Reject a payload whose `features` is not a list with
   `HTTPError("GDACS: unexpected response shape", provider_hint="gdacs")`.
2. Per feature, read `geometry.coordinates` as GeoJSON `[lon, lat]` - the
   reversed order relative to the function's own `lat, lon` arguments, handled
   by `elon, elat = float(coords[0]), float(coords[1])`. Malformed or short
   coordinate arrays are skipped.
3. `base.haversine_km()` from the caller to the event, dropping anything beyond
   `_RADIUS_KM = 1000.0`. That is an epicentre-to-caller distance, which is the
   right shape for a point hazard like an earthquake and a poor proxy for an
   area hazard like a drought or a cyclone track.
4. Build `AlertEntry` from `properties`: `eventtype` to `event` via the table,
   `alertlevel` to `severity`, `fromdate`/`todate` to `start`/`end`,
   `htmldescription` (falling back to `description`) to `description`, and
   `name or desc or event` to `headline`.
5. Sort by distance ascending, truncate to `_MAX_ALERTS = 8`, and return.

**An empty list is valid data**, so a location with no nearby events yields an
`AlertsResult` with `alerts=[]` and `_format_alerts()` prints
`No active alerts.` The only raise is the shape check in step 1 - which makes
the package docstring's "never raises" claim inaccurate.

`tests/test_new_weather_capabilities.py - TestGDACS.test_distance_filter` pins
the radius filter and the `EQ` to `Earthquake` mapping;
`test_antipodal_no_math_domain_error` is a regression guard that a near-antipodal
event does not raise a math domain error mid-iteration, which is why
`base.haversine_km()` clamps `sqrt(a)` to 1.0.

## Severity vocabulary mismatch

GDACS `alertlevel` values are `Green` / `Orange` / `Red`. `AlertEntry.severity`
is documented in `../base.md` as the CAP vocabulary
`extreme / severe / moderate / minor / unknown`, and
`modules/weather.py - _format_alerts()` ranks by exactly that set:

```python
_RANK = {"extreme": 0, "severe": 1, "moderate": 2, "minor": 3, "unknown": 4}
ordered = sorted(distinct, key=lambda a: _RANK.get((a.severity or "").lower(), 4))
```

Every GDACS level misses the table and scores 4. Because the sort is stable,
this is benign in practice - all entries tie and the nearest-first order from
`fetch()` survives - but the severity ranking that exists to float the important
alert to the top is inert for this provider, and the rendered line reads
`[ORANGE]` rather than a CAP severity.

## Coverage, latency, cadence

Worldwide. Freshness is whatever the SEARCH list currently holds; the module
caches nothing and pulls the full list per call. `fromdate`/`todate` are
carried through as opaque strings and are not parsed, so an expired event is
filtered only by GDACS dropping it from the list, never by this code.

## Failure behavior

One local raise (unexpected shape) plus anything `_http` raises, all with
`provider_hint="gdacs"` so `../dispatch.md` attributes it. A parsed-but-empty
result terminates the dispatch chain successfully, so a GDACS "no alerts" answer
prevents lower-ranked providers (`eccc`, `metno`) from being tried.

## Concurrency, state, lifecycle

Stateless; no instance fields. Imported lazily in `_f_gdacs()` during
`configure()` (`../init.md`). One awaited request per call, then a linear pass
over the global feature list in the event loop.

## Security

No credential, HTTPS literal URL, no interpolation - there is no injection
surface on the request side. The output side carries the most untrusted text of
any provider in this group: `name`, `htmldescription`, and `description` are
upstream free text. `htmldescription` is raw HTML, and when `name` is empty it
becomes the `headline` that `_format_alerts()` prints. `modules/weather.py -
_sanitize()` strips C0/DEL control bytes and truncates to 200 chars, so IRC
control-code injection is blocked, but HTML markup passes through verbatim into
the channel line.

## Findings

- **Questionable | `gdacs/alerts.py - fetch()`** - `htmldescription` is taken
  before the plain `description` and can become the printed `headline` when
  `name` is empty, putting raw HTML on an IRC line. `_sanitize()` removes
  control characters, not markup.
- **Questionable | `gdacs/alerts.py - fetch()`** - `alertlevel` populates a
  field whose documented domain is the CAP severity vocabulary, so
  `_format_alerts()`'s severity ranking silently degrades to a no-op for every
  GDACS result.
- **Questionable | `gdacs/alerts.py - fetch()`** - the whole-world event list is
  fetched at the default 1 MiB cap with no `max_bytes` override, while smaller
  global datasets elsewhere in the package raise theirs. Growth in the upstream
  list would surface as a `ResponseTooLargeError`, not as truncated data, but
  the failure would be sudden.
- **Doc-drift | `gdacs/__init__.py`** - the package docstring says GDACS
  "returns an empty AlertsResult, never raises"; `alerts.fetch()` raises on an
  unexpected response shape and propagates every `_http` failure.
- **Questionable | `gdacs/alerts.py`** - `_RADIUS_KM = 1000.0` is applied to the
  event's single representative point, so a distant epicentre is dropped even
  when the affected area reaches the caller, and a nearby drought centroid is
  reported for a region that may not include the caller at all.
