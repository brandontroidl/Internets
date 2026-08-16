# firms - NASA FIRMS satellite thermal-anomaly detections (not incidents)

## Purpose

Serves the `wildfire` capability from NASA FIRMS (Fire Information for Resource
Management System). Global, key-gated, and rank 2 for `wildfire` in
`_dispatch.py - DEFAULT_RELIABILITY` behind `nifc`, so it is what answers
outside the United States.

Package layout: `weather_providers/firms/__init__.py` (provider class) and
`weather_providers/firms/wildfire.py` (fetch and parse). This is the only
provider in this group that does **not** use `_http.get_json()`.

## What it actually returns

**Satellite pixel detections, not mapped fires.** Each CSV row is one VIIRS
thermal anomaly from a single overpass, so the same fire can contribute many
rows and a row can be a flare, a burn pile, or an industrial heat source. The
module docstring states the consequence directly: there are no incident names
and no acreage. `WildfireResult` (`../base.md`) comes back with only:

- `fire_count` - number of detection rows inside the bounding box.
- `nearest_km` - great-circle distance to the closest detection, 1 dp.
- `nearest_name` empty, `max_acres` `None`, `sized_count` 0.

`modules/weather.py - _format_wildfire()` prints the bare count with no
"(N sized)" qualifier for this provider, which is exactly why `sized_count`
exists on the dataclass.

## Registration and credentials

- Registered id `firms` via `_reg("firms", _f_firms)` in
  `weather_providers/__init__.py`.
- `_f_firms(cfg)` resolves `_cred(cfg, "firms_key", "firms_key")`:
  `secret_store.get("firms_key")` (which checks environment variable
  `INTERNETS_FIRMS_KEY` first, then the 0600 `config.ini[secrets]` store), then
  `config.ini[weather_providers] firms_key` (present in `config.ini.example`).
- No key means the factory logs `firms: skipped (no firms_key)` and returns
  `None`, so `wildfire` degrades to `nifc` (US only).
- `FirmsProvider.requires_key = True`; the value is FIRMS' `MAP_KEY`.
- User-facing flag `-firms`, asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_wildfire(lat, lon, location, **kw)`. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoint, request shape, and the CSV transport

```text
GET https://firms.modaps.eosdis.nasa.gov/api/area/csv
        /<MAP_KEY>/VIIRS_SNPP_NRT/<west>,<south>,<east>,<north>/1
```

The area endpoint answers CSV, so the shared JSON helper cannot be used.
`_fetch_csv(url)` is a blocking `urllib.request.urlopen` with a 10 s timeout,
run off the event loop through `asyncio.to_thread()`. It reads
`_MAX_BYTES + 1` bytes so an oversize body is detectable rather than silently
truncated, and decodes UTF-8 with `errors="replace"`. `_MAX_BYTES` is a local
1 MB constant chosen to match the `_http` default; it does **not** follow
`_http.set_max_response_bytes()`.

The bounding box is `_BOX = 0.7` degrees on each side of the point. That is a
square in degrees, not a circle in kilometres: about 78 km north-south
everywhere, but the east-west extent shrinks with the cosine of latitude, so the
searched area at high latitude is far narrower than at the equator. `_DAYS = 1`
selects the most recent day of detections, and `_SOURCE = "VIIRS_SNPP_NRT"`
selects the near-real-time VIIRS S-NPP stream (product identity per the module
docstring; the observation latency of that stream is upstream behaviour and is
not asserted by this code).

## Parsing

1. Guard first: if the first line does not contain `latitude`, raise
   `HTTPError("FIRMS: invalid request or MAP_KEY", provider_hint="firms")`. The
   comment records why - an invalid key or malformed request returns
   **plain-text prose with HTTP 200**, and without this check `csv.DictReader`
   would yield zero rows and the bot would confidently report "no fires".
2. `csv.DictReader` over the decoded text, so columns resolve by header name and
   header order does not matter. The documented VIIRS_SNPP_NRT header is
   `latitude, longitude, bright_ti4, scan, track, acq_date, acq_time,
   satellite, instrument, confidence, version, bright_ti5, frp, daynight`.
3. Only `latitude` and `longitude` are read. Rows whose coordinates are absent
   or unparseable are skipped and do not count. `confidence`, `frp` (fire
   radiative power), `acq_date`/`acq_time`, and `daynight` are all discarded.
4. `base.haversine_km()` per row, minimum retained.
5. **Zero detections is data, not an error** - an empty `WildfireResult` is
   returned so `_format_wildfire()` can print
   `No active fires detected nearby.` rather than the dispatcher falling
   through to another provider.

## Failure behavior

`_fetch_csv()` translates urllib exceptions into the repo's `HTTPError`:

| Condition | Result |
| --- | --- |
| `urllib.error.HTTPError` | `HTTPError` with `.status`, `is_rate_limit` set on 429 |
| `URLError`, `TimeoutError`, `OSError` | `HTTPError` with `status=None` |
| body larger than `_MAX_BYTES` | `HTTPError("FIRMS: response too large")` |
| 200 with a non-CSV body | `HTTPError("FIRMS: invalid request or MAP_KEY")` |

All carry `provider_hint="firms"` so `../dispatch.md` records the failure
against the right provider and the 429 case feeds the rate-limit path.

## Concurrency, state, lifecycle

Stateless apart from `self._key`. Imported lazily in `_f_firms()` during
`configure()` (`../init.md`). The single blocking fetch is offloaded with
`asyncio.to_thread()`, so the bot's event loop is not stalled for up to 10 s;
this is the only offload in the specialist provider set.

## Security

- **The MAP_KEY is in the URL path**, which is how the FIRMS API is designed. It
  is therefore visible to anything that records outbound request URLs, unlike
  the header-borne keys used by `tidecheck`.
- The key is interpolated into the path with an f-string and no quoting. The
  value is operator-supplied config rather than user input, but a key containing
  a `/`, `?`, or `..` would restructure the request path.
- The `# nosec B310` annotation on `urlopen` is justified by the scheme being a
  literal `https://` prefix in `_BASE`; no caller-supplied URL reaches it.
- Bypassing `_http` means this path does not inherit that module's session
  reuse, configurable cap, or shared timeout policy (`../http.md`).
- No upstream free text reaches IRC: only the count and a rounded distance.

## Tests

`tests/test_new_weather_capabilities.py` covers capability discovery with a
dummy key (`TestCapabilityDiscovery`) and the `**kwargs` contract
(`test_get_methods_accept_kwargs`). `tests/test_weather_flags.py` covers the
flag. There is **no test of `wildfire.fetch()` itself** - unlike `nifc`, which
has four - so the CSV parse, the plain-text guard, the oversize check, and the
bounding-box maths are unexercised.

## Findings

- **Test-gap | `firms/wildfire.py - fetch()`, `_fetch_csv()`** - no test at all.
  The plain-text-error guard exists precisely because its absence produced a
  silent "0 fires", and that guard has no regression test.
- **Questionable | `firms/wildfire.py - fetch()`** - `fire_count` counts
  detection pixels, so a single large fire under repeated overpasses inflates
  the number the user reads as "active fire(s) nearby".
- **Questionable | `firms/wildfire.py`** - `_BOX` is a fixed degree offset, so
  the east-west search width contracts with latitude; the comment documents only
  the north-south figure.
- **Questionable | `firms/wildfire.py`** - `_MAX_BYTES` duplicates the `_http`
  cap as a local constant and ignores `set_max_response_bytes()`, so a
  deployment that tunes the global cap silently does not affect this path.
- **Questionable | `firms/wildfire.py - fetch()`** - the `confidence` column is
  parsed away, so low-confidence detections count the same as nominal ones.
