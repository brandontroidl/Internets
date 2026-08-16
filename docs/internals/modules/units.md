# units.py - weather unit conversion and formatting helpers

`modules/units.py` (97 lines) is a small pure-function library that formats weather
quantities as dual-unit IRC strings ("21.0C / 69.8F"). It exists so the weather
command module renders every metric consistently and `None` (a provider gap) always
renders as an explicit "N/A" rather than crashing a format string.

## Responsibilities and boundaries

Presentation only: metric-to-imperial conversion and string formatting. Belongs
here: anything that turns a numeric weather quantity into display text. NOT here:
fetching, provider normalization (that is `weather_providers/`), or the assembly of
whole reply lines (`modules/weather.py`). Note that `weather_providers/base.py`
keeps its own numeric converters (`deg_to_card`, `ms_to_kph`, `km_to_m`) for
normalizing provider data into canonical units; this file formats the canonical
units for display - with one duplication (see Findings).

## Dependencies and dependents

Stdlib only (`datetime`). Sole production consumer: `modules/weather.py`, which
imports `cf, kph, km_mi, mb, aqi_fmt, wave_fmt, swell_fmt`. The remaining three
functions (`deg_to_card`, `fmt_dt`, `fmt_short`) are exercised only by
`tests/run_tests.py`.

## Functions

All are pure, stateless, and total for their input domain: every function accepts
`None` (or, for the datetime pair, a malformed string) and returns an "N/A"-style
fallback instead of raising. Conversion factors are exact where an exact factor
exists (1 mi = 1.609344 km, used consistently by `kph` and `km_mi`).

| Function | Input | Output shape | Fallback |
|---|---|---|---|
| `deg_to_card(deg)` | wind degrees | 16-point cardinal (`"NNE"`), `round(deg/22.5) % 16` into `WIND_DIRS` | `""` on None |
| `cf(c)` | Celsius | `"21.0C / 69.8F"` | `"N/A"` |
| `kph(k)` | km/h | `"10.0km/h / 6.2mph"` | `"N/A"` |
| `km_mi(m)` | meters | `"1.0km / 0.6mi"` | `"N/A"` |
| `mb(v)` | millibars | `"1013mb / 29.92in"` (inHg via /33.864) | `"N/A"` |
| `fmt_dt(iso)` | ISO datetime (`Z` accepted) | `"March 03, 12:00 PM UTC"` | input string, or `"N/A"` if empty |
| `fmt_short(iso)` | ISO datetime | `"Tue 4:00 PM"` (leading zero stripped) | input string, or `"N/A"` if empty |
| `aqi_fmt(aqi, category)` | AQI int + label | `"AQI 42 (Good)"` | `"AQI N/A"` |
| `wave_fmt(height_m, period_s, direction)` | marine | `"Waves 1.2m / 3.9ft 8s from SW"` (optional parts appended) | `"Waves N/A"` |
| `swell_fmt(...)` | marine | same shape with `"Swell"` prefix | `"Swell N/A"` |

Behavioral evidence: `tests/run_tests.py` pins `deg_to_card` cardinal mapping and
None-handling, the `cf`/`kph`/`km_mi`/`mb` dual-unit strings, and the
`fmt_dt`/`fmt_short` bad-input fallbacks (empty string and non-date garbage).

Notes on the two datetime helpers: `iso.replace("Z", "+00:00")` predates reliance
on newer `fromisoformat` Z-handling, keeping the >=3.10 floor honest; the bare
`except Exception` returning the raw input means a malformed upstream timestamp is
displayed verbatim rather than hidden - a deliberate show-something choice, and the
raw string will already have passed through the module's normal output path.

## Lifecycle, state, concurrency, failure, security

Imported by `weather.py` at module load. No state, no I/O, no concurrency concerns
(pure functions are trivially thread-safe for the to_thread weather workers). No
security surface: inputs are provider-derived numbers already bounded by the
weather pipeline; the string outputs are spliced into replies that `weather.py` is
responsible for sanitizing.

## Findings

- questionable | `units.py - deg_to_card()` | Duplicated in
  `weather_providers/base.py - deg_to_card()` (identical algorithm, separately
  maintained table); the entire weather_providers tree uses that copy and nothing in
  production imports this one - two homes for one fact.
- questionable | `units.py - fmt_dt()` / `fmt_short()` | No production callers
  (only `tests/run_tests.py` imports them); either dead formatting variants or
  reserved for a feature that never landed - candidates for removal alongside the
  `deg_to_card` duplicate.
- test-gap | `units.py - aqi_fmt()` / `wave_fmt()` / `swell_fmt()` | The three
  newer formatters have no direct unit tests (the run_tests.py units block covers
  only the older six).
