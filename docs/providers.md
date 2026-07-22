# Weather subsystem - provider framework and reference

Internets 5.0.0. This is the maintainer manual for the weather aggregator: the
provider framework under `weather_providers/`, how `modules/weather.py` drives
dispatch, how `modules/geocode.py` turns a user string into coordinates, and a
complete reference of the 32 provider packages.

Everything here is grounded in the code as it stands. Read the cited files
alongside this doc; line numbers are navigation aids, not contracts.

## 1. Shape of the system

```
modules/weather.py        IRC command layer: flag parsing, geocode, fetch, format
  -> modules/geocode.py   location string -> (lat, lon, display_name, cc)
  -> weather_providers/__init__.py   public get_*() facade + provider registry
       -> _dispatch.Dispatcher       capability discovery + accuracy/health ordering + fallback
            -> _health.ProviderHealth EMA score + circuit breaker (per provider, global singleton)
            -> <provider>/            one package per upstream, one file per endpoint
                 -> _http.get_json    capped async HTTP (aiohttp, requests fallback)
       -> base.py                     frozen normalized dataclasses returned to the formatter
```

A request is `capability -> ordered provider chain -> first provider that
returns real data`. Providers do not know about each other; the dispatcher owns
ordering and fallback. Providers return one of the frozen dataclasses in
`base.py` or raise; the dispatcher converts a raise, a `None` return, or a
result with no usable core (`is_empty()`: a current result with no temperature,
an hourly result with no entries) into a fall-through to the next provider. For
current conditions it also gap-fills: a sparse result (e.g. NWS nulling
dewpoint/pressure/visibility) keeps its temperature and conditions and has only
its missing secondary fields filled from the next usable provider, crediting
both sources (`[NWS + Open-Meteo]`), bounded to 3 contributors and the chain
deadline (`_dispatch.py`, `base.py: WeatherResult.has_gaps/fill_gaps`).

The live count is 32 (`ls weather_providers/` minus the framework files and
`__pycache__`), across 14 capabilities. Both previously-stale hardcoded counts
(the package docstring and a session-cache comment in `_http.py`) have been
corrected; the docstring matters because autoapi renders it into the API
reference. `provider_status()` remains the runtime source of truth - prefer
querying it over trusting any count written into prose, including this one.

## 2. The normalized data contract (`base.py`)

`base.py` defines one frozen, slotted dataclass per capability result. Frozen +
slots is deliberate: a provider that tries to mutate a result or set an unknown
field raises `dataclasses.FrozenInstanceError` / `AttributeError`, which the
dispatcher classifies as a provider-code bug (section 4.6), not an upstream
outage.

| Dataclass | Capability | Key fields |
| --- | --- | --- |
| `WeatherResult` + `ForecastDay` | current, forecast | temperature, description, wind, pressure, dewpoint, `forecast: list[ForecastDay]` |
| `HourlyResult` + `HourlyEntry` | hourly | per-hour temp, precip, precip_chance, wind |
| `AlertsResult` + `AlertEntry` | alerts | event, severity, headline, start/end |
| `AirQualityResult` | air_quality | aqi (US EPA 0-500), category, pm25/pm10/o3/no2/so2/co, aod |
| `AstronomyResult` | astronomy | sunrise/sunset, day_length, moonrise/set, moon_phase, illumination |
| `HistoricalResult` | historical | date, high/low/avg, precip, max_wind, humidity |
| `MarineResult` | marine | wave/swell height+period+direction, water_temp, wind_wave |
| `NowcastResult` + `NowcastEntry` | nowcast | summary + per-step precip_mm/type/intensity |
| `UVResult` | uv | uv_index (now), uv_max (today), category |
| `PollenResult` | pollen | three coexisting models (see below) |
| `WildfireResult` | wildfire | fire_count, nearest_km, nearest_name, max_acres |
| `SpaceWeatherResult` | space_weather | kp_index, kp_category, aurora_pct |
| `TideResult` | tides | station, next_high/low time+height, water_temp |

`WeatherResult` is shared by both `current` and `forecast`. `get_weather`
populates the scalar fields; `get_forecast` populates `forecast` (a list of
`ForecastDay`). The forecast formatter returns `""` when `forecast` is empty,
which `modules/weather.py` treats as no data.

`PollenResult` normalizes three incompatible upstream models into one struct
(`base.py:278`); the formatter renders whichever group a provider filled:

- Open-Meteo / CAMS (Europe): per-species grains/m3 (`alder` ... `ragweed`).
- Google Pollen (global): tree/grass/weed Universal Pollen Index 0-5.
- Pollen.com / IQVIA (US): single `overall_index` 0-12 + `category` + `triggers`.

`base.py` also holds the shared helpers every provider imports: `deg_to_card`,
`ms_to_kph`, `km_to_m`, `haversine_km` (great-circle, clamps `sqrt(a)` to 1.0 so
near-antipodal float rounding can't throw a domain error - used by the
nearest-station/sensor/fire providers), and the category mappers `aqi_category`
(US EPA), `uv_category` (WHO), `kp_category` (NOAA G-scale), `pollen_cat_12`,
`pollen_cat_5`.

The provider protocol is `WeatherProvider` (`base.py:417`, `@runtime_checkable`
at `:416`). Required: `name: str`, `requires_key: bool`, `get_weather`,
`get_forecast`. All other `get_*` methods are optional - a provider supports a
capability iff it defines that method. The method names in the Protocol
docstring MUST match `_dispatch.CAPABILITY_METHODS`; that mapping is what
capability discovery keys on.

## 3. The capped HTTP client (`_http.py`)

Single choke point for provider HTTP. `get_json(url, *, params, headers,
timeout=_TIMEOUT, max_bytes=None)` returns parsed JSON or raises `HTTPError`.

- `_TIMEOUT = 10` seconds (`_http.py:27`). Per-request total timeout. Note this
  is the *transport* timeout for one HTTP hop; it is independent of, and nests
  under, the dispatcher's per-call and whole-chain budgets (section 4.3). A
  provider making N sequential hops (NWS, AccuWeather) can take up to N x 10s
  at the transport layer.
- `_MAX_RESPONSE_BYTES = 1_048_576` (1 MB) default cap. Overridable per call via
  `max_bytes=` or globally via `set_max_response_bytes()` (rejects < 1 KiB).
- Two transports, same interface: aiohttp when importable (true async), else
  `requests` + `asyncio.to_thread` (`_http.py:32`, selected at import). Both
  stream the body and enforce the cap **incrementally** (`iter_chunked` /
  `iter_content`, 64 KiB chunks): once cumulative bytes exceed the cap they
  raise `ResponseTooLargeError` before the oversize body is fully buffered
  (tagged SEC-WP-001, `_http.py:257`/`:330`). A naive `r.json()` would buffer
  the whole body first and defeat the cap - do not reintroduce that.
- aiohttp sessions are cached one-per-event-loop (`_session_cache`, keyed by
  `id(loop)`) to amortize TLS setup across the fan-out; `aclose()` /
  `_atexit_close` tear them down.
- 4xx/5xx: reads at most a 2048-byte body snippet for log context, then raises
  `HTTPError(status=..., is_rate_limit=(status==429))`. JSON decode failure,
  timeout, and client/transport errors all raise `HTTPError` with `status=None`.

`HTTPError` (`_http.py:41`) carries `status: int | None`, `provider_hint` (URL
host, for logs), and `is_rate_limit`. Its constructor forces `is_rate_limit =
True` whenever `status == 429` regardless of the passed flag (`_http.py:66`).
`ResponseTooLargeError` subclasses it with `status=None`. This typed surface is
what lets the dispatcher branch on exception *type* instead of sniffing
strings.

Not every provider routes through `_http.get_json`. `modules/geocode.py` has its
own capped reader (`_read_json_capped`, 128 KiB cap) and some providers fetch
via `modules.base.fetch_json`. The size-cap discipline is the invariant, not the
specific helper.

## 4. The dispatcher (`_dispatch.py`)

`Dispatcher` (`_dispatch.py:208`) is instantiated once as the module-global
`dispatcher` in `__init__.py:487`.

### 4.1 Capability discovery

`CAPABILITY_METHODS` (`_dispatch.py:33`) maps each of the 14 capabilities to the
`async def get_*` method name a provider must define to support it. On
`register()`, `_RegisteredProvider.__init__` walks that map and adds a
capability to the provider's set iff `hasattr(provider, method) and
callable(...)` (`_dispatch.py:201`). No declaration list - the method's presence
*is* the declaration.

### 4.2 Ordering: accuracy, then health, then registration

`sort_chain(capability, provider_ids)` (`_dispatch.py:274`; private alias
`_sorted_for_capability` kept for `modules/weather.py`) sorts by the tuple
`(rank, -health_score, reg_order)`:

1. **Static reliability rank** from `DEFAULT_RELIABILITY` (`_dispatch.py:75`),
   per capability, lower = more accurate, unlisted = 99. This is the dominant
   key. The ranking encodes model science: NWS (NDFD + human forecaster) leads
   US current/forecast; ECMWF/ICON-driven (Meteomatics, Open-Meteo, WeatherKit)
   next; GFS-derivative tier-3 (WeatherAPI, WeatherBit, Tomorrow.io) below;
   ERA5 reanalysis leads historical; Stormglass leads marine; radar-blended
   leads nowcast; CAMS leads air quality. The rationale block at
   `_dispatch.py:50` documents the per-capability reasoning.
2. **Health score** (negated so higher sorts first) - tie-break among
   comparably-accurate providers, prefers currently-up-and-fast.
3. **Registration order** (`reg_order`) - final tie-break, set from
   `provider_priority` in config (section 6).

Accuracy dominating health is a deliberate decision: a more-accurate provider
that is merely slower still wins until its breaker actually trips. Health and
reg order only separate providers that share a reliability rank.

### 4.3 The dispatch loop and its time budget

`dispatch(capability, *args, **kwargs)` (`_dispatch.py:320`) is the heart.

`force_provider` is a reserved kwarg, popped before forwarding
(`_dispatch.py:335`). When set it restricts the chain to that one provider; if
the provider is unregistered, lacks the capability, or its breaker is open, the
call returns `None` with no fallback (the caller's explicit choice).

Two nested deadlines bound the chain so one brownout upstream can't eat the
60s outer command timeout (`internets.py` `_CMD_TIMEOUT`) and starve the healthy
fallbacks queued behind it (`_dispatch.py:134`):

- `_CHAIN_BUDGET = 45.0` - whole fallback chain. Captured once as `deadline =
  time.monotonic() + 45` at loop start (`_dispatch.py:370`). Time already spent
  on slow earlier providers shrinks what later ones get. When `remaining <= 0`
  the loop breaks before trying the next provider and logs
  `dispatch_budget_exhausted`.
- `_PER_CALL_BUDGET = 30.0` - any single provider call. Each call is wrapped in
  `asyncio.wait_for(method(...), timeout=min(remaining, 30))` (`_dispatch.py:414`).
  A hang raises `asyncio.TimeoutError`, caught as a failure - so a brownout
  provider also trips its breaker instead of silently consuming budget.

Per-provider step (`_dispatch.py:372`):

1. Check chain budget; break if exhausted.
2. Skip if `not rp.health.is_callable()` (breaker open, section 5).
3. `record_call(pid)` - increments the quota counter (section 7). Imported
   lazily inside the loop to avoid the `__init__ <-> _dispatch` import cycle;
   wrapped in try/except so a quota bug can't break dispatch.
4. `await asyncio.wait_for(method(...), call_timeout)`.

### 4.4 Success, no-data, and the record_success-after-None check

This is a load-bearing distinction (`_dispatch.py:413`):

- **`result is None`**: the provider responded but has no data for this
  location (region it doesn't cover - e.g. a US-only provider asked about
  Tokyo, marine asked about an inland point). The loop `continue`s to the next
  provider and **deliberately does NOT call `record_success`**. A no-data (or
  slow no-data) result must not reset the breaker or improve the health score,
  or a provider that returns `None` fast forever would look perfectly healthy
  and keep winning its rank slot. Only real data is a success.
- **non-`None` result**: `rp.health.record_success(latency)` then return it.

A provider must therefore signal "not my region" by **returning `None`, not by
raising**. Raising routes into 4.5 and records a failure against a provider that
did nothing wrong. NWS is the worked example - see section 4.9.

### 4.5 Cross-provider gap-fill (`current` only)

NWS station observations routinely null `dewpoint`, `pressure`, `visibility`
and `textDescription`. Returning that as-is prints a line of `N/A` even when the
next provider in the chain has the values, so for the `current` capability the
dispatcher keeps walking and merges (`_dispatch.py:429-441`):

1. The first usable result becomes `primary`.
2. If `primary.has_gaps()` (`base.py:122`), the loop continues instead of
   returning.
3. Each subsequent usable result is folded in with
   `primary.fill_gaps(other)` (`base.py:129`), which copies **only** fields the
   primary is missing and never overwrites one it has.
4. Stops when `not primary.has_gaps()` or `merged >= 3`, and is bounded by the
   same chain deadline as everything else.
5. Source credit becomes `"NWS + Open-Meteo"` so the output names every
   contributor.

If the chain is exhausted with a still-sparse primary, it is returned anyway
(`_dispatch.py:474`) - a partial answer beats none.

#### The derived-field invariant (do not undo this)

`_CURRENT_GAP_FIELDS` (`base.py:75`) is the gap-fill set:

```python
_CURRENT_GAP_FIELDS = (
    "humidity", "wind_kph", "wind_dir",
    "pressure_mb", "visibility_m", "description",
)
```

`feels_like_c` and `dewpoint_c` are **deliberately absent**, and `temperature`
was never in it. Those three are not independent measurements - feels-like and
dewpoint are *derived from* an observation's own temperature, humidity and wind.
Importing one from a provider that measured a different temperature produces a
line that contradicts itself.

Observed: `.w yosemite national park` printed `Temperature 24.2C :: Feels like
11.3C` at 44% humidity and 6.6mph wind, a figure no apparent-temperature formula
yields. NWS read the nearest station (2900m elevation, 714mb) at 24.22C and
supplied no feels-like; Open-Meteo's model grid read 13.8C and contributed a
feels-like of 11.9C computed against *its* temperature. The two providers were
describing points 10.4C apart. `.w 91773` erred the other way - 24.4C shown
beside a borrowed 28.8C.

The rule: **a derived field must come from the same observation as the
temperature printed beside it.** Providers populate them natively or leave them
`None`. If you add a field to `WeatherResult`, decide which kind it is before
adding it to this tuple; when in doubt, leave it out, because a missing value is
honest and a borrowed one is not.

Regression cover: `tests/test_dispatcher.py::TestGapFill` -
`test_derived_fields_are_never_imported_from_another_observation` and
`test_missing_derived_fields_do_not_keep_the_chain_walking` (the latter pins
that these fields, being unfillable, must not keep the chain burning provider
calls).

### 4.6 Failure handling

On any exception (`_dispatch.py:443`):

- Classify rate-limit via `_is_rate_limit_error` (`_dispatch.py:147`): prefers
  structured signals (`HTTPError.is_rate_limit`, `status == 429`), falls back to
  a narrow substring sniff (`_RL_TOKEN_HINTS`) only for non-`HTTPError`
  provider-raised exceptions.
- `rp.health.record_failure(rate_limited=...)`.
- If `HTTPError` with status 401/403: `rp.health.mark_auth_failure()` - trips
  the breaker *immediately* (section 5.4) so a bad/unentitled key stops burning
  one request per dispatch; it re-probes after the cooldown.
- Log `dispatch_fail` as a single grep-friendly line. Args/kwargs (lat/lon/loc)
  are deliberately not logged - URL params can include API keys; `_redact`
  (`_dispatch.py:171`) additionally scrubs `apikey/appid/key/token/...=` from
  the error string and truncates to 160 chars.

The loop always `continue`s on failure - one broken provider can never take the
bot down. If every provider in the chain fails or returns no data, `dispatch`
logs `All providers failed` and returns `None`; the command layer then prints
the per-command failure message.

### 4.7 Provider-bug exceptions

`_BUG_EXC_TYPES` (`_dispatch.py:130`: `TypeError, AttributeError, KeyError,
IndexError, NameError, FrozenInstanceError`) signal a defect in the provider's
own code (mis-constructed dataclass, bad key/index) rather than an upstream
outage. The dispatcher still falls through (resilience), but additionally logs
`dispatch_bug` at ERROR so the defect surfaces instead of hiding behind the
normal "provider unavailable" path.

### 4.8 Introspection helpers

`capabilities()` -> `{capability: [provider_ids]}`. `capability_matrix()` ->
human-readable per-capability chain string (used by `.providers`).
`health_summary()` -> delegates to `health_registry.summary()`. `get_provider`,
`provider_ids`, `register`/`unregister`/`clear`.

### 4.9 Coverage vs failure: the NWS worked example (`nws/_scope.py`)

api.weather.gov serves US points only, and says so three different ways:

| surface | signal |
|---|---|
| `/alerts/active?point=` | HTTP 400 `Parameter "point" is invalid: out of bounds` |
| `/points/{lat},{lon}` | HTTP 404 `Data Unavailable For Requested Point` |
| any endpoint | HTTP 200 whose payload carries no station, forecast URL or marine zone |

All three used to surface as exceptions, so every non-US `.w` or `.al` took the
4.6 path and called `record_failure()` against NWS. Enough of them open the
breaker (section 5.4) and US alerts then fall through to a less authoritative
provider - a provider degraded by questions it was never able to answer. An
inland `.marine` did the same: not being in a marine zone is a normal answer,
not a fault.

`_scope.py` converts all three into one `None`:

- `_NO_DATA_STATUSES = frozenset({400, 404})` (`_scope.py:34`). Every request
  here is built from validated coordinates, so these mean the *point* is
  unsupported, not that the request was malformed.
- `nws_json()` (`_scope.py:41`) wraps `get_json` and re-raises those two
  statuses as `OutOfCoverage`. **Every other status still raises `HTTPError`** -
  401/403/429/5xx must stay failures so the breaker and rate-limit accounting
  still see them.
- The payload-shaped cases raise `OutOfCoverage` directly at their check sites
  (`current.py:18,22`, `forecast.py:12`, `hourly.py:14`, `marine.py:18,24`).
- `none_if_uncovered()` (`_scope.py:55`) awaits a fetch and returns `None` on
  `OutOfCoverage`. Every `NWSProvider` method wraps its fetch in it
  (`nws/__init__.py`).

Coverage is left to upstream rather than a hardcoded bounding box. A box for
CONUS + Alaska + Hawaii + territories drifts the moment NWS changes what it
serves, and would have to handle the Aleutians' antimeridian wrap besides.

**Apply this pattern to any regional provider you add.** ECCC (Canada), Met.no,
and the US-only air-quality providers have the same shape. The test is not "did
the call fail" but "is this provider *able* to answer for this point" - and only
the second one should ever touch the breaker.

Regression cover: `tests/test_new_weather_capabilities.py::TestNWSCoverage`,
including a parametrized check that 403/429/500/503 still propagate.

## 5. Per-provider health and circuit breaker (`_health.py`)

One `ProviderHealth` per provider id, held in the global `health_registry`
singleton (`_health.py:366`). State survives a module reload because the
registry is module-global, not rebuilt by `configure()`.

### 5.1 Composite health score

`health_score` (`_health.py:138`, a property) is a weighted EMA blend:

- `success_rate` (weight 0.70) - EMA of 1.0/0.0 per call, `_ALPHA = 0.1` (slow,
  stable adaptation).
- latency component (0.20): `max(0, 1 - avg_latency/_LATENCY_CAP)`,
  `_LATENCY_CAP = 10.0`s.
- rate-limit component (0.10): `max(0, 1 - decayed_rate_limit/_RATELIMIT_CAP)`,
  `_RATELIMIT_CAP = 5`.

Cold-start interpolation: below `_MIN_SAMPLES = 3` recorded calls the score is
interpolated from `_COLD_DEFAULT = 0.90` toward the live score, so a brand-new
provider doesn't instantly out- or under-rank a 100-call provider on one data
point.

The score read is intentionally lock-free (`_health.py:153`): a concurrent
transition may give one stale answer, acceptable for a coarse ranking guardrail.

### 5.2 Failure penalizes latency too

`record_failure` (`_health.py:283`) pushes `_FAILURE_LATENCY = _LATENCY_CAP`
(10s) into the latency EMA. Without this a provider returning 500s in 50ms would
look "fast" and keep out-ranking healthy peers on the latency axis. A failure
dings both the success and latency components.

### 5.3 Rate-limit counter decay

`rate_limit_count` is a float with a half-life of `_RATELIMIT_HALFLIFE = 300`s
(`_health.py:45`). Decay is computed on read (`_decayed_rate_limit`, pure) and
applied lazily under lock on the next `record_*` call. A success also steps the
counter down by 1.0. So a 429 storm fades on its own and a clean recovery
actively shrinks it - a transient quota burst can't permanently lock a provider
out of its rank.

### 5.4 Circuit breaker

Layered on top of the EMA as a coarse discrete guardrail. State machine
(`_health.py:184`):

- **closed**: normal. Failures accumulate in a rolling window.
- **closed -> open**: `_CB_THRESHOLD = 5` consecutive failures within
  `_CB_WINDOW = 60`s.
- **open**: all calls refused. `is_callable()` returns `False` and
  `health_score` is force-pinned to `0.0` for `_CB_COOLDOWN = 60`s.
- **open -> half_open**: after the cooldown, `is_callable()` releases exactly
  one probe (transition happens lazily inside `is_callable()` under lock).
- **half_open -> closed**: probe succeeds. **half_open -> open**: probe fails.

The breaker is not a strict semaphore: in half_open, concurrent callers may all
see `is_callable() == True`. That's accepted - it's a guardrail, not a lock.

`mark_auth_failure()` (`_health.py:306`) is the immediate-trip path: on a 401/403
it sets state `open` now and forces `cb_consecutive_failures = cb_threshold`,
logging at ERROR with "check the API key/entitlement". It still re-probes after
the cooldown, so fixing the key recovers the provider automatically without a
restart.

The dispatcher gates on `is_callable()` *before* each call (`_dispatch.py:392`),
so an open breaker means the provider is skipped entirely, not invoked and
caught.

`circuit_state`, `summary()` (the per-line `.providers` output), and the
registry's `summary()` (sorted by score) provide read access. `ProviderHealth`
mutators take a `threading.Lock` because health is touched from worker threads
in the requests-fallback path.

## 6. The public facade and registry (`__init__.py`)

### 6.1 Public API

`__init__.py` exports the dataclasses, `configure`, the 14 `get_*` coroutines,
`dispatcher`, `provider_status`, `quota_status`/`record_call` (plus the
module-global `quota` dict, importable by name though not in `__all__`), and the
error types. Each `get_*` (e.g. `get_weather`, `__init__.py:609`) is a thin
wrapper over `dispatcher.dispatch(capability, lat, lon, location, **kw)`. They
all accept `force_provider=<id>`; `_force_kw` injects it only when non-`None`
so normal calls keep full fallback. `get_forecast` clamps `days` to
`[1, _MAX_FORECAST_DAYS=16]`; `get_hourly` clamps `hours` to `[1, 48]`.

### 6.2 Credential resolution

`_cred(cfg, secret_name, ini_key)` (`__init__.py:65`): tries `secret_store.get`
first, falls back to `config.ini [weather_providers] <ini_key>`. The fallback
keeps the bot working before `python -m secret_store migrate`; after migration
the ini values are blanked and only the secret store holds them. Secrets are
never logged - the WeatherKit factory counts missing fields as an aggregate int
rather than naming them (`__init__.py:257`) specifically to dodge CodeQL's
clear-text-logging heuristic.

### 6.3 Factory registry and configure()

Each provider has a `_f_<id>(cfg)` factory registered via `_reg` into
`_PROVIDER_FACTORIES` (`__init__.py:451`). A keyed provider's factory returns
`None` (and logs `skipped (no <key>)`) when its credential is absent - that is
how an unconfigured provider stays out of the chain. Keyless providers always
construct.

`configure(cfg)` (`__init__.py:490`), called on module load/reload:

1. `dispatcher.clear()`.
2. Read `provider_priority` (or legacy `priority`) from
   `[weather_providers]`. This is an **ordering preference + reg-order
   tie-break, NOT an allowlist**. Every provider not named is appended after
   the listed ones (`__init__.py:505`) so providers added after the config was
   written still register (they just sort last). A stale list therefore can't
   silently disable whole capabilities - the failure mode this guards against.
3. For each id in order: run the factory; if it returns non-`None`,
   `dispatcher.register`. A factory raising is caught and logged, not fatal.
4. If *nothing* registered, register Open-Meteo as a hard fallback
   (`__init__.py:524`) - keyless, so it always works.
5. Log the resulting chain and capability matrix.

### 6.4 provider_status()

`provider_status()` (`__init__.py:546`) snapshots every *known* provider
(active + unconfigured) with a `state`:

- `unconfigured` - factory exists, no key, never registered.
- `cold` - registered, zero calls yet.
- `failing` - registered, calls happened, `success_rate <= 0.5`.
- `active` - registered, calls happened, `success_rate > 0.5` (auth currently
  working).

This drives the `-l` listing badges in `modules/weather.py` (`[OK]`/`[?]`/`[X]`).

## 7. Quota tracking (`__init__.py:102`)

Per-provider daily call counter, reset at UTC midnight, compared to a per-tier
limit (`_DEFAULT_QUOTA_LIMITS`, `__init__.py:131`). Visibility only, **not
enforcement** - the dispatcher never refuses a call on quota. `record_call(pid)`
is invoked once per attempted upstream call from inside `dispatch`
(`_dispatch.py:402`), counting failures too so `quota_status` reflects reality.
`quota` is a module-global dict (readable directly); mutations are serialized
under `_quota_lock`. Limits are best-effort approximations of vendor free
tiers - several vendors publish monthly or per-minute caps that don't translate
cleanly to calls/day (see the comment block). `None` = no published cap (NWS,
Open-Meteo, Meteomatics, WeatherKit, PurpleAir).

## 8. The command layer (`modules/weather.py`)

`WeatherModule` (`modules/weather.py:483`) registers the commands: `.weather/.w`,
`.forecast/.f`, `.hourly/.h`, `.alerts/.al`, `.aqi/.air`, `.astro/.sun`,
`.history/.hist`, `.marine/.sea`, `.nowcast/.nc`, `.uv/.uvi`, `.pollen/.allergy`,
`.wildfire/.fire`, `.space/.aurora`, `.tides/.tide`, and `.providers` (admin).

### 8.1 Command lifecycle

The generic path is `_weather_cmd` (`modules/weather.py:630`): parse flags ->
optional `-l` listing or `-p` validation -> geocode -> rate-limit check -> fetch
-> format -> send. `.alerts`, `.history`, `.nowcast` have their own handlers
because their output or argument shape differs (alerts emits multiple lines,
history parses a leading `YYYY-MM-DD`, nowcast renders step entries).

`on_load` (`modules/weather.py:510`) calls `configure(self.bot.cfg)`, loads the
Nominatim User-Agent via `cred()` (secret_store -> `[weather].user_agent`),
warns if absent (geocoding disabled without a contact UA), and reads
`[weather].default_country` (default `us`) for postal-code resolution.

`_resolve` (`modules/weather.py:527`) turns the arg into a location string:
`-n <nick>` looks up another user's saved location (refused if that nick opted
out of location sharing, unless it's the invoker themselves); otherwise the raw
arg, or the invoker's own saved location, or an error telling them to
`.regloc`. There is deliberately no operator-default-location fallback - silently
answering with a default point confuses users into thinking it's their weather.

### 8.2 Provider flags

`_PROVIDER_FLAGS` (`modules/weather.py:342`) maps short aliases to canonical
provider ids: `-nws`, `-mm`/`-meteomatics`, `-wk`/`-aw`/`-apple`/`-appleweather`
(Apple WeatherKit - note `-aw` is Apple, NOT AccuWeather, which is `-acc`),
`-om`, `-vc`, `-acc`, `-owm`, `-wb`, `-wapi`, `-pw`/`-pirate`, `-sg`, `-tio`/
`-tomorrow`, `-wwo`, `-ws`, `-an` (AirNow), `-pa`, `-waqi`, `-oaq`, `-iq`,
`-yr`/`-metno`, `-ss`, `-cuv`, `-gdacs`, `-eccc`, `-power`, `-nifc`, `-firms`,
`-swpc`, `-tc` (tidecheck), `-coops` (noaa_coops), `-pc`/`-pollencom`,
`-gp`/`-googlepollen`.

`_parse_weather_flags` (`modules/weather.py:410`) pulls flags out of anywhere in
the line: `-l` (list providers for the capability), `-p <name>` (legacy explicit
form), `-<alias>` (force a provider), and passes through `-n <nick>` and bare
`YYYY-MM-DD` untouched. A token that looks like a flag but matches no alias is
captured as `unknown_flag` and dropped from the geocoder query; the command
warns and aborts. A forced provider is validated with `_validate_provider`
(must be active AND support the capability) before dispatch; it becomes
`force_provider=` into the `get_*` call.

`-l` (`_send_provider_list`, `modules/weather.py:580`) lists only active,
key-configured providers ranked by accuracy for that capability, each badged
with auth state. Unconfigured providers are hidden - only what's usable shows.

### 8.3 Formatting and output safety

Each `_format_*` function type-checks its argument against the expected
dataclass (raising `TypeError` on mismatch) and emits a ` :: `-joined IRC line.
Every upstream string passes through `_sanitize` (`strip_ctrl`, default 200
chars) before it touches an IRC line - upstream display names and descriptions
are attacker-influenceable (OSM is user-editable; API text is third-party), so
C0/DEL control bytes and over-length strings are stripped to prevent IRC command
injection and line-limit overruns. Hourly is capped at 12 entries, alerts at 5,
nowcast at 8.

`.providers` (`cmd_providers`, admin only) dumps `health_summary()` and
`capability_matrix()`.

**Feels-like is shown whenever it is known** (`modules/weather.py:62`). It was
previously suppressed unless it differed from the temperature by 2 degrees,
which made "we don't know" and "it feels like what it is" indistinguishable.
Once the value started coming from the same observation as the temperature
(section 4.5), the honest answer is usually a close one rather than a missing
one, so the suppression was removed. A `None` stays absent - never synthesized
from the temperature.

### 8.4 `.alerts` scoping: point vs area

`.alerts` does not use the generic `_weather_cmd` path. Beyond geocoding, it
asks whether the query names a whole US state (`modules/weather.py:730`):

```python
raw, _ = self._resolve(nick, rest)
area = us_state_code(raw) if raw else None
```

A bare state name or USPS code passes `area=XX` through the dispatcher into
`NWSProvider.get_alerts`, which forwards it to `alerts.fetch(..., area=...)`
(`nws/alerts.py:14`). `area` and `point` are mutually exclusive - sending both
narrows straight back to the point.

Why: a state geocodes to one inland coordinate, and a point lookup returns only
the alerts whose polygon covers that exact spot. With Tropical Storm Bertha's
centre on the Mississippi coast, `.al mississippi` returned a single Heat
Advisory from NWS Jackson. Measured against api.weather.gov at the time:

```
point = Jackson MS       ->  1 alert
point = Biloxi MS coast  ->  4 alerts (incl. Tropical Storm Warning)
area  = MS               -> 17 alerts (3x TS Warning, 3x TS Watch, ...)
```

Naming a place *inside* a state (`jackson mississippi`) stays a point lookup -
that user asked about a place, not a state.

The other alert providers receive `area` in their `**kw` and ignore it; all 12
`get_alerts` implementations accept `**kw`.

#### Making a 17-alert state readable

Widening the query exposed two further ways the important alert stayed hidden
(`_format_alerts`, `modules/weather.py:116`):

1. **Per-zone duplicates.** NWS issues one alert per forecast zone, so a
   state-wide query repeats the same warning across every zone it covers. Three
   identical Tropical Storm Watches consumed the 5-line cap. Identical
   `(event, headline)` pairs are collapsed first.
2. **Newest-first ordering.** NWS returns most-recently-issued first, so routine
   statements outranked the actual Tropical Storm Warning. Entries are sorted by
   `_RANK` (`modules/weather.py:137`: extreme < severe < moderate < minor <
   unknown), stable so equal-severity alerts keep the provider's own order.

Anything past the cap is reported as `... and N more` (counted over the
*deduped* set) rather than vanishing. A state under a hurricane can carry 15+;
showing 5 with no marker reads as "that is all of them".

### 8.5 `.wildfire` acreage

`WildfireResult` (`base.py:349`) carries `max_acres` and `sized_count`.

NIFC's WFIGS current-incident layer has four acreage fields. `DiscoveryAcres` is
the size at *initial report* and sits at a dispatch default of 0.01 on nearly
every record; `IncidentSize` is the current size. Reading the wrong one printed
`46 active fire(s) nearby :: Largest 0 acres` while the SUMMIT fire was burning
2690 acres at 98% containment.

`IncidentSize` is null on most records - small incidents nobody sized - so
`sized_count` reports how many of `fire_count` carry a size at all, rather than
implying the whole set is measured:

```
46 active fire(s) nearby (8 sized) :: Nearest LAC-253228 5km :: Largest 2,690 acres :: [NIFC]
```

Detection-only sources (NASA FIRMS) leave `sized_count` at 0 and print the plain
count with no acreage. Sub-acre sizes render with two decimals rather than
rounding to a bare `0 acres` - 0.1 acres is a real fire.

## 9. Location resolution (`modules/geocode.py`)

`geocode(query, user_agent, *, default_country="us")` (`geocode.py:720`) ->
`(lat, lon, display_name, cc)` or `None`. An `async def` awaited directly by the
command layer (`modules/weather.py:565`); it offloads its own blocking `requests`
calls to threads internally.

### 9.1 Pipeline order

1. **Validate / cap**: strip quotes, reject empty, cap query to
   `_MAX_QUERY_CHARS = 200`, coerce `default_country` to a safe ISO2 via
   `_normalize_cc` (bad value -> `us`, so a typo'd `default_country` can't
   disable the home bias or inject junk into `countrycodes`).
2. **UA gate**: `_ua_has_contact` requires the User-Agent to embed an email or
   URL (`geocode.py:109`). Without a contactable UA, geocoding is refused
   entirely - Nominatim bans generic UAs and a ban hits the whole channel's IP.
3. **TTL cache**: keyed on `(lowercased query, user_agent, default_country)`,
   24h TTL, 1000-entry LRU `OrderedDict` under a lock. Caches negative results
   (`None`) too, so a flood of identical bad queries can't hammer Nominatim.
   `default_country` is in the key because the same bare numeric code resolves
   differently per home country.
4. **Coordinate passthrough** (`_parse_coords`): decimal pairs, hemisphere
   decimals (`39N 98W`), and DMS (`39 50 15 N`). Parsed deterministically and
   range-validated, then reverse-geocoded to a name. This exists because
   free-text Nominatim mangles non-decimal coordinate forms (`39N 98W` resolves
   to a random Missouri suburb). A bare `39 98` (no comma/sign/decimal) is
   intentionally rejected as too ambiguous.
5. **Postal-code resolution** (structured, country-aware) - see 9.2.
6. **Settlement pass + unconstrained free-text with word-drop** - see 9.3.

### 9.2 Postal codes: classifier + structured lookup

The key insight (`geocode.py:382`): free-text `q=` fuzzy-matches a postal code
against the nearest building, so `08000` pinned to the US returns a random Ohio
motel and `A1A 1A1` unpinned returns a Swiss street. The fix is to **classify**
the input and resolve with structured `postalcode=` / Zippopotam lookups that
match the value *as a postal code* - a bogus code returns nothing instead of
garbage. Postal codes deliberately never fall through to the fuzzy free-text
loop.

`_postal_kind` (`geocode.py:404`) classifies:

- `us` - ZIP+4 (`\d{5}-\d{4}`, unambiguously US).
- `ca` - Canadian alphanumeric (`A1A 1A1`).
- `uk` - UK postcode.
- `ie` / `jp` / `br` - country-unique formats (Eircode, dashed-JP `\d{3}-\d{4}`,
  dashed-BR CEP `\d{5}-\d{3}`); the kind *is* the ISO2, pinned directly.
- `num` - bare numeric `\d{4,10}`, shared across countries -> home-first.
- `None` - not a postal code -> free-text.

CA/UK/IE are kept disjoint by their trailing structure (CA ends in a digit, UK
in two letters, IE in a 4-char group). `_split_postal_country` (`geocode.py:446`)
honors an explicit trailing country override (`08000 spain` / `08000 es`), but
only when the leading part is itself a postal code - so `london ontario` and
`paris france` fall through unchanged. A bare 2-letter tail is accepted only if
it's a real ISO2 that is NOT a US-state or CA-province abbreviation
(`_ISO2_OVERRIDES`, `geocode.py:439`), so `90210 ca` stays the California ZIP
rather than mis-pinning to Canada.

`_resolve_postal` (`geocode.py:682`):

- `ca` -> Zippopotam by FSA (first 3 chars; Nominatim lacks Canadian postal
  data, which is proprietary Canada Post).
- `us` (ZIP+4) -> resolve the 5-digit base US-pinned via Nominatim, Zippopotam
  backstop (the +4 is sub-ZIP granularity neither source carries).
- `uk` -> Nominatim pinned to the hint or `gb`.
- `ie`/`jp`/`br` -> Nominatim pinned to the kind.
- `num` with explicit hint -> Nominatim then Zippopotam pinned to the hint.
- `num` no hint -> home country first (Nominatim then Zippopotam), then global
  best-match. So `43812` -> Ohio, `08000` -> Barcelona.

Nominatim postal (`_nominatim_postal`) and Zippopotam (`_zippo`) both fail
closed (`None`) on any missing/oversize/unparseable field; `_zippo` treats a 404
as a clean miss.

### 9.3 Place names: the settlement pass and why it exists

Nominatim's free-text `q=` returns the single best-ranked OSM object of **any**
class. For a weather lookup that is the wrong contract: a query that happens to
name a business outranks the place it was named after.

Observed, US-pinned (the pin comes from the state name in the query):

```
.al new york new york      -> tourism/hotel   New York New York Hotel and Casino, Las Vegas
.al north shore new jersey -> highway/residential  North Shore Boulevard, Helmetta NJ
```

`featureType=settlement` constrains a search to cities/towns/villages. But
preferring the settlement *unconditionally* breaks the mirror case - a township
named Graceland in South Africa preempts the Memphis landmark. So both searches
run and the more prominent object wins.

`_search_place(candidate, hdrs, *, feature_type=None)` (`geocode.py:754`) is the
single implementation both passes share. It returns `(hit, importance, stop)`:

- `hit` - `(lat, lon, name, cc)` or `None`.
- `importance` - the matched object's OSM prominence (0.0 if upstream omits it).
- `stop` - `True` when the caller must not retry with a shorter query: transport
  failure, or a row whose lat/lon is missing, unparseable, or out of range.
  Retrying either just burns requests against the 1 req/s policy.

`countrycodes` is derived fresh per candidate inside the helper (`_looks_like_us`
for US states/abbrevs/ZIP, else `_country_code_for` for country names and CA/AU
subdivisions).

The resolution order (`geocode.py:939`):

1. Settlement pass on the **full** query. `stop` here returns `None`
   immediately - Nominatim is unhappy, do not hammer it.
2. The unconstrained loop. On a hit for the full query, the settlement hit wins
   if `sett_imp >= free_imp`; otherwise the free-text hit does.
3. On a hit only *after* dropping tokens, a full-query settlement hit always
   wins - a truncated query answers a different question than the user asked.
4. If the unconstrained search finds nothing usable, the settlement hit (if any)
   is returned.

**`importance` is used only to rank two answers to the same query.** It is
worthless as an absolute quality bar: Oxford Circus (the wrong answer for
`circus circus`) scores 0.5086 and Graceland (the right answer for `graceland`)
scores 0.5087. The result *class* is the signal; the score only breaks a tie
between two candidates for one query. If an upstream ever stops returning
`importance`, both scores fall to 0.0 and the tie goes to the settlement - which
still fixes the casino case but silently reinstates the Graceland one, so the
ranking is only as good as that field.

Word-drop is unchanged and still capped at `_MAX_DROPS = 4` (`geocode.py:978`),
an abuse control: an adversarial many-token query would otherwise drop one token
at a time for ~100 sequential requests. Worst case per query is now 6 requests
(settlement + initial + 4 drops), up from 5.

It still recovers trailing-token typos (`la quinta caifornia` -> `la quinta`)
and the Georgia clash: `Georgia` is a US state, so `tbilisi georgia` first pins
to `countrycodes=us` and misses, then drops `georgia` and resolves `tbilisi`
unconstrained (`_COUNTRY_NAME_MAP` deliberately omits `georgia` for this reason,
`geocode.py:252`).

Known residual: `circus circus` resolves to City of Westminster. Nominatim ranks
Oxford Circus above the Las Vegas casino globally and the query carries no city;
`circus circus las vegas` resolves correctly. Arbitrating business names is out
of scope for a weather bot.

### 9.4 Whole-query US state detection (`us_state_code`)

`us_state_code(query)` (`geocode.py:249`) returns a USPS code only when the
**entire** query is a bare state name or abbreviation. `_STATE_QUERY` maps both
directions, lowercased.

This is deliberately not `_US_STATE_ABBR_RE`, which scans *within* a query and
so must stay uppercase-only to avoid matching `ca`/`or`/`in` as ordinary words.
Matching the whole query is unambiguous: a user who types just `ms` means
Mississippi.

```python
us_state_code("mississippi")        # "MS"
us_state_code("ms")                 # "MS"
us_state_code("jackson mississippi")# None - a place INSIDE the state
```

The consumer is `.alerts` (section 8.4). All 51 codes it can emit (50 states +
DC) were verified accepted by api.weather.gov as `area` values.

### 9.5 Display-name safety and the feature fallback

`_format_name(addr, fallback)` (`geocode.py:591`) builds `City, ST` (US, USPS
abbreviation via `_STATE_ABBR`) or `City, Country`.

Nominatim returns no `city`/`town`/`village`/`county` for parks, landmarks and
nature reserves, which collapsed the output to a bare state - `.w yosemite
national park` announced itself as `:: CA ::`. When there is no city component,
the feature's own name is taken from the first component of the display_name
fallback, giving `Yosemite National Park, CA`.

That split is guarded on `", "` being present, because the reverse-geocode path
passes a bare `lat,lon` pair as its fallback - comma, no space - and splitting
that would print just the latitude.

Every component is `_strip_ctrl`'d - all of it ultimately comes from
user-editable OSM data, so a vandalized place name (`\r\nQUIT :pwned`,
reverse/bold/color codes) can never be spliced into an IRC line. Names are
capped at `_MAX_NAME_CHARS = 160`.

## 10. WeatherKit JWT signing (`weatherkit/__init__.py`)

Apple WeatherKit is the only provider that mints its own bearer token. It needs
PyJWT + cryptography and four config fields: `weatherkit_team_id`,
`weatherkit_service_id`, `weatherkit_key_id`, `weatherkit_key_file` (path to the
`.p8` ECDSA P-256 private key). The factory skips the provider unless all four
are present and PyJWT imports (`__init__.py:241`).

`_make_jwt` (`weatherkit/__init__.py:78`) signs with **ES256** (ECDSA P-256 /
SHA-256):

- Claims: `iss = team_id`, `iat = now`, `exp = now + _JWT_LIFETIME`,
  `sub = service_id`. `_JWT_LIFETIME = 55*60` (55 min).
- Headers: `alg = ES256`, `kid = key_id`, `id = "{team_id}.{service_id}"`.
  `algorithm="ES256"` on `jwt.encode` is the load-bearing one (PyJWT derives the
  `alg` header from it); the duplicate in `headers=` is redundant-but-harmless
  and documented as such - do not remove the `algorithm=` kwarg.

Token caching and refresh (`_headers`, `weatherkit/__init__.py:108`): the token
is cached in `self._token` / `self._token_exp` and reused until 60s before
expiry, then re-minted. Each refresh **re-reads the `.p8` from disk** rather than
holding the key in memory. The rationale (`_read_private_key`, `:55`): CPython's
immutable `str` can't be reliably zeroed (interned/copied buffers in PyJWT and
cryptography outlive a `del`), so re-reading limits the key's in-memory
residency to the few ms of the signing call, at the cost of one ~1 KB read per
hour. After signing, the local `pk` reference is dropped in a `finally`.

Key-file hardening at init (`__init__.py:96`): the path is `resolve()`d,
`_check_key_perms` refuses to load it unless POSIX mode is `0o600` or `0o400`
(group/other must not read; skipped on Windows where ACLs apply), and
`_read_private_key` requires a recognized PEM header (PKCS#8 `BEGIN PRIVATE KEY`
or SEC1 `BEGIN EC PRIVATE KEY`). Both checks run at construction so a
misconfiguration fails loudly at startup, not at first lookup. The error never
includes file contents - even a wrong file's header line could leak info.

The request URL is `https://weatherkit.apple.com/api/v1/weather/en/{lat}/{lon}`
(4-decimal precision), with `Authorization: Bearer <token>`.

## 11. Provider reference (32 packages)

Each package is a directory with `__init__.py` defining the provider class
(`name`, `requires_key`, and the `get_*` methods) plus one file per endpoint.
Capabilities below are derived directly from the `async def get_*` methods
present in each package (the same `hasattr` discovery the dispatcher uses).
"Keyed" = `requires_key = True` (factory skips it without a credential).

The `name` attribute is what appears as the `[source]` tag in IRC output.

### Current / forecast / multi-capability providers

| id | name | key | capabilities | upstream API |
| --- | --- | --- | --- | --- |
| `nws` | NWS | keyless | current, forecast, hourly, alerts, marine | api.weather.gov (US National Weather Service) |
| `meteomatics` | Meteomatics | keyed (user/pass) | current, forecast, hourly | api.meteomatics.com |
| `weatherkit` | Apple Weather | keyed (JWT) | current, forecast, hourly, alerts | weatherkit.apple.com |
| `openmeteo` | Open-Meteo | keyless | current, forecast, hourly, air_quality, astronomy, historical, marine, nowcast, pollen, uv | *.open-meteo.com (api / air-quality / archive / marine) |
| `visualcrossing` | Visual Crossing | keyed | current, forecast, hourly, alerts, historical | weather.visualcrossing.com |
| `accuweather` | AccuWeather | keyed | current, forecast, hourly, alerts | dataservice.accuweather.com |
| `openweathermap` | OpenWeatherMap | keyed | current, forecast, hourly, alerts, air_quality | api.openweathermap.org |
| `weatherbit` | WeatherBit | keyed | current, forecast, hourly, alerts, air_quality, historical | api.weatherbit.io |
| `weatherapi` | WeatherAPI | keyed | current, forecast, hourly, alerts, astronomy, air_quality, historical | api.weatherapi.com |
| `pirateweather` | Pirate Weather | keyed | current, forecast, hourly, alerts, nowcast | api.pirateweather.net (Dark Sky-compatible) |
| `stormglass` | Stormglass | keyed | current, hourly, marine | api.stormglass.io (marine specialist) |
| `tomorrowio` | Tomorrow.io | keyed | current, forecast, hourly, alerts, air_quality | api.tomorrow.io |
| `worldweatheronline` | World Weather Online | keyed | current, forecast, hourly, astronomy, historical, marine | api.worldweatheronline.com |
| `weatherstack` | Weatherstack | keyed | current, forecast, historical | api.weatherstack.com |
| `metno` | MET Norway | keyless | current, forecast, hourly, alerts, nowcast | api.met.no (Yr) |

`weatherstack`: the endpoint modules pin `https://` and carry an explicit
comment that the base was once `http://` and leaked `access_key` in the
plaintext query string (`weatherstack/current.py:5`). Keep it HTTPS. The free
tier returns an `https_access_restricted` error code that the provider handles.

### Air-quality specialists

| id | name | key | capabilities | upstream API |
| --- | --- | --- | --- | --- |
| `airnow` | AirNow | keyed | air_quality | airnowapi.org (US EPA official AQI, US only) |
| `purpleair` | PurpleAir | keyed | air_quality | api.purpleair.com (crowdsourced PM2.5 sensors, global) |
| `waqi` | WAQI | keyed | air_quality | api.waqi.info (World Air Quality Index / aqicn) |
| `openaq` | OpenAQ | keyed | air_quality | api.openaq.org |
| `iqair` | IQAir | keyed | air_quality | api.airvisual.com (IQAir AirVisual) |

### Single-capability specialists

| id | name | key | capability | upstream API |
| --- | --- | --- | --- | --- |
| `sunrisesunset` | SunriseSunset | keyless | astronomy | api.sunrisesunset.io |
| `currentuvindex` | currentuvindex | keyless | uv | currentuvindex.com |
| `pollendotcom` | Pollen.com | keyless* | pollen | www.pollen.com (IQVIA, US) |
| `google_pollen` | Google Pollen | keyed | pollen | pollen.googleapis.com (global) |
| `gdacs` | GDACS | keyless | alerts | gdacs.org (Global Disaster Alert and Coordination System) |
| `eccc` | ECCC | keyless | alerts | api.weather.gc.ca (Environment Canada GeoMet) |
| `nasapower` | NASA POWER | keyless | historical | power.larc.nasa.gov |
| `nifc` | NIFC | keyless | wildfire | services3.arcgis.com / data-nifc.opendata.arcgis.com (US) |
| `firms` | NASA FIRMS | keyed | wildfire | firms.modaps.eosdis.nasa.gov (MODIS/VIIRS, global) |
| `swpc` | NOAA SWPC | keyless | space_weather | services.swpc.noaa.gov (Space Weather Prediction Center) |
| `noaa_coops` | NOAA CO-OPS | keyless | tides | api.tidesandcurrents.noaa.gov (US tide stations) |
| `tidecheck` | TideCheck | keyed | tides | tidecheck.com |

`pollendotcom` is keyless but takes the configured Nominatim User-Agent in its
constructor (`__init__.py:435` factory) because it reverse-geocodes lat/lon to a
US ZIP via Nominatim before hitting Pollen.com. With no contactable UA that
reverse-geocode step is degraded the same way `geocode.py` gates on it.

### Cross-reference: capability -> default chain

From `DEFAULT_RELIABILITY` (`_dispatch.py:75`), the static lead order per
capability (live order also factors health + reg order). Providers not listed in
a capability's rank map sort last at rank 99 if they support it.

- current/forecast: nws, meteomatics, weatherkit, openmeteo, visualcrossing,
  accuweather, openweathermap, weatherbit/weatherapi, pirateweather, tomorrowio,
  worldweatheronline, metno, weatherstack.
- hourly: nws, meteomatics, weatherkit, openmeteo, pirateweather, ... stormglass.
- alerts: nws, weatherkit, openweathermap, pirateweather, accuweather,
  weatherbit, visualcrossing, weatherapi, tomorrowio, gdacs, eccc, metno.
- air_quality: airnow, waqi, openaq, openmeteo, iqair, openweathermap,
  weatherbit, weatherapi, tomorrowio, accuweather, purpleair.
- astronomy: sunrisesunset, openmeteo, weatherapi, worldweatheronline.
- historical: openmeteo, visualcrossing, weatherbit, weatherapi,
  worldweatheronline, weatherstack, nasapower.
- marine: stormglass, nws, openmeteo, worldweatheronline.
- nowcast: pirateweather, meteomatics, openmeteo, metno. (Meteomatics ranks 2
  for nowcast but defines no `get_nowcast`, so it is not eligible - the rank
  table can list providers that don't implement the method; discovery is what
  gates eligibility, not the rank map.)
- uv: openmeteo, currentuvindex.
- pollen: google_pollen, pollendotcom, openmeteo.
- wildfire: nifc, firms.
- space_weather: swpc.
- tides: noaa_coops, tidecheck.

## 12. Adding a provider

The dispatcher discovers capabilities from method names, so a provider is
"registered" by existing, having a factory, and being named in
`provider_priority`. Nothing enumerates its capabilities by hand.

### 12.1 Package layout

One package per provider, one sub-module per endpoint:

```
weather_providers/myprovider/
    __init__.py      provider class; delegates to sub-modules
    current.py       -> get_weather
    forecast.py      -> get_forecast
    _codes.py        shared helpers (optional)
```

### 12.2 Endpoint sub-module

Use the shared capped client (`_http.get_json`) - never a bare `requests`/
`aiohttp` call. It enforces the body-size cap, timeout, redirect policy and
session reuse described in section 3.

```python
from .._http import get_json
from ..base import WeatherResult

_HEADERS = {"User-Agent": "Internets IRC Bot (https://github.com/brandontroidl/Internets)"}

async def fetch(api_key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(
        "https://api.myweather.com/current",
        params={"key": api_key, "lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
        headers=_HEADERS,
    )
    p = data.get("current", {})
    return WeatherResult(
        source="MyWeather",
        temperature=p.get("temp_c"),
        description=(p.get("condition") or ""),
        location=location,
        humidity=p.get("humidity"),
        wind_kph=p.get("wind_kph"),
    )
```

Import explicitly (`from ..base import WeatherResult`), not `import *` - the
star form was removed from this package deliberately.

Field rules that matter:

- **Populate what you measured; leave the rest `None`.** The formatter renders
  `None` as `N/A` and the dispatcher gap-fills it from the next provider
  (section 4.5). A fabricated value is worse than a missing one.
- **`description` empty string, not `"Unknown"`.** `""` counts as a gap and gets
  filled; `"Unknown"` is treated as a real value and blocks the fill.
- **`feels_like_c` / `dewpoint_c` only if the upstream gives them for this same
  observation.** They are excluded from gap-fill for the reason in section 4.5.
  Never compute one from another provider's numbers.
- **SI/metric in, always.** `base.py` has `ms_to_kph`, `km_to_m`, `deg_to_card`;
  the `modules/units.py` layer does the display conversion. Do not store
  Fahrenheit or mph.

### 12.3 Provider class

```python
from ..base import WeatherResult, ForecastDay   # explicit imports
from . import current, forecast


class MyProvider:
    name: str = "MyWeather"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)
```

- **`**kw` on every method.** The dispatcher forwards caller kwargs verbatim
  (`area=` for alerts, `days=`, `hours=`, `target_date=`). A method without
  `**kw` raises `TypeError` on an unrelated caller's kwarg and is logged as a
  provider bug (section 4.7).
- **Implement only the capabilities you support.** Omit the method entirely;
  `hasattr` discovery skips you. Do not stub one that returns `None` forever -
  that wastes a chain slot.
- **Return `None` for "outside my coverage", never raise** (sections 4.4, 4.9).
- Method names must match `CAPABILITY_METHODS` (`_dispatch.py`) exactly.

### 12.4 Factory + registration (`weather_providers/__init__.py`)

```python
def _f_myprovider(cfg):
    key = _cred(cfg, "myprovider_key", "myprovider_key")
    if not key:
        log.info("myprovider: skipped (no myprovider_key)")
        return None
    from .myprovider import MyProvider     # lazy: keeps import graph light
    return MyProvider(key)

_reg("myprovider", _f_myprovider)
```

`_cred(cfg, secret_name, ini_key)` (`__init__.py:65`) reads secret_store first,
then `config.ini` - the ini fallback exists so the bot works before
`python -m secret_store migrate`. Return `None` (do not raise) when unconfigured;
the registry simply skips you. A keyless provider omits the whole key block.

Import the provider class **inside** the factory, not at module top - a
top-level import pulls every provider's dependencies on startup.

### 12.5 Config, secrets, priority

1. Add the key to `KNOWN_SECRETS` (`secret_store.py`) so the CLI and the
   migration path know about it.
2. Add it to `config.ini.example` under `[weather_providers]` with a blank
   value and a comment naming the signup URL.
3. Add the id to `provider_priority` in `config.ini.example`. **Priority is an
   ordering, not an allowlist** - a provider absent from the list still loads
   and sorts last.
4. Add a short flag to `_PROVIDER_FLAGS` (`modules/weather.py`) if the provider
   is worth forcing manually. Check for collisions first (`-aw` is Apple, not
   AccuWeather).

### 12.6 Tests and docs before you open the PR

- A registry test asserting the factory is discovered and the capability set
  matches (see `tests/test_new_weather_capabilities.py` for the shape).
- A fetch test with a stubbed `get_json` covering a full payload, a sparse
  payload (missing secondary fields), and the no-coverage path.
- Add the provider to section 11 of this file and to the capability
  cross-reference table.

Run **both** suites (`python tests/run_tests.py` and `pytest tests/`) - they are
disjoint; see CONTRIBUTING.

## 13. Gotchas for the next maintainer

- **Adding a capability to a provider = adding the `get_*` method.** No
  registration list to update. But if it's a new capability entirely, add it to
  `CAPABILITY_METHODS` (`_dispatch.py:33`), the `WeatherProvider` Protocol
  docstring, a `DEFAULT_RELIABILITY` entry, a `base.py` result dataclass, a
  `get_*` facade in `__init__.py`, a `_format_*` + command in
  `modules/weather.py`, and a `__all__` export.
- **`force_provider` and an open breaker.** Forcing a provider whose breaker is
  open returns `None` (no fallback) - that's the caller's explicit choice, not a
  bug. It re-probes after the 60s cooldown.
- **A `None` return is not a failure.** It's "no data for this location" and
  does NOT count as a health success. Returning `None` fast forever keeps a
  provider looking healthy and high-ranked but useless. If a provider should
  surface an error, it must raise, not return `None`.
- **Quota is visibility, never a limiter.** Don't expect it to throttle. It also
  isn't auto-wired beyond the `record_call` inside `dispatch`.
- **Health state survives reload; provider registration does not.** `configure`
  rebuilds the dispatcher but the `health_registry` singleton keeps EMA and
  breaker state across reloads. A provider you just fixed may still be in
  cooldown.
- **`provider_priority` is not an allowlist.** Listing a subset does not disable
  the rest - everything unlisted appends and registers. To actually exclude a
  provider, remove its key (keyed) or its factory registration.
- **Never `r.json()` in a provider.** Route through `_http.get_json` (or another
  capped reader); the incremental size cap is a security control (SEC-WP-001),
  not a nicety.
- **Postal codes never use fuzzy free-text.** If a postal lookup is wrong, fix
  the classifier or the structured resolver in `geocode.py` - do not route it
  back through the `q=` word-drop loop, which is exactly what produced
  wrong-country matches.
- **All upstream strings are hostile.** OSM is user-editable and API text is
  third-party. Anything reaching an IRC line goes through `strip_ctrl` /
  `_sanitize` first.
</content>
</invoke>
