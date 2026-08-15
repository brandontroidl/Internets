# weather.py - IRC-facing weather commands over the multi-provider aggregation layer

## Purpose

`modules/weather.py` is the IRC half of the weather system. It owns command
registration, flag parsing, location resolution (including the saved-location
fallback and the cross-user privacy check), per-capability provider listing and
validation, and the formatting of every provider result type into IRC lines.
Everything upstream of that - provider selection, fallback chains, health
scoring, HTTP - lives in the `weather_providers/` package and is documented
separately; this module talks to it only through the public `get_*` coroutines
and a small set of dispatcher inspection APIs.

The module subclasses `BotModule` ([base](base.md)) and follows the standard
command contract; only weather-specific behavior is documented here.

## Commands

15 command families, all defined in `weather.py - WeatherModule.COMMANDS`:

| Command | Alias | Capability | Handler | Notes |
|---|---|---|---|---|
| `.weather` | `.w` | `current` | `cmd_weather` | current conditions |
| `.forecast` | `.f` | `forecast` | `cmd_forecast` | 4-day daily forecast (`days=4`) |
| `.hourly` | `.h` | `hourly` | `cmd_hourly` | next 12 hours (`hours=12`) |
| `.alerts` | `.al` | `alerts` | `cmd_alerts` | active alerts; state-wide widening (below) |
| `.aqi` | `.air` | `air_quality` | `cmd_aqi` | AQI + pollutant concentrations |
| `.astro` | `.sun` | `astronomy` | `cmd_astro` | sun/moon ephemeris |
| `.history` | `.hist` | `historical` | `cmd_history` | past date, leading `YYYY-MM-DD` arg |
| `.marine` | `.sea` | `marine` | `cmd_marine` | waves, swell, water temp |
| `.nowcast` | `.nc` | `nowcast` | `cmd_nowcast` | short-range precipitation |
| `.uv` | `.uvi` | `uv` | `cmd_uv` | UV index now + today's peak |
| `.pollen` | `.allergy` | `pollen` | `cmd_pollen` | three per-source render shapes (below) |
| `.wildfire` | `.fire` | `wildfire` | `cmd_wildfire` | nearby fire detections |
| `.space` | `.aurora` | `space_weather` | `cmd_space` | Kp index + aurora chance |
| `.tides` | `.tide` | `tides` | `cmd_tides` | next high/low from nearest station |
| `.providers` | - | - | `cmd_providers` | admin only: health + capability chains |

Usage shape shared by all data commands:

```text
.<cmd> [flags] [location | -n <nick> | (nothing = saved location)]
.w 90210            .f -vc Tokyo          .aqi -an 67127
.hist 2020-03-01 London                   .w -n othernick
```

Reply shape for single-line commands: `:: <display> :: <fields joined by " :: "> :: `
with a trailing `[<source>]` tag. `.alerts` replies multi-line (header + one
line per alert). Errors and usage go back as `{nick}: <message>`.

## Per-provider flag parsing

`weather.py - _parse_weather_flags()` scans the whole argument string token by
token, so flags are positional-anywhere (`-vc Tokyo` and `Tokyo -vc` both
work). Recognized forms:

- `-<alias>` - any key of `weather.py - _PROVIDER_FLAGS`, a ~70-entry alias
  table mapping short and long forms to the canonical dispatcher id
  (`-aw`/`-apple`/`-appleweather`/`-wk` all mean `weatherkit`). Aliases must be
  globally unique; the table's comment pins the one known trap: `-aw` is Apple
  WeatherKit, not AccuWeather (`-acc`), and
  `test_weather_flags.py - TestProviderAliases.test_aw_is_weatherkit_not_accuweather`
  guards it. Matching is case-insensitive (aliases are stored lowercase;
  `TestAliasMapInvariants.test_all_aliases_lowercase`).
- `-p <name>` - backwards-compatible explicit form. The value resolves through
  the alias table; an unknown value passes through literally and is rejected
  later by `_validate_provider()` with a helpful message
  (`TestPFlag.test_p_passes_through_unknown_as_literal`).
- `-l` - list mode: print the ranked provider chain for this command's
  capability and stop.
- `-n <nick>` - NOT consumed here. Both tokens are kept together in `rest` so
  `WeatherModule._resolve()` can match its `^-n\s+(\S+)$` regex
  (`TestNFlag.test_n_kept_together`).
- A leading `-` followed by a digit is not a flag - southern-hemisphere
  coordinates like `-33.8688,151.2093` pass through intact
  (`TestUnknownFlag.test_negative_number_not_treated_as_flag`).
- Any other `-foo` token is an unknown flag: the first one is remembered and
  returned, the token is dropped so it cannot pollute the geocoder query, and
  the command replies with `_warn_unknown_flag()` and stops.

Return contract: `(force_provider | None, list_mode, rest | None, unknown_flag | None)`.
`weather.py - _flag_examples_for()` renders the alias list for one canonical id
(sorted short-to-long) for the `-l` output.

## Location resolution and saved-location fallback

`weather.py - WeatherModule._resolve()` produces the raw location string:

1. `arg` matches `-n <nick>` exactly (entire argument): look up the target's
   saved location via `internets.py - IRCBot.loc_get()` (a passthrough to
   `store.py - Store.loc_get()`). Before that, a privacy gate: if the target is
   a different nick and `Store.is_opted_out(target)` is true, refuse with
   `"{target} has opted out of location sharing."`. The probe is via `getattr`
   so an older `Store` without the helper defaults to allow. Users can always
   look up themselves regardless of their own opt-out. The invoker-side
   commands that WRITE locations live in `modules/location.py`.
2. Non-empty `arg` that is not `-n`: used verbatim as the query.
3. No `arg`: the invoker's own saved location. If none exists the reply
   directs the user to `.regloc` - there is deliberately no operator default
   location, because answering with a default point reads as "your weather"
   and is not (comment at `WeatherModule._resolve()`, behavior proven by
   `run_tests.py - "weather: no saved location prompts for regloc..."`).

`weather.py - WeatherModule._geo()` then applies the per-nick API cooldown
(`IRCBot.rate_limited()`, notice on refusal), calls
`geocode.py - geocode()` with the configured User-Agent and home country, and
on a miss echoes the query back through `_sanitize()` so user-controlled input
cannot inject IRC formatting into bot output. Returns
`(lat, lon, display, cc)` or `None` (error already reported).

### State-wide alert widening

`WeatherModule.cmd_alerts()` is the one handler that does not use the generic
path unchanged: a bare US state query ("mississippi", "MS") asks a state-wide
question that a single geocoded point cannot answer (the centroid is inland
and misses coastal warnings). It re-derives the raw query and calls
`geocode.py - us_state_code()`; on a whole-query state match it passes
`area=<USPS code>` to `get_alerts()` so the NWS provider queries the whole
state. "jackson mississippi" stays a point lookup.
`run_tests.py - "weather .al: a bare state widens to an area query..."` proves
both branches (`seen == ["MS", None]`).

## Integration boundary with weather_providers

The module never touches providers or HTTP directly. Contract surface:

- `weather_providers.get_weather/get_forecast/get_hourly/get_alerts/get_air_quality/get_astronomy/get_historical/get_marine/get_nowcast/get_uv/get_pollen/get_wildfire/get_space_weather/get_tides` -
  async, `(lat, lon, display, **kw)`, return a frozen result dataclass or
  `None` when every provider failed. All accept `force_provider=<id>`, which
  pins the chain to one provider with no fallback
  (`weather_providers/_dispatch.py - Dispatcher.dispatch()`).
- `weather_providers.configure(cfg)` - called once from `on_load()` to build
  the provider registry from config + secret store.
- `weather_providers.dispatcher` - `provider_ids`,
  `_sorted_for_capability()` (back-compat name for `sort_chain()`),
  `health_summary()`, `capability_matrix()` for `-l` and `.providers`.
- `weather_providers.provider_status()` / `provider_capabilities()` - state
  badges and capability validation.
- `weather_providers/_dispatch.py - CAPABILITY_METHODS` - the authoritative
  capability-name set, used to reject unknown capabilities in `-l`.

Result types (`WeatherResult`, `HourlyResult`, `AlertsResult`,
`AirQualityResult`, `AstronomyResult`, `HistoricalResult`, `MarineResult`,
`NowcastResult`, `UVResult`, `PollenResult`, `WildfireResult`,
`SpaceWeatherResult`, `TideResult`) are lazily imported inside each formatter
and isinstance-checked before any field access - a wrong type raises
`TypeError` rather than formatting garbage.

## The single-source rule (deliberate rejected alternative)

One observation per reading. Multi-provider mean/median blending of readings
was proposed and REJECTED (2026-07-22): providers are not independent samples
of one truth (they disagree mostly on WHICH location/elevation/grid-cell they
measured, not on measurement noise), so averaging them manufactures a number
no instrument produced.

The narrower rule inside that: derived fields are never gap-filled across
providers. `feels_like_c` and `dewpoint_c` are functions of an observation's
own temperature/humidity/wind, so importing them from a provider that measured
a different temperature yields a self-contradicting line (observed live:
"Temperature 24.2C :: Feels like 11.3C", NWS station temperature paired with
Open-Meteo's feels-like - see the incident narrative at
`weather_providers/base.py - _CURRENT_GAP_FIELDS`).

What the dispatcher DOES do for `current` only is fill MISSING secondary
fields (`humidity`, `wind_kph`, `wind_dir`, `pressure_mb`, `visibility_m`,
`description` - the `_CURRENT_GAP_FIELDS` tuple) from the next provider in the
chain, crediting both sources in the `[A + B]` tag
(`weather_providers/base.py - WeatherResult.fill_gaps()`). Temperature is
never touched, and `feels_like_c`/`dewpoint_c` are deliberately absent from
the fillable set - after gap-filling,
`WeatherResult.derive_missing()` computes them from the primary observation's
own temperature (Rothfusz heat index / Environment Canada wind chill / Magnus
dewpoint). This module's `_format_current()` carries the display half of the
same decision: feels-like is always shown when known (the old
suppress-when-close rule made "no feels-like" and "matches temperature"
indistinguishable - comment in `_format_current()`, test
`run_tests.py - "weather _format_current: feels-like is shown whenever it is known"`).

## Configuration

`WeatherModule.on_load()`:

- `weather_providers.configure(self.bot.cfg)` - registers whichever providers
  have keys; keyless providers (NWS, Open-Meteo, MET Norway, SWPC, ...) always
  register, so the module works with zero keys.
- `self._ua` via `base.py - cred(cfg, "weather_user_agent", "weather", "user_agent")` -
  secret store first (env override `INTERNETS_WEATHER_USER_AGENT`), then
  `[weather] user_agent`, else `""`. A blank UA disables geocoding (enforced
  inside `geocode()`, which refuses to call Nominatim without a contactable
  UA); `on_load()` logs a warning. `cred()` is used precisely so a fresh
  install with no `[weather] user_agent` key cannot crash the module out of
  `.help` (comment in `on_load()`).
- `self._cooldown` from `[bot] api_cooldown`, floored at 1.
- `self._default_country` from `[weather] default_country` (default `"us"`);
  `geocode()` validates and normalizes it, a bad value falls back to `us`.

`is_configured()` is not overridden: the module is always visible in `.help`.
Keyless degradation is per-feature (geocoding refuses without a UA; provider
chains shrink to the keyless providers).

## State and concurrency

The module owns no persistent state. Per-instance fields are `_ua`,
`_cooldown`, `_default_country`, set once in `on_load()` and read-only
afterwards. Saved locations live in `store.py - Store` (accessed through the
bot's `loc_get` passthrough); provider health/quota state lives in
`weather_providers`. All handlers are coroutines on the bot's event loop;
blocking work happens inside `geocode()` and the provider layer (both offload
via `asyncio.to_thread`). No locks are needed here.

## Failure and degradation behavior

- Unknown flag: warn and stop (no partial execution).
- Forced provider not registered / lacks the capability:
  `_validate_provider()` replies with the active-provider list or the
  unsupported-capability message and stops. A forced provider whose circuit
  breaker is open fails inside the dispatcher with `None` - surfaced as the
  command's generic `fail_msg` (the caller's explicit choice disables
  fallback).
- Rate limited: notice to the invoker, no channel traffic.
- Geocode miss: `"location not found: '<sanitized query>'"`.
- All providers failed / no data: per-command `fail_msg` (e.g. marine hints
  "location may be inland", tides "no station near this location", nowcast
  names the missing capability).
- Formatter type mismatch raises `TypeError`, caught by the bot's command
  wrapper (reported as an internal error), never sent as garbage output.

## Security notes

- Every upstream-derived string spliced into an IRC line passes through
  `weather.py - _sanitize()`, a thin alias over `base.py - strip_ctrl()`
  (C0 + DEL strip, 200-char default cap) kept so there is no second
  control-byte set to drift. Field-specific tighter caps throughout the
  formatters (source 30, wind dir 4, headline 200, ...).
  `run_tests.py - "SEC-WP-004: weather module sanitizes API strings"` covers
  it.
- The echoed not-found query is sanitized (user input, same treatment as
  upstream data).
- Cross-user location reads honor the store opt-out flag (privacy gate in
  `_resolve()`); location writes/deletes are in `modules/location.py`; full
  erasure is `modules/privacy.py - cmd_forgetme()`.
- `.providers` is admin-gated (`IRCBot.is_admin()`).
- Command logging (`log.info` in `_weather_cmd()` and the bespoke handlers)
  records the resolved display name and coordinates but not the invoking
  nick, so the bot log does not accumulate nick-to-location pairs from
  lookups. (`.regloc` logging in location.py does pair them - see that doc.)
- No direct HTTP in this module, so the fetch_json size-cap rule is satisfied
  by construction; HTTP hygiene is the provider layer's and geocode's.

## Functions and methods

### Formatters (module-level, pure, sync)

`run_tests.py - "weather _format_current and _format_forecast are sync (pure functions)"`
pins the sync-purity contract. All take one result object, isinstance-check
it, and return a string (or list of strings for alerts). Number formatting
delegates to `modules/units.py` (`cf`, `kph`, `km_mi`, `mb`, `aqi_fmt`,
`wave_fmt`, `swell_fmt`).

| Function | Result type | Notable behavior |
|---|---|---|
| `_format_current()` | `WeatherResult` | "Calm" under 1 kph wind; feels-like always shown when known; N/A per missing field |
| `_format_forecast()` | `WeatherResult` | empty string when no forecast days (caller turns that into `fail_msg`) |
| `_format_hourly()` | `HourlyResult` | first 12 hours; precip % only when > 0 |
| `_format_alerts()` | `AlertsResult` | dedupe + severity ranking + honest cap, see below |
| `_format_aqi()` | `AirQualityResult` | AQI + per-pollutant ug/m3, only fields present |
| `_format_astronomy()` | `AstronomyResult` | field-presence-driven |
| `_format_historical()` | `HistoricalResult` | date always, other fields presence-driven |
| `_format_marine()` | `MarineResult` | "No marine data available" when all fields absent |
| `_format_uv()` | `UVResult` | index + category + peak |
| `_format_pollen()` | `PollenResult` | three source shapes: Google 0-5 indices, Pollen.com 0-12 overall, Open-Meteo per-species grains/m3 (checked in that order) |
| `_format_wildfire()` | `WildfireResult` | sized-count qualifier; sub-acre sizes never render as "0 acres" |
| `_format_space()` | `SpaceWeatherResult` | Kp + category + aurora % |
| `_format_tides()` | `TideResult` | station + next high/low + water temp; "No tide data available" if only the station is known |

`_format_alerts()` implements three field-hardening steps, each traceable to a
real incident recorded in its comments and each test-covered in
`run_tests.py`:

1. Dedupe by `(event, headline)` - NWS issues one alert per forecast zone, so
   a state-wide query repeats one warning per zone and three copies would eat
   the cap ("per-zone duplicates collapse to one line").
2. Stable sort by severity rank (`extreme < severe < moderate < minor <
   unknown`) - NWS returns newest-first, which buried a Tropical Storm
   Warning below routine statements past the cap ("the most severe alerts
   survive the cap").
3. Cap at 5 with an explicit `... and N more` marker - never silently drop
   ("says how many alerts were withheld past the cap").

### WeatherModule methods

- `_weather_cmd()` - the generic pipeline shared by 11 of the 15 commands:
  parse flags, warn/stop on unknown flag, `-l` listing, provider validation,
  geocode, log, fetch with optional `force_provider`, format, send. Handlers
  that need extra behavior (alerts widening, history date parsing, nowcast
  multi-part formatting, providers admin output) are written longhand and
  repeat the same prologue.
- `cmd_history()` - splits a leading `YYYY-MM-DD` token (`weather.py -
  _DATE_RE`) off the location, passes it as `target_date` (empty string means
  provider default).
- `cmd_nowcast()` - formats inline (summary + up to 8 timeline entries +
  source) rather than via a module-level formatter.
- `_send_provider_list()` - `-l` output: rejects unknown capabilities against
  `CAPABILITY_METHODS`, ranks the registered chain via
  `dispatcher._sorted_for_capability()`, tags each provider with a state badge
  from `provider_status()` (`_STATE_BADGE`: `[OK]` active / `[?]` cold /
  `[X]` failing; unconfigured providers never appear because the chain only
  contains registered ones), appends the flag aliases, and sends a legend
  line. Covered by `test_weather_flags.py - TestSendProviderList`.
- `_validate_provider()` - registered + supports-capability check with
  user-facing messages; `TestValidateProvider` covers all three branches
  including silent success.
- `cmd_providers()` - admin-only dump of `dispatcher.health_summary()` and
  `dispatcher.capability_matrix()` via `preply` (private reply).
- `help_lines()` - eight themed rows (bold labels via `\x02`), grouped so the
  output stays constant-size regardless of provider count; provider flags are
  summarized with a pointer to `-l`. Reads `len(dispatcher.provider_ids)`
  live.
- `setup()` - standard module entry point.

## Implementation walk

- Lines 1-24: module docstring (stale - see Findings), imports, logger. The
  only intra-repo imports are `base` (BotModule, strip_ctrl), `geocode`
  (geocode, us_state_code), and `units` (formatting helpers);
  `weather_providers` is imported lazily inside functions to keep module
  import light and reload-friendly.
- Lines 27-33: `_sanitize()` alias (security enforcement).
- Lines 36-367: the thirteen formatters (formatting; alert hardening as
  described above; each guarded by an isinstance check - validation).
- Lines 371-444: `_DATE_RE`; `_PROVIDER_FLAGS` alias table (business logic;
  the header comment sets the uniqueness rule and the `-aw` trap).
- Lines 447-517: `_parse_weather_flags()` (input validation / control flow)
  and `_flag_examples_for()` (formatting).
- Lines 520-663: class header, `COMMANDS`, `on_load()` (initialization),
  `_resolve()` (state read + privacy enforcement), `_geo()` (rate limit +
  geocode + error reporting), `_STATE_BADGE`, `_send_provider_list()`,
  `_validate_provider()`, `_warn_unknown_flag()`.
- Lines 667-694: `_weather_cmd()` generic pipeline (control flow).
- Lines 696-868: the fifteen `cmd_*` handlers - eleven one-line delegations to
  `_weather_cmd`, plus alerts/history/nowcast/providers longhand.
- Lines 870-900: `help_lines()` (formatting; the row helper pads inside the
  bold codes so columns align).
- Lines 903-905: `setup()`.

## Findings

- doc-drift | weather.py - module docstring | The header docstring lists only
  8 command families; the module registers 15 plus `.providers` (nowcast, uv,
  pollen, wildfire, space, tides are missing).
- doc-drift | weather.py - WeatherModule.help_lines() | Docstring says "Seven
  lines + the [weather] header"; the method returns 8 rows.
- questionable | weather.py - WeatherModule.cmd_alerts() | Calls
  `self._resolve(nick, rest)` a second time after `_geo()` already resolved
  the same argument, repeating the store read and the opt-out probe and
  discarding the error member; passing the raw string out of one resolution
  would remove the duplicate.
- questionable | weather.py - WeatherModule._resolve() | `-n <nick>` only
  matches when it is the entire argument, so trailing text (`.w -n bob Tokyo`)
  silently degrades into a junk free-text geocode of the literal string
  `"-n bob Tokyo"` instead of an error or a lookup.
- test-gap | weather.py - WeatherModule._resolve() | The opt-out refusal
  branch for cross-user `-n` lookups has no test in either
  `tests/test_weather_flags.py` or `tests/run_tests.py` (only the store-level
  opt-out flag is tested).
- test-gap | weather.py - _PROVIDER_FLAGS | The pollen aliases
  (`pc`/`pollendotcom`/`pollencom`, `gp`/`googlepollen`/`google_pollen`) are
  absent from the `TestProviderAliases` parametrization; only the generic
  self-alias invariant covers them.
