# weather_providers/__init__.py - package facade, provider registry, quota tracking

## Purpose

Public entry point of the multi-provider weather aggregation layer. It owns three
things: the factory registry that turns configuration into live provider objects,
the module-level `dispatcher` singleton plus the `get_*` convenience wrappers the
IRC side calls, and best-effort per-provider daily quota counters. Everything
heavier lives in the private submodules it re-exports from: normalized dataclasses
(`base.py`), routing (`_dispatch.py`), health/circuit-breaker state (`_health.py`),
and HTTP transport (`_http.py`).

## Responsibilities / boundaries

Belongs here:

- Provider factory functions (`_f_*`) and the `_PROVIDER_FACTORIES` registry -
  the only place that knows which credential each provider needs.
- `configure()` - rebuilds the dispatcher's provider set from a `ConfigParser`.
- The public async API: `get_weather` .. `get_tides`, one per capability.
- Introspection: `get_providers()`, `provider_capabilities()`, `provider_status()`.
- Daily quota visibility: `quota`, `record_call()`, `quota_status()`.

Deliberately not here:

- Provider implementations. Each provider is a sub-package
  (`weather_providers/<id>/`, e.g. `weather_providers/nws/`) with one module per
  endpoint (`current.py`, `forecast.py`, ...). This file only holds their
  factories; the sub-packages are separate documentation targets (32 of them,
  verified on disk and by `tests/test_dispatcher.py -
  TestProviderRegistration.test_factory_count_is_32`).
- Selection order, fallback, gap-fill (`_dispatch.py`); health scoring
  (`_health.py`); HTTP caps (`_http.py`); IRC formatting (`modules/weather.py`).

## Dependencies and dependents

Internal dependencies: `base` (dataclass re-exports), `_dispatch`
(`Dispatcher`, `CAPABILITY_METHODS`), `_health` (`health_registry`,
`format_health_score`), `_http` (`HTTPError`, `ResponseTooLargeError`),
`secret_store` (optional, credential lookup), and lazily each provider
sub-package inside its factory.

External: stdlib only (`configparser`, `datetime`, `threading`, `logging`).

Dependents: `modules/weather.py` (all commands), `tests/test_dispatcher.py`,
`tests/run_tests.py` weather section. `_dispatch.Dispatcher.dispatch()` imports
`record_call` back from this package at call time (deliberate late import to
break the `__init__` - `_dispatch` cycle).

## Lifecycle

Imported the first time `modules/weather.py` executes one of its lazy
`from weather_providers import ...` statements. Import builds the empty
`dispatcher = Dispatcher()` and registers all 32 factories; no provider objects
exist yet and no I/O happens. `configure(cfg)` is called from
`modules/weather.py - WeatherModule.on_load()` on bot start and on every module
reload; it clears and repopulates the dispatcher. There is no teardown here
(HTTP session cleanup lives in `_http.aclose()` / its atexit hook).

## State

- `_PROVIDER_FACTORIES: dict[str, factory]` - populated once at import by the
  `_reg()` calls; effectively immutable afterwards.
- `dispatcher` - module-level `Dispatcher` singleton, mutated only by
  `configure()`.
- `quota: dict[str, dict]` - in-memory per-provider daily counters
  (`{"day", "count", "limit"}`), guarded by `_quota_lock`; resets lazily when
  the UTC date rolls. Nothing is persisted; a restart zeroes both quota and
  health state.

## Concurrency

Quota mutations are serialized under `_quota_lock` (a `threading.Lock`, so it is
safe from both the event loop and any worker thread). `configure()` is expected
to run from module load/reload on the main loop; it is not locked against a
concurrent `dispatch()` - a reload while a request is in flight can briefly
dispatch against a partially rebuilt registry. In practice reloads are
operator-triggered and rare; the dispatcher copies nothing, so the worst case
is a request seeing the fallback-registered Open-Meteo only.

## Failure behavior

- A factory that lacks its credential returns `None` and the provider is simply
  not registered (logged at INFO, no error).
- A factory that raises is caught in `configure()` and logged at WARNING; the
  remaining providers still register.
- If nothing registered at all, `configure()` force-registers `OpenMeteoProvider`
  (keyless) so the weather commands never come up empty-handed.
- The `get_*` wrappers return `None` when the whole dispatch chain fails; they
  raise nothing themselves.

## Security

- Credentials come from `_cred()`: `secret_store.get(name)` first, then the
  `[weather_providers]` ini key as an upgrade-path fallback. Keys are passed
  into provider constructors and never logged; `_f_weatherkit()` deliberately
  logs only a missing-field *count* to keep CodeQL's sensitive-data heuristic
  (and actual secrets) out of the log line.
- `record_call` / `quota_status` accept arbitrary strings but only ever use them
  as dict keys - no injection surface.
- No network or filesystem I/O in this file.

## Classes

None. The package facade is function- and singleton-based; classes live in
`base.py` (dataclasses, protocol), `_dispatch.py`, and `_health.py`.

## Functions and methods

### `_cred(cfg, secret_name, ini_key)`

Credential resolution: `secret_store` first (ImportError-tolerant), then
`cfg["weather_providers"][ini_key]` stripped, else `""`. The ini fallback exists
for installs that have not yet run `python -m secret_store migrate`.

### `_today_utc()` / `_quota_entry_locked(provider_id)`

Date stamp `YYYY-MM-DD` (UTC) and the lazy get-or-roll of one quota entry.
`_quota_entry_locked` must be called with `_quota_lock` held; it recreates the
entry (count 0, limit from `_DEFAULT_QUOTA_LIMITS`) whenever the stored day is
not today, which is how the midnight-UTC reset works without any timer.

### `record_call(provider_id)`

Increments today's counter for the provider; no-op on empty id. Called by
`_dispatch.Dispatcher.dispatch()` once per provider *attempt* (before the call,
so failed calls count too). Its docstring still claims the dispatcher does not
call it - see Findings.

### `quota_status(provider_id)`

Returns `{"used", "limit", "remaining", "pct"}` for the provider. A missing or
non-positive limit normalizes to `limit=None, remaining=None, pct=0.0`. Purely
informational - nothing enforces these limits (the module comment says so
explicitly: "visibility, not enforcement").

### `_reg(pid, factory)` and the `_f_*` factories

`_reg` inserts into `_PROVIDER_FACTORIES`. Each `_f_<id>(cfg)` factory either
returns a constructed provider or `None` (credential missing, or for
`_f_weatherkit` PyJWT not installed). The provider class import happens inside
the factory, so an unconfigured provider costs no import time and a provider
with a broken dependency cannot break package import. `_f_pollendotcom` is the
odd one out: keyless, but it needs the Nominatim `weather_user_agent` for its
reverse-geocode hop, pulled through `_cred` (see Findings for the ini-fallback
mismatch with `modules/weather.py`).

### `configure(cfg)`

1. `dispatcher.clear()`.
2. Reads `provider_priority` (fallback key `priority`) from
   `[weather_providers]`. The list is an *ordering preference*, not an
   allowlist: unlisted known providers are appended after the listed ones so a
   stale config cannot silently disable whole capabilities (the comment records
   the concrete failure this prevents).
3. For each id in order: run the factory, `dispatcher.register(provider, pid)`
   when non-None; log the discovered capability set. Registration order becomes
   the dispatcher's final tie-break.
4. Fallback-register Open-Meteo if the registry ended empty.
5. Logs the provider chain and the full capability matrix.

Behavioral evidence: `tests/test_dispatcher.py - TestGlobalDispatch`
(`test_global_dispatcher_clears_on_configure`, `test_reconfigure_replaces_set`).

### `get_providers()` / `provider_capabilities(pid)` / `provider_status()`

Read-only views. `provider_status()` enumerates every *known* factory id, not
just registered ones, and classifies each as `unconfigured` (not registered),
`cold` (registered, zero calls), `failing` (success EMA <= 0.5), or `active`;
each entry carries call/failure counts, EMA success rate, composite health
score, and `quota_status()`. Shape is pinned by `tests/test_dispatcher.py -
TestProviderStatusShape`.

### `_force_kw(force_provider, kw)` and the `get_*` wrappers

Fourteen thin async wrappers, one per capability
(`current`, `forecast`, `hourly`, `alerts`, `air_quality`, `astronomy`,
`historical`, `marine`, `nowcast`, `uv`, `pollen`, `wildfire`,
`space_weather`, `tides`), all delegating to
`dispatcher.dispatch(capability, lat, lon, location, **kw)`. Each accepts
`force_provider=<id>` (injected into kwargs by `_force_kw`) which the
dispatcher consumes to pin the chain to one provider - this is what the
user-facing `-<provider>` flags in `modules/weather.py` ride on. Input
clamping: `get_forecast` clamps `days` to `1..16` (`_MAX_FORECAST_DAYS`);
`get_hourly` clamps `hours` to `1..48`. All other kwargs pass through to the
provider method untouched (`get_alerts` forwards `area=` for NWS state-wide
queries, `get_historical` forwards `target_date`).

## Implementation walk

- Lines 1-40: module docstring - architecture sketch, usage, and the ranked
  provider rationale (partial list; see Findings).
- Imports + `_PROVIDER_FACTORIES` declaration: re-exports the dataclass and
  error types so callers need only this package.
- `_cred()`: credential resolution (validation/compatibility).
- `__all__`: the supported public surface, including `dispatcher` itself and
  the error types.
- Quota block (`_DEFAULT_QUOTA_LIMITS`, `quota`, `_quota_lock`, `_today_utc`,
  `_quota_entry_locked`, `record_call`, `quota_status`): state mutation under a
  lock, lazy daily roll. The limits table is annotated per vendor; some entries
  are monthly caps stored in a daily-limit field (see Findings).
- Factory block: 32 `_f_*` functions plus 32 `_reg()` calls. The `_reg` order
  (NWS first, keyed tier-2/3 next, specialists last) is the default
  registration order when no `provider_priority` is configured, and therefore
  the last-resort tie-break in the dispatcher's sort.
- `dispatcher = Dispatcher()`: singleton creation.
- `configure()`: control flow described above; the priority-list append is
  compatibility logic guarding against stale configs.
- Introspection helpers: pure reads over dispatcher + health + quota.
- Dispatch wrappers: input clamping and delegation only - no business logic.

## IRC-side wiring and an end-to-end trace

`configure()` wiring: `modules/weather.py - WeatherModule.on_load()` calls
`weather_providers.configure(self.bot.cfg)` on module load and reload, then
separately resolves `weather_user_agent` (for geocoding) via
`modules/base.py - cred()`. Every command imports its `get_*` wrapper lazily
inside the handler, so the weather module stays loadable even if this package
is mid-reconfigure.

Trace of `.weather tokyo` to a reply, each function crossed:

1. IRC PRIVMSG -> command router -> `modules/weather.py -
   WeatherModule.cmd_weather(nick, reply_to, "tokyo")`.
2. `cmd_weather` -> `WeatherModule._weather_cmd("weather", "current", ...,
   fetch_fn=get_weather, format_fn=_format_current)`.
3. `_parse_weather_flags("tokyo")` - extracts `-l` / `-p <name>` /
   `-<provider>` aliases (table `_PROVIDER_FLAGS`); here none, so
   `force_provider=None`, `rest="tokyo"`. A `-nws`-style flag would flow into
   step 7 as `force_provider="nws"` after
   `WeatherModule._validate_provider()` confirms the provider is active and
   supports `current`.
4. `WeatherModule._geo` -> `WeatherModule._resolve` (literal arg vs saved
   location vs `-n <nick>` with opt-out check) -> `bot.rate_limited(nick)`
   cooldown gate -> `modules/geocode.py - geocode("tokyo", ua, ...)` ->
   `(lat, lon, display, cc)`.
5. `_weather_cmd` awaits `weather_providers.get_weather(lat, lon, display)`
   (`__init__.py`), which calls
   `dispatcher.dispatch("current", lat, lon, display)`.
6. `_dispatch.py - Dispatcher.dispatch`: resolves method name `get_weather`
   from `CAPABILITY_METHODS`; collects eligible providers; orders them with
   `Dispatcher.sort_chain` (static `DEFAULT_RELIABILITY` rank, then
   `ProviderHealth.health_score`, then registration order); sets the 45 s
   chain deadline.
7. Per provider until usable data: `ProviderHealth.is_callable()` breaker
   gate -> `__init__.record_call(pid)` quota tick ->
   `asyncio.wait_for(provider.get_weather(lat, lon, display),
   timeout=min(remaining, 30))`.
8. Inside e.g. Open-Meteo: `OpenMeteoProvider.get_weather` ->
   `weather_providers/openmeteo/current.py - fetch()` ->
   `_http.get_json(url, params=...)` -> `_get_json_aiohttp` (cached session,
   1 MiB streamed cap) -> normalized `base.WeatherResult`.
9. Back in `dispatch`: `WeatherResult.is_empty()` false ->
   `ProviderHealth.record_success(latency)`; because the capability is
   `current`, the result becomes the gap-fill `primary`; if
   `WeatherResult.has_gaps()` the chain keeps walking and
   `WeatherResult.fill_gaps()` merges missing secondary fields from up to two
   more providers; finally `WeatherResult.derive_missing()` computes
   feels-like/dewpoint from the primary's own observation and the result is
   returned up through `get_weather` to `_weather_cmd`.
10. `_weather_cmd` -> `_format_current(result)` (sanitizes strings, formats
    units via `modules/units.py - cf/kph/mb/km_mi`, appends the `[source]`
    credit) -> `bot.privmsg(reply_to, f":: {display} :: ... ::")` -> the
    sender queue writes the IRC line. On a None result the user gets
    "weather data unavailable right now." instead.

## Findings

- doc-drift | `__init__.py - record_call()` | Docstring says "This is **not**
  called from the dispatcher automatically - callers ... must invoke it", but
  `_dispatch.py - Dispatcher.dispatch()` calls `record_call(pid)` on every
  provider attempt; the note is stale and would mislead someone into
  double-counting.
- questionable | `__init__.py - record_call()` semantics | The counter counts
  dispatcher *attempts*, not upstream HTTP requests; a multi-hop provider (NWS
  makes 2-3 sequential requests per attempt, per the `_dispatch.py` budget
  comment) under-counts its real API usage, while the docstring promises "once
  per upstream request".
- questionable | `__init__.py - _f_pollendotcom()` | Its ini fallback reads
  `[weather_providers] weather_user_agent`, while `modules/weather.py -
  WeatherModule.on_load()` reads the same secret with ini fallback
  `[weather] user_agent`; an ini-only install that sets only the latter hands
  Pollen.com an empty User-Agent for its Nominatim reverse-geocode.
- questionable | `__init__.py - _DEFAULT_QUOTA_LIMITS` | `weatherapi` (1M/month)
  and `weatherstack` (1000/month) store monthly caps in the per-day `limit`
  field, so `quota_status()["pct"]` is misleading for those providers; the
  comment admits best-effort, but the field name says per-day.
- doc-drift | `__init__.py` module docstring | The ranked provider list names
  only the original 16 providers; the 16 specialist/single-capability providers
  (metno, waqi, openaq, iqair, sunrisesunset, currentuvindex, gdacs, eccc,
  nasapower, nifc, firms, swpc, tidecheck, noaa_coops, pollendotcom,
  google_pollen) are absent from the docstring and only discoverable in code.
- note (scoping) | provider implementations | The 32 provider sub-packages
  (`weather_providers/<id>/*.py`, one module per endpoint) are not documented
  here or in the other four package docs - they need separate assignment. The
  capability catalog below is derived from each sub-package's endpoint modules
  and factory requirements, for orientation only.

### Provider catalog (registration order; credential = secret_store name)

```{tabularcolumns} |l|l|p{0.36\linewidth}|p{0.26\linewidth}|
```

| id | credential | capabilities | notes |
|---|---|---|---|
| nws | none | current, forecast, hourly, alerts, marine | US only; api.weather.gov; multi-hop |
| meteomatics | meteomatics_username, meteomatics_password | current, forecast, hourly | premium ECMWF blend; basic auth |
| weatherkit | 4 fields + PyJWT | current, forecast, hourly, alerts | Apple; JWT-signed |
| openmeteo | none | current, forecast, hourly, air_quality, astronomy, historical, marine, nowcast, uv, pollen | broadest keyless provider; forced fallback |
| visualcrossing | visualcrossing_key | current, forecast, hourly, alerts, historical | ERA5 historical |
| accuweather | accuweather_key | current, forecast, hourly, alerts | 50/day free tier |
| openweathermap | openweathermap_key | current, forecast, hourly, alerts, air_quality | |
| weatherbit | weatherbit_key | current, forecast, hourly, alerts, air_quality, historical | 50/day free tier |
| weatherapi | weatherapi_key | current, forecast, hourly, alerts, air_quality, astronomy, historical | |
| pirateweather | pirateweather_key | current, forecast, hourly, alerts, nowcast | Dark Sky compatible |
| stormglass | stormglass_key | current, hourly, marine | marine specialist; 10/day tier |
| tomorrowio | tomorrowio_key | current, forecast, hourly, alerts, air_quality | |
| worldweatheronline | worldweatheronline_key | current, forecast, hourly, astronomy, historical, marine | |
| weatherstack | weatherstack_key | current, forecast, historical | plaintext HTTP upstream; least preferred |
| airnow | airnow_key | air_quality | US EPA, authoritative AQI |
| purpleair | purpleair_key | air_quality | crowdsourced PM2.5 |
| metno | none | current, forecast, hourly, alerts, nowcast | MET Norway / Yr |
| waqi | waqi_token | air_quality | |
| openaq | openaq_key | air_quality | |
| iqair | iqair_key | air_quality | |
| sunrisesunset | none | astronomy | sunrisesunset.io |
| currentuvindex | none | uv | currentuvindex.com |
| gdacs | none | alerts | disaster events, distance-filtered |
| eccc | none | alerts | Environment Canada |
| nasapower | none | historical | NASA POWER |
| nifc | none | wildfire | incident records, mostly unsized |
| firms | firms_key | wildfire | satellite detections, no acreage |
| swpc | none | space_weather | NOAA SWPC Kp + aurora |
| tidecheck | tidecheck_key | tides | |
| noaa_coops | none | tides | NOAA CO-OPS stations |
| pollendotcom | weather_user_agent (UA, not a key) | pollen | US only; Nominatim reverse-geocode |
| google_pollen | google_pollen_key | pollen | 0-5 Universal Pollen Index |
