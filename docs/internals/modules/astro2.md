# astro2.py - astronomy and space-weather commands (`.solar` `.neo` `.launches` `.moon` `.sky`)

Five keyless commands in one module: two NOAA/NASA API wrappers, one Launch Library 2
wrapper, and two pure-local commands (moon phase computation, bundled Messier
catalog). Base contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.solar` | - | NOAA SWPC latest GOES X-ray flare class + peak time, plus best-effort sunspot number. |
| `.neo` | - | NASA NeoWs: count of near-earth objects for today (UTC) + the closest approach. |
| `.launches` | `[n]` (1-3, default 1) | Next n launches from The Space Devs LL2: name, provider, NET time, pad. Non-numeric arg replies with usage. |
| `.moon` | `[YYYY-MM-DD]` | Moon phase name, illuminated %, age in days. Pure compute, no network. |
| `.sky` | `<M-number or name>` | Bundled Messier catalog lookup (M1-M110): common name, type, constellation, magnitude. Pure data. |

Handlers: `astro2.py - Astro2Module.cmd_solar/cmd_neo/cmd_launches/cmd_moon/cmd_sky`.
All five check the per-nick rate limit first (`Astro2Module._gate()`); the three
network commands run their `_fetch_*` helper in `asyncio.to_thread`.

## Integration

All HTTP goes through `base.fetch_json` (size-capped streaming; default cap 256 KiB).

| Endpoint | Helper | Timeout | Cap | Auth |
|---|---|---|---|---|
| `services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json` | `_fetch_solar()` | 10 s | default | none |
| `services.swpc.noaa.gov/json/sunspot_report.json` | `_fetch_solar()` (nested, best-effort) | 10 s | default | none |
| `api.nasa.gov/neo/rest/v1/feed` | `_fetch_neo()` | 12 s | default | `api_key` query param (DEMO_KEY default) |
| `ll.thespacedevs.com/2.2.0/launch/upcoming/` | `_fetch_launches()` | 12 s | `_LAUNCH_MAX_BYTES` = 512 KiB | none |

Nothing user-specific is sent: no nick, no channel, no location. The only variable
request data is today's UTC date (`.neo`) and the clamped item count (`.launches`).

## Configuration

`is_configured()` returns True unconditionally - every command works keyless.
`on_load()` reads:

- `weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`, fallback
  `[weather] user_agent`, default `Internets/1.0`) - UA for all requests.
- `nasa_api_key` (env `INTERNETS_NASA_API_KEY`, fallback `[apod] api_key`) -
  optional; defaults to NASA's documented public `DEMO_KEY` (stricter rate limits).
  Shared with the apod module by design (same `cred` lookup chain).

## State

None beyond the two strings cached at `on_load()`. No result caching - every
`.solar`/`.neo`/`.launches` invocation hits the upstream (rate-limited only by the
bot's per-nick limiter).

## Parsing and shaping

- `_fetch_solar()` - the flare endpoint returns a list of recent records; takes the
  last, reads the first present of `max_class`/`current_class`/`flare_class` and
  `max_time`/`time_tag`/`begin_time` (defensive against SWPC schema variants). The
  sunspot fetch is wrapped in its own except block so its failure never suppresses
  the flare line (`tests/test_astro2.py - test_solar_ssn_failure_is_nonfatal`).
- `_fetch_neo()` - walks `near_earth_objects[today]`, scans every
  `close_approach_data.miss_distance.kilometers` (string floats), keeps the minimum.
  Per-object parse failures are skipped, not fatal.
- `_fetch_launches()` - clamps n to 1-3 before the request (verified by
  `test_launches_clamped`), uses LL2 `mode=list`, and tolerates the nested
  `launch_service_provider`/`pad` fields being a dict, a bare string, or absent
  (an observed LL2 list-mode inconsistency, per the inline comment).

All third-party strings are `strip_ctrl`-capped before reaching IRC.

## The moon algorithm (`moon_phase`, `_julian_day`, `_moon`)

Pure mean-cycle computation, no perturbation terms:

1. `_julian_day()` converts the UTC datetime to a Julian Day using the floor-based
   civil-calendar formula (Meeus, Astronomical Algorithms ch. 7 form: the
   `a = y // 100; b = 2 - a + a // 4` Gregorian century correction). Verified
   against the reference epoch: JD(2000-01-06 18:14 UTC) = 2451550.2597, matching
   `_KNOWN_NEW_JD` = 2451550.26. The formula is Gregorian-only (no Julian-calendar
   branch), so dates before 1582 are treated as proleptic Gregorian.
2. Age = `(jd - _KNOWN_NEW_JD) % _SYNODIC` with `_SYNODIC` = 29.53058867 days (the
   mean synodic month).
3. Illuminated fraction = `(1 - cos(2*pi * age/_SYNODIC)) / 2` - the illumination of
   the mean phase angle, 0 at new, 1 at full.
4. Phase name = `int(age/_SYNODIC * 8 + 0.5) % 8` into `_PHASES` - rounds to the
   nearest of 8 buckets, so each named phase is centered on its exact instant.

Accuracy bound: real lunations deviate from the mean cycle because of the eccentric
lunar orbit; the dominant Meeus correction terms sum to roughly +-0.6 days (~14
hours), so the reported phase instant/age can be off by up to about 14 hours versus
the true phase, and the illumination percentage correspondingly near quarters. No
correction terms are applied and none are claimed; this is adequate for a one-line
IRC display, not for almanac use. Spot-checked: 2024-01-11 (true new moon 11:57 UTC)
reports New Moon at 0%, 2024-01-25 (true full moon 17:54 UTC) reports Full Moon at
99%. Tests: `test_moon_known_new_moon`, `test_moon_full_moon`,
`test_moon_command_parses_date`, `test_moon_command_bad_date`.

`_moon()` parses an optional strict `%Y-%m-%d` argument (anything else replies with
usage); no argument uses now (UTC).

## The Messier catalog (`_MESSIER`, `sky_lookup`)

A bundled static table M1-M110: `(common name, type, constellation, magnitude)`,
with an inverted name index (`_MESSIER_BY_NAME`) built at import for common-name
lookups. `_parse_messier_num()` accepts `M31`, `m 31`, or bare `31`.
`sky_lookup()` resolves number first, then name, and formats
`M<num> (<common name>) - <type> in <constellation>, mag <m>`; objects without a
common name get no parenthetical (`test_sky_unnamed_object`). Unknown input replies
"no Messier object matching ...". All output is `strip_ctrl`-capped, including the
echoed query.

## Failure behavior

Each network helper maps failures to a fixed user string and never raises to the
dispatcher: transport/`ResponseTooLarge` -> "... lookup failed"; parse/shape errors
-> "... data unavailable" (all pinned in `tests/test_astro2.py`). `_fetch_launches`
uses a broad `except Exception` for the parse arm (commented: never raise to
caller); the other two enumerate `(ValueError, KeyError, TypeError[, IndexError])`.

## Security notes

- All outbound HTTP via `fetch_json` (bounded body, streamed); no user-controlled
  URLs or parameters - `.launches [n]` is clamped to 1-3, `.sky`/`.moon` never
  touch the network.
- The NASA key travels as a query parameter (`_fetch_neo`), which is how
  api.nasa.gov specifies it; with the default `DEMO_KEY` there is nothing secret in
  transit.
- Every upstream string is control-stripped and length-capped before IRC output.

## Findings

- questionable | `astro2.py - moon_phase()` | The `if age < 0: age += _SYNODIC`
  branch is dead code: Python's `%` with a positive modulus never returns a
  negative value (verified: `(-3.0) % 29.53058867` = 26.53).
- doc-drift | `astro2.py - _julian_day()` | Docstring attributes the algorithm to
  Fliegel-Van Flandern, but the implementation is Meeus's floor-based
  calendar-to-JD formula; Fliegel-Van Flandern is the pure-integer-division variant
  and looks nothing like this code.
