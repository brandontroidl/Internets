# base.py - normalized result dataclasses, derived-field formulas, provider protocol

## Purpose

Defines the contract between the 32 provider sub-packages and everything above
them: frozen dataclasses for each capability's result, shared unit/geometry
helpers, the self-consistent derived-field formulas (feels-like, dewpoint), the
gap-fill field policy (`_CURRENT_GAP_FIELDS`), category mappers (AQI, UV, Kp,
pollen), and the `WeatherProvider` protocol the dispatcher discovers
capabilities against.

## Responsibilities / boundaries

Belongs here: data shapes, pure math on those shapes, and the protocol.
Deliberately not here: HTTP, provider selection, health, formatting for IRC
(`modules/weather.py` renders these dataclasses), and any provider-specific
parsing (each sub-package normalizes its upstream JSON into these types).

## Dependencies and dependents

Dependencies: stdlib only (`dataclasses`, `math`, `typing`).

Dependents: every provider sub-package (result construction plus the helpers
`deg_to_card`, `ms_to_kph`, `km_to_m`, `haversine_km`, re-exported through each
provider's `_codes.py`); `_dispatch.py` (duck-typed use of `is_empty` /
`has_gaps` / `fill_gaps` / `derive_missing`); `__init__.py` (re-exports);
`modules/weather.py` (isinstance checks in every `_format_*`, `pollen_cat_5`);
`tests/test_dispatcher.py`, `tests/test_new_weather_capabilities.py`,
`tests/run_tests.py`.

## Lifecycle

Imported once when the package loads. Everything is module-level constants,
pure functions, and dataclass definitions; no initialization, no teardown.

## State

None owned. All dataclasses are `frozen=True, slots=True`, so results are
immutable value objects (`tests/run_tests.py` asserts frozenness for
`WeatherResult` and `ForecastDay`). "Mutation" is expressed as
`dataclasses.replace()` copies in `fill_gaps` / `derive_missing`.

## Concurrency

No shared mutable state; every function is pure. Immutability makes results
safe to pass between the event loop and threads without locks.

## Failure behavior

Pure functions with total domains where it matters: `deg_to_card`, unit
converters, and all category mappers accept `None` and return neutral values
(`""` / `None`). `haversine_km` clamps `sqrt(a)` to 1.0 so near-antipodal
rounding cannot push `asin` out of domain
(`tests/test_new_weather_capabilities.py -
TestGDACS.test_antipodal_no_math_domain_error`). `_magnus_dewpoint` will raise
`ValueError` on `humidity <= 0` (log of non-positive); its only caller,
`derive_missing`, guards on `humidity is not None` but not on zero - a 0%
humidity reading would raise inside the dispatcher's per-provider try/except
and be misclassified as a provider failure (see Findings).

## Security

No I/O, no secrets. Untrusted upstream strings (descriptions, headlines) pass
through unmodified; sanitization is deliberately deferred to the IRC boundary
(`modules/weather.py - _sanitize`), which is the correct single choke point.

## Classes

All result classes share the pattern `frozen=True, slots=True`, a `source`
attribution string and a `location` echo; capability-specific fields default to
`None`/`""`/empty so sparse upstreams still construct.

### `ForecastDay` / `WeatherResult`

`WeatherResult` doubles for the `current` and `forecast` capabilities: current
conditions in the scalar fields, `forecast: list[ForecastDay]` for daily
forecasts. Fields: `source`, `temperature`, `description`, `location`,
`feels_like_c`, `humidity`, `wind_kph`, `wind_dir` (16-point string,
`""` = unknown), `pressure_mb`, `visibility_m`, `dewpoint_c`, `forecast`.

Methods (all used by `_dispatch.Dispatcher.dispatch`):

- `is_empty()` - no temperature AND no forecast days. Providers build with
  `.get()`, so a sparse upstream yields a non-None result with everything
  None; the dispatcher treats empty like a failure-to-produce and falls
  through (`tests/test_dispatcher.py - TestGapFill.test_coreless_result_falls_through`).
- `has_gaps()` - any of `_CURRENT_GAP_FIELDS` missing (`None` or `""` via
  `_missing`). Note `description == ""` alone makes this True
  (`test_has_gaps_true_when_only_description_empty`).
- `fill_gaps(other)` - returns a copy with only the *missing* secondary fields
  taken from `other`, crediting both sources
  (`"NWS + Open-Meteo"`); returns `self` unchanged when `other` adds nothing.
  Temperature and forecast are never touched; a present description is never
  overwritten (`test_present_primary_description_not_overwritten`). The
  `other.source in self.source` substring check prevents re-crediting the same
  provider twice.
- `derive_missing()` - computes `feels_like_c` (via `_apparent_temp`) and
  `dewpoint_c` (via `_magnus_dewpoint`) from this result's *own* temperature,
  humidity and wind when the provider left them None; rounds to 0.1. No-op if
  temperature is missing or both fields are populated
  (`test_derive_missing_computes_from_own_observation`,
  `test_derive_missing_noop_when_already_populated`).

### The gap-fill boundary: `_CURRENT_GAP_FIELDS`

```python
_CURRENT_GAP_FIELDS = (
    "humidity", "wind_kph", "wind_dir",
    "pressure_mb", "visibility_m", "description",
)
```

Only these secondary fields may be imported from a fallback provider.
`feels_like_c` and `dewpoint_c` are deliberately excluded: both are derived
from an observation's own temperature, so borrowing them from a provider that
measured a different temperature produces a self-contradicting line. The long
comment above the tuple records the observed incident (Yosemite: NWS 24.2C
station obs paired with Open-Meteo's 11.9C feels-like computed against its own
13.8C grid temperature). Cross-provider import of derived fields is also
regression-tested (`tests/test_dispatcher.py -
TestGapFill.test_derived_fields_are_never_imported_from_another_observation`),
and missing derived fields alone do not keep the chain walking
(`test_missing_derived_fields_do_not_keep_the_chain_walking`).

### `HourlyEntry` / `HourlyResult`

Per-hour time label, temp, description, precip mm and chance, humidity, wind.
`HourlyResult.is_empty()` - no entries - drives fall-through.

### `AlertEntry` / `AlertsResult`

Event, severity (`extreme/severe/moderate/minor/unknown`), headline, start/end,
description. No `is_empty`: zero alerts is a *valid* answer ("No active
alerts"), so the dispatcher must not fall through on it - the asymmetry with
Hourly/Weather is intentional.

### `AirQualityResult`

US EPA AQI plus pollutant concentrations (pm25, pm10, o3, no2, so2, co) and
`aod` (aerosol optical depth, smoke proxy).

### `AstronomyResult`, `HistoricalResult`, `MarineResult`

Plain field bags; no behavior. `MarineResult` carries wave/swell/wind-wave
triplets plus water temperature.

### `NowcastEntry` / `NowcastResult`

Short-range precipitation steps plus a human `summary`.

### `UVResult`, `PollenResult`, `WildfireResult`, `SpaceWeatherResult`, `TideResult`

Single-capability results. `PollenResult` unifies three upstream data models
(Open-Meteo CAMS per-species grains/m3, Google 0-5 index triplet, Pollen.com
0-12 overall index + `triggers`); the formatter renders whichever group is
populated. `WildfireResult.sized_count` exists because NIFC's incident layer is
mostly unsized dispatch records - the docstring explains why count and
sized-count are reported separately.

### `WeatherProvider` (Protocol, `runtime_checkable`)

The provider contract: attributes `name`, `requires_key`; required coroutines
`get_weather` and `get_forecast`; optional `get_hourly` .. `get_tides`. The
docstring pins the invariant that method names MUST match
`_dispatch.CAPABILITY_METHODS`, because capability discovery is `hasattr`
based. `runtime_checkable` supports the `isinstance` assertions in
`tests/run_tests.py`. Note the protocol makes `get_weather`/`get_forecast`
"required" in prose only - air-quality-only providers (AirNow, PurpleAir)
deliberately omit them and are registered anyway
(`tests/run_tests.py`: "it deliberately does not implement get_weather"); the
dispatcher never requires them, so the protocol docstring overstates the
requirement (see Findings).

## Functions and methods

### Shared helpers

- `deg_to_card(deg)` - degrees to 16-point compass via `round(deg/22.5) % 16`;
  `None` -> `""`.
- `ms_to_kph(v)` / `km_to_m(v)` - None-propagating unit conversions.
- `haversine_km(lat1, lon1, lat2, lon2)` - great-circle distance, 6371 km
  radius, domain-clamped; shared by nearest-station/sensor/event providers.
- `_missing(v)` - `None` or `""`; the single definition of "missing" for
  gap-fill.

### Derived-field formulas

- `_magnus_dewpoint(temp_c, humidity)` - Magnus formula with Alduchov &
  Eskridge 1996 coefficients (a=17.625, b=243.04).
- `_heat_index_c(temp_c, humidity)` - NWS Rothfusz regression computed in
  Fahrenheit with both low-RH and high-RH adjustment terms, converted back to
  Celsius.
- `_wind_chill_c(temp_c, wind_kph)` - Environment Canada / NWS wind chill
  (13.12 + 0.6215 T - 11.37 v^0.16 + 0.3965 T v^0.16).
- `_apparent_temp(temp_c, humidity, wind_kph)` - regime selector: heat index
  when T >= 27C and RH >= 40%; wind chill when T <= 10C and wind > 4.8 kph;
  otherwise the temperature itself. Both regimes are exercised by
  `tests/test_dispatcher.py - TestGapFill.test_derive_missing_heat_index` /
  `test_derive_missing_wind_chill`.

### Category mappers

| function | scale | output |
|---|---|---|
| `aqi_category(aqi)` | US EPA 0-500 | Good .. Hazardous |
| `uv_category(uv)` | WHO UV index | Low .. Extreme |
| `kp_category(kp)` | NOAA Kp 0-9 | Quiet .. Extreme storm (G5) |
| `pollen_cat_12(idx)` | IQVIA 0-12 | Low .. High |
| `pollen_cat_5(idx)` | Google 0-5 | None .. Very High |

`uv_category` and `kp_category` boundaries are pinned by parametrized tests
(`tests/test_new_weather_capabilities.py - TestHelpers`).

## Implementation walk

- Docstring + imports: contract statement (stale type list, see Findings).
- `_DIRS` + `deg_to_card`, unit converters, `haversine_km`: shared formatting
  and geometry helpers.
- `_CURRENT_GAP_FIELDS` + `_missing`: the gap-fill policy and its rationale
  comment (business logic; the comment is the load-bearing part).
- Formula block (`_magnus_dewpoint`, `_heat_index_c`, `_wind_chill_c`,
  `_apparent_temp`): business logic, each formula cited to its source.
- Dataclass blocks, one per capability: data shape definitions;
  `WeatherResult`'s four methods are the only behavior.
- `pollen_cat_12` / `pollen_cat_5`: sit between `PollenResult` and the
  Wildfire block rather than with the other category helpers - cosmetic only.
- `_AQI_THRESHOLDS` + `aqi_category`, `uv_category`, `kp_category`: threshold
  tables.
- `WeatherProvider` protocol: the discovery contract.

## Findings

- questionable | `base.py - WeatherResult.derive_missing()` | Calls
  `_magnus_dewpoint` whenever `humidity is not None`, but `log(humidity/100)`
  raises `ValueError` at `humidity == 0` (a real desert/sensor-glitch value);
  the exception would be caught by the dispatcher and mis-scored as a provider
  failure instead of yielding a result without dewpoint.
- doc-drift | `base.py` module docstring | The "Data types" list ends at
  `MarineResult`; `NowcastResult`, `UVResult`, `PollenResult`,
  `WildfireResult`, `SpaceWeatherResult`, and `TideResult` are missing.
- doc-drift | `base.py - WeatherProvider` docstring | States `get_weather` and
  `get_forecast` are "Required (all providers)", but 17 of the 32 registered
  providers (the air-quality/uv/pollen/wildfire/space/tide/astronomy/alerts
  specialists) implement neither, by design, and nothing enforces the
  requirement.
- test-gap | `base.py - fill_gaps()` source crediting | The
  `other.source in self.source` substring guard (e.g. `"NWS"` vs
  `"NWS + Open-Meteo"`) has no direct test; a provider whose source string is a
  substring of another's (`"Open-Meteo"` in a hypothetical
  `"Open-Meteo Pro"`) would be silently un-credited.
