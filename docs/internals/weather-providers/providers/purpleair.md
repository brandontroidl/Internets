# purpleair - crowdsourced community PM2.5 sensors, EPA-corrected

## Purpose

Reads the nearest outdoor PurpleAir community sensor, applies the EPA humidity
correction, and converts the corrected PM2.5 concentration to a US EPA AQI.
Rank 11 - last - in the `air_quality` chain (`_dispatch.py -
DEFAULT_RELIABILITY`), deliberately below every model-based and
regulatory-monitor provider.

## Responsibilities / boundaries

Belongs here: the bounding-box sensor query, nearest-sensor selection, the EPA
correction and PM2.5-to-AQI math (`_codes.py`), and normalization into
`AirQualityResult`. Not here: geocoding, category text (`base.py -
aqi_category()`), great-circle distance (`base.py - haversine_km()`),
formatting, or provider ordering (see [../dispatch.md](../dispatch.md),
[../base.md](../base.md)).

Single-capability specialist: implements `get_air_quality` **only**.
`tests/test_airnow_purpleair.py - TestCapabilityDiscovery.test_purpleair_exposes_only_air_quality`
asserts the discovered set is exactly `{"air_quality"}`.

## Dependencies and dependents

Internal: `_http.py - get_json()`, `HTTPError`; `base.py - AirQualityResult`,
`aqi_category()`, `haversine_km()`; `purpleair/_codes.py - pm25_to_aqi()`,
`epa_correct()`. External: `https://api.purpleair.com/v1/sensors`.
Dependents: `__init__.py - _f_purpleair()`, `_dispatch.py`,
`modules/weather.py`, and - notably - `openaq/air_quality.py`, which imports
`purpleair._codes.pm25_to_aqi` across package boundaries.

## Lifecycle

Registered as id `purpleair` by `_reg("purpleair", _f_purpleair)`. The factory
resolves `_cred(cfg, "purpleair_key", "purpleair_key")`:
`secret_store.get("purpleair_key")` (env `INTERNETS_PURPLEAIR_KEY`, then
`config.ini` `[secrets] purpleair_key` at mode 0600), falling back to the legacy
`[weather_providers] purpleair_key`. The credential must be a **READ** key -
PurpleAir issues read and write keys separately. No key logs
`purpleair: skipped (no purpleair_key)` and returns `None`, leaving the
`-purpleair` / `-pa` flags inactive.

## State

`PurpleAirProvider._key`. `fetch()` builds a per-call `idx` field-name index and
tracks `best` / `best_km` locally. `_codes.py` holds only the frozen
`_PM25_BREAKPOINTS` tuple. Nothing persists between calls.

## Concurrency

One `await get_json()` per call; the nearest-sensor scan is synchronous CPU work
over a response that is normally a few dozen rows and is bounded by `_http.py`'s
1 MiB cap. No locks, no shared mutable state.

## Failure behavior

Two `HTTPError` raise sites, both `status=None`, `provider_hint="purpleair"`:

- `fields` or `data` empty - "no nearby sensor"
  (`tests/test_airnow_purpleair.py - TestPurpleAirFetch.test_no_sensors_raises_for_fallback`)
- every row lacks latitude, longitude, or `pm2.5` - "no nearby sensor with
  PM2.5"

Individual malformed rows are skipped rather than fatal. Transport, status,
oversize, and JSON errors propagate from `get_json()`. Nothing returns `None`.

## Security

The key is sent as the `X-API-Key` **request header**, so it never appears in a
URL, a log line, or a referrer. All caller-supplied values (`lat`, `lon`,
converted into four bounding-box parameters) go through `get_json(params=...)`,
never string-interpolated into the URL. Response size capped by `_http.py`. No
filesystem or subprocess access.

Trust boundary worth naming explicitly: the response is **community-submitted
data**. Latitude, longitude, PM2.5, and humidity all originate from
unauthenticated hardware owned by strangers. The module treats those numbers as
inputs to arithmetic (`haversine_km`, `epa_correct`, `pm25_to_aqi`), not as
paths, identifiers, or format strings, so the worst case is a wrong reading, not
code execution.

## Classes

### `PurpleAirProvider` (`purpleair/__init__.py`)

`name = "PurpleAir"`, `requires_key = True` (asserted by
`tests/test_airnow_purpleair.py - TestCapabilityDiscovery.test_both_require_a_key`).
Constructor stores the READ key as `_key`. Sole method
`get_air_quality(lat, lon, location, **kw)` delegates to `air_quality.fetch()`.

## Functions and methods

### `air_quality.fetch(key, lat, lon, location)`

Async; returns `AirQualityResult`, raises `HTTPError`. One GET to
`/v1/sensors` with `fields="latitude,longitude,pm2.5,humidity"`,
`location_type=0` (outdoor only), `max_age=3600` (seen within the last hour),
and a bounding box of `+/- _BOX` (0.1 degrees) around the point expressed as
`nwlng`/`nwlat`/`selng`/`selat`.

PurpleAir returns a column-oriented payload: `fields` is a name list and `data`
is a list of positional rows. The function builds `idx = {name: position}` and
reads through the closure `_get(row, name)`, which bounds-checks the index - so
a response whose `fields` list and row width disagree yields `None` for the
missing column rather than an `IndexError`.

Selection is a linear scan keeping the minimum `haversine_km` among rows that
carry all three of latitude, longitude, and PM2.5.
`tests/test_airnow_purpleair.py - TestPurpleAirFetch.test_picks_nearest_sensor_and_corrects`
pins it: given a 1 km sensor at 8.0 ug/m3 / 50 %RH and a 15 km sensor at
30.0 ug/m3, the result is the near sensor's corrected 5.6 ug/m3 and AQI 31.

### `_codes.pm25_to_aqi(conc)`

Pure. Converts a PM2.5 concentration to a US EPA AQI. Truncates to 0.1 ug/m3
first (`int(conc * 10) / 10.0`, the EPA convention), then applies the
piecewise-linear interpolation over `_PM25_BREAKPOINTS`. Returns `None` for
`None` or negative input, and `500` above the top breakpoint.

### `_codes.epa_correct(pa_pm25, humidity)`

Pure. Applies `PM2.5 = 0.524 * PA - 0.0862 * RH + 5.75`, clamped at 0. Returns
the raw value unchanged when humidity is missing and `None` when the reading is
`None`. All four behaviors are pinned by
`tests/test_airnow_purpleair.py - TestEpaCorrect`.

## AQI scale semantics

The AQI here is a **US EPA PM2.5 sub-index** computed locally - PurpleAir
publishes no index of its own. The breakpoints in `_codes.py` are the **EPA 2024
revision** (effective 2024-05-06): AQI 50 moved from 12.0 to 9.0 ug/m3, and the
200/300/500 boundaries dropped to 125.4/225.4/325.4. The module docstring marks
them frozen on purpose - a regulatory standard, not a tunable.
`tests/test_airnow_purpleair.py - TestPm25ToAqi` anchors all six boundaries,
the 500 cap, the `None`/negative contract, monotonicity, and specifically that
12.0 ug/m3 is now Moderate rather than Good.

Only `pm25` (the corrected value) and `aqi` are populated; `pm10`, `o3`, `no2`,
`so2`, and `co` stay `None`, because a PurpleAir sensor is a particle counter
and measures nothing else. The source label carries the sensor distance,
e.g. `PurpleAir ~3km`, so users can judge how local the reading is; the comment
notes this fits the 30-character source cap that
`modules/weather.py - _sanitize()` enforces.

## Sensor-network caveats (accuracy and QC)

These are properties of the data source, not defects in the code, and they are
the reason for the rank-11 placement:

- **Low-cost optical sensors, not regulatory monitors.** Plantower laser
  counters infer mass from scattering; they read high, especially at elevated
  humidity, which is exactly what `epa_correct()` compensates for.
- **Single sensor, no averaging.** The nearest qualifying sensor drives the
  whole reading. One miscalibrated, obstructed, or indoor-but-mislabelled unit
  produces a confidently wrong answer with no outlier rejection and no
  cross-check against neighbours.
- **No channel-agreement check.** Each PurpleAir unit has two independent laser
  channels (A and B); large A/B divergence is the standard signal of a fouled or
  failing sensor, and the API exposes `pm2.5_a`, `pm2.5_b`, and `confidence` for
  exactly this purpose. The module requests none of them, so the only quality
  filters applied are `location_type=0` and `max_age=3600`.
- **Siting is uncontrolled.** Sensors sit wherever an owner put them - beside a
  barbecue, inside a garage marked outdoor, next to a road. Placement metadata
  is not validated.
- **Distance is bounded only by the box.** `_BOX = 0.1` degrees is about 11 km
  north-south but `11 * cos(latitude)` km east-west, so the search area shrinks
  toward the poles; a corner sensor can sit roughly 15 km away at the equator
  and still be selected. The distance is disclosed in the source label but never
  rejected.

## Coverage and quota

Global wherever owners have deployed sensors - dense in North America, Europe,
and parts of Asia, sparse elsewhere. `__init__.py - _DEFAULT_QUOTA_LIMITS`
records `"purpleair": None` with the comment that PurpleAir bills a points
budget rather than a fixed daily call cap, so `quota_status("purpleair")`
reports `limit=None` and tracks only the raw call count.

## Implementation walk

1. Bounding-box parameter construction and the GET with the READ-key header -
   external I/O, credential injection.
2. `fields` / `data` presence check and raise - fail-closed on a sensor-free
   area.
3. `idx` construction plus the bounds-checked `_get` closure - defensive
   protocol processing for a column-oriented payload.
4. Nearest-sensor scan with `haversine_km`, skipping incomplete rows - selection
   logic.
5. Raise when no row qualified - error handling.
6. `epa_correct()` then `pm25_to_aqi()` on the corrected value - business logic.
7. Distance-bearing source label and dataclass construction - formatting.

## Findings

- defect | `air_quality.py - _FIELDS` with `_codes.epa_correct()` | The module
  requests the generic `pm2.5` field, which the PurpleAir v1 API returns as the
  **ATM** variant for outdoor sensors (`location_type=0`, what this module
  queries). The EPA / Barkjohn US-wide correction the coefficients come from is
  defined against **`pm2.5_cf_1`**, not ATM. ATM and CF=1 agree at low
  concentrations and diverge as concentration rises, so the correction is
  applied to the wrong input exactly when it matters most - smoke episodes.
  Fix is one string: request `pm2.5_cf_1` in `_FIELDS` and read that column.
  (Sources: [EPA correction equation, Barkjohn et al.](https://amt.copernicus.org/articles/14/4617/2021/);
  [PurpleAir API field default, ATM for outdoor sensors](https://community.purpleair.com/t/contradictory-cf-information-in-api-docs/9827).)
- questionable | `_codes.epa_correct()` | The single linear form is applied at
  every concentration. EPA scopes that equation to uncorrected readings below
  roughly 570 ug/m3 and publishes a piecewise extension for extreme smoke, where
  the sensor response goes nonlinear
  ([Barkjohn et al. 2022 wildfire-smoke correction](https://www.mdpi.com/1424-8220/22/24/9669)).
  Practical impact is capped by `pm25_to_aqi()` returning 500 above 325.4 ug/m3,
  but the reported concentration itself stays biased.
- questionable | `air_quality.fetch()` | No maximum-distance rejection. The only
  bound on how far away the reading came from is `_BOX`, whose east-west span
  shrinks with `cos(latitude)`; the distance is disclosed in the source label
  but a 15 km "local" reading is still returned as if it described the queried
  point.
- questionable | `air_quality.fetch()` | No sensor quality control beyond
  `location_type` and `max_age`. `confidence` and the A/B channel pair are
  available in the same API call and would let a single bad unit be rejected;
  neither is requested.
- questionable | `openaq/air_quality.py` importing
  `..purpleair._codes.pm25_to_aqi` | A regulatory conversion shared by two
  providers lives in one provider's private `_codes` module. It belongs beside
  `aqi_category()` in `base.py`. Recorded in both provider docs.
- test-gap | `air_quality.fetch()` | No test covers the `_get` bounds-check
  (a row shorter than `fields`), the `max_age` / `location_type` parameters, or
  a payload where every row is missing `pm2.5`.
