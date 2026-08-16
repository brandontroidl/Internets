# Weather provider architecture

Operator and integrator guide to the weather aggregation layer: what the
providers are, how one is selected for a request, what happens when it has no
answer, and how to turn providers on and off.

This is the architecture level. Line-level implementation notes live in
[internals/weather-providers/index](internals/weather-providers/index.md), and
one page per provider in
[internals/weather-providers/providers/index](internals/weather-providers/providers/index.md).
To add a provider, see [writing-providers](writing-providers.md).

Read the [dispatcher fallback defect](#known-defect-fallback-is-disabled-for-11-of-14-capabilities)
section before you reason about why a command returned no data. It changes the
behaviour most of this document would otherwise imply.

## The two layers

The weather system is split at a hard boundary.

`modules/weather.py` is the IRC surface: 15 command families, flag parsing,
location resolution through `modules/geocode.py`, and every formatter that turns
a result into an IRC line. It never performs HTTP and never names an upstream
API. Its whole contract with the layer below is the public `get_*` coroutines
plus a few dispatcher inspection calls.

`weather_providers/` is the aggregation layer: a registry of 32 provider
packages, capability discovery, accuracy-and-health ordering, fallback, health
and quota accounting, and one size-capped HTTP client.

```
modules/weather.py            command parsing, geocode, formatting
  |
  v
weather_providers/__init__.py public get_*() facade, factories, quota
  |
  v
_dispatch.Dispatcher          capability discovery, ordering, fallback
  |-> _health.ProviderHealth  EMA score plus circuit breaker (singleton)
  |-> <provider>/             one package per upstream, one file per endpoint
        |-> _http.get_json    capped async HTTP
  |
  v
base.py                       frozen normalized result dataclasses
```

A request is always `capability -> ordered provider chain -> first provider that
returns real data`. Providers never call each other and never see the chain;
ordering and fallback belong to the dispatcher alone.

## Capabilities

A capability is a named kind of weather data. The authoritative list is
`weather_providers/_dispatch.py - CAPABILITY_METHODS`, which maps each
capability name to the method a provider must define to support it. There are
14.

| Capability | Provider method | Result type |
| --- | --- | --- |
| `current` | `get_weather` | `WeatherResult` |
| `forecast` | `get_forecast` | `WeatherResult` |
| `hourly` | `get_hourly` | `HourlyResult` |
| `alerts` | `get_alerts` | `AlertsResult` |
| `air_quality` | `get_air_quality` | `AirQualityResult` |
| `astronomy` | `get_astronomy` | `AstronomyResult` |
| `historical` | `get_historical` | `HistoricalResult` |
| `marine` | `get_marine` | `MarineResult` |
| `nowcast` | `get_nowcast` | `NowcastResult` |
| `uv` | `get_uv` | `UVResult` |
| `pollen` | `get_pollen` | `PollenResult` |
| `wildfire` | `get_wildfire` | `WildfireResult` |
| `space_weather` | `get_space_weather` | `SpaceWeatherResult` |
| `tides` | `get_tides` | `TideResult` |

Thirteen result types cover fourteen capabilities because `current` and
`forecast` share `WeatherResult`: `get_weather` fills the scalar observation
fields, `get_forecast` fills the `forecast: list[ForecastDay]` field.

Each capability has one public coroutine in `weather_providers/__init__.py`
(`get_weather`, `get_forecast`, `get_hourly`, and so on). All of them take
`(lat, lon, location, **kw)`, accept `force_provider=<id>`, and return either a
result dataclass or `None` when the whole chain produced nothing.

## Registration and the configure() wiring

Registration happens in three steps, all in `weather_providers/__init__.py`.

1. A factory function per provider, `_f_<id>(cfg)`. It reads credentials, and
   returns either a constructed provider object or `None` when the credential
   is absent. Provider classes are imported *inside* the factory so an
   unconfigured provider's dependencies are never imported.
2. `_reg("<id>", _f_<id>)` records the factory in the module-level
   `_PROVIDER_FACTORIES` dict. There are 32 `_reg()` calls, matching the 32
   provider sub-packages on disk and the pinned
   `tests/test_dispatcher.py - test_factory_count_is_32`.
3. `configure(cfg)` runs every factory and registers whatever comes back.
   `modules/weather.py - WeatherModule.on_load()` calls it once per module load
   or reload.

`configure()` clears the dispatcher first, so a `.reload weather` rebuilds the
registry from scratch. It then determines an order:

- `[weather_providers] provider_priority` (or the legacy `priority` key), a
  comma-separated list of provider ids.
- Every registered id *not* named in that list is appended after it.

That append is the important part. `provider_priority` is an **ordering
preference and final dispatch tie-breaker, not an allowlist**. A config file
written before a provider existed still loads that provider; it simply sorts
last. There is no way to disable a provider by omitting it from the list. To
actually exclude one, remove its credential (keyed providers) or its `_reg()`
call (keyless providers).

If every factory declines, `configure()` registers Open-Meteo unconditionally
and logs a warning, so the bot is never left with an empty registry.

The shipped `config.ini.example` `provider_priority` line names 30 of the 32
ids (`pollendotcom` and `google_pollen` are absent) under a comment claiming to
list all 32. Because the list is not an allowlist, both pollen providers still
register; the omission is cosmetic drift in the example file, not a functional
gap.

## Capability discovery

Nothing enumerates a provider's capabilities by hand. When
`Dispatcher.register()` builds a `_RegisteredProvider`, it walks
`CAPABILITY_METHODS` and adds a capability to the provider's set when
`hasattr(provider, method)` is true and the attribute is callable
(`_dispatch.py - _RegisteredProvider.__init__`).

Consequences worth internalizing:

- A provider supports a capability if and only if the method exists. Adding a
  capability to an existing provider means adding one `async def get_*` method.
- A method that exists but always returns `None` is worse than no method: it
  occupies a slot in the chain, costs a dispatch attempt and a quota tick, and
  contributes nothing.
- `_dispatch.py - DEFAULT_RELIABILITY` can name providers that do not implement
  the capability. Those entries are inert (discovery gates eligibility, not the
  rank table). Two exist today: `air_quality/accuweather` and
  `nowcast/meteomatics`, both verified as ranked-but-unimplemented.
- The inverse case is not inert and is a live hazard. See
  [Reliability ranking](#reliability-ranking).

`Dispatcher.capabilities()` returns the capability-to-provider map and
`Dispatcher.capability_matrix()` renders the live chains; `.providers` (admin
only) prints both alongside health.

## Normalized results and derived fields

`weather_providers/base.py` defines one frozen, slotted dataclass per result
type. Frozen plus slots is deliberate: a provider that mutates a result or sets
an unknown field raises `FrozenInstanceError` or `AttributeError`, which the
dispatcher classifies as a provider-code bug and logs at ERROR rather than
silently treating as an upstream outage (`_dispatch.py - _BUG_EXC_TYPES`).

Unit conventions are metric and SI throughout: Celsius, km/h, millibars, metres,
millimetres, micrograms per cubic metre. Display conversion happens in
`modules/units.py`, never in a provider. `base.py` supplies the shared
converters `deg_to_card`, `ms_to_kph`, `km_to_m`, the great-circle helper
`haversine_km`, and the category mappers `aqi_category` (US EPA), `uv_category`
(WHO), `kp_category` (NOAA G-scale), `pollen_cat_12`, `pollen_cat_5`.

`PollenResult` normalizes three incompatible upstream models into one struct and
the formatter renders whichever group a provider populated: Open-Meteo/CAMS
per-species concentrations in grains/m3, Google's 0-5 Universal Pollen Index,
and Pollen.com/IQVIA's single 0-12 index with category and trigger names.

### Derived fields

Two fields on `WeatherResult` are computed rather than reported when the
upstream omits them: `feels_like_c` and `dewpoint_c`.
`WeatherResult.derive_missing()` fills them from *this result's own*
temperature, humidity and wind, using the Rothfusz heat index above 27C at 40%+
relative humidity, the Environment Canada wind chill at or below 10C with wind
over 4.8 km/h, and the Magnus formula (Alduchov and Eskridge 1996 coefficients)
for dewpoint.

Both are deliberately excluded from the cross-provider gap-fill set. See
[The single-source rule](#the-single-source-rule).

## Dispatch and fallback semantics

`Dispatcher.dispatch(capability, *args, **kwargs)` is the whole of the routing
logic (`weather_providers/_dispatch.py`).

1. Resolve the capability to a method name; unknown capability returns `None`.
2. Build the eligible set: every registered provider whose discovered
   capability set contains this capability. Empty set returns `None`.
3. If `force_provider` was passed, the chain is exactly that provider. It fails
   with `None` (no fallback) when the provider is unregistered, lacks the
   capability, or has an open circuit. Suppressing fallback is the caller's
   explicit choice, not a defect.
4. Otherwise sort the eligible set into a chain (see
   [Reliability ranking](#reliability-ranking)).
5. Walk the chain. For each provider: check the whole-chain deadline, skip it
   when its breaker is open, increment its quota counter, then call the method
   under `asyncio.wait_for`.

### Time budget

The bot caps a command at 60 seconds (`internets.py`). Inside that, the
dispatcher enforces two budgets so one slow upstream cannot starve the healthy
providers queued behind it:

- `_CHAIN_BUDGET = 45.0` seconds for the entire chain, measured from a deadline
  captured before the first call. When it is exhausted the loop stops and logs
  `dispatch_budget_exhausted`.
- `_PER_CALL_BUDGET = 30.0` seconds for any single provider call, further capped
  at whatever chain time remains. A hang raises `asyncio.TimeoutError`, which is
  handled as a failure, so a brownout provider trips its own breaker instead of
  quietly eating the budget.

Both nest above the HTTP transport timeout (`_http.py - _TIMEOUT`, 10 seconds
per request). A provider that makes several sequential hops, such as NWS or
AccuWeather, can consume 10 seconds per hop at the transport layer.

### Outcome classification

| Provider outcome | Health effect | Chain effect |
| --- | --- | --- |
| Returns a result with data | success recorded, latency EMA updated | chain ends, result returned |
| Returns `None` | nothing recorded | falls through to next provider |
| Returns a result where `is_empty()` is true | nothing recorded | falls through to next provider |
| Raises | failure recorded, latency penalized | falls through to next provider |
| Raises `HTTPError` 401/403 | failure plus immediate breaker trip | falls through to next provider |
| Times out | failure recorded | falls through to next provider |

The "nothing recorded" rows are deliberate. A provider that has no data for a
location is neither healthy nor broken; recording a success there would let a
provider that returns nothing quickly stay top-ranked forever, and recording a
failure would trip the breaker on a provider that is working correctly outside
its coverage area.

That is also the reason for the **no-data contract**: a provider outside its
coverage returns `None`, it does not raise. Raising records a health failure and
five of them inside 60 seconds opens the breaker. Verified compliant:
`nws/` (returns `None` outside CONUS scope), `openmeteo/pollen.py`,
`pollendotcom/pollen.py`, `google_pollen/pollen.py`. Verified non-compliant:
`worldweatheronline/` (`astronomy.py`, `marine.py`, `historical.py` all
`raise ValueError` on no data) and `airnow/air_quality.py` (raises `HTTPError`
for a location outside US AQI coverage). Both punish themselves in the health
score for correct behaviour.

The gate that decides "no usable data, try the next one" is a single expression
at `_dispatch.py:417`, and it is the subject of the next section.

## Known defect: fallback is disabled for 11 of 14 capabilities

**Verified by direct enumeration against the source. Do not read the fallback
semantics above as working for the capabilities named here.**

The dispatcher decides whether a returned result counts as an answer with:

```python
if result is None or (hasattr(result, "is_empty") and result.is_empty()):
    continue
```

`_dispatch.py:417`. The `hasattr` guard means a result type that does not define
`is_empty()` can never be judged empty. Only two of the 13 result dataclasses
define it:

- Implements `is_empty()`: `WeatherResult` (no temperature and no forecast
  days), `HourlyResult` (no hour entries).
- Does **not** implement it: `AlertsResult`, `AirQualityResult`,
  `AstronomyResult`, `HistoricalResult`, `MarineResult`, `NowcastResult`,
  `UVResult`, `PollenResult`, `WildfireResult`, `SpaceWeatherResult`,
  `TideResult`.

For the 11 capabilities backed by those types, **a hollow result counts as
success**. It records a health success, ends the chain, and returns to the
formatter, which renders a row of `N/A`. No lower-ranked provider is ever
tried, and nothing is logged as a failure.

Every provider is individually correct here. The defect lives in the interaction
between the provider contract and the dispatcher guard, which is why per-file
review kept surfacing it as a series of unrelated provider bugs.

### Confirmed user-visible consequences

| Capability | Shadowing provider | Suppressed fallback |
| --- | --- | --- |
| `alerts` | `tomorrowio` returns an empty `AlertsResult` on 401/403 (free tier) | `nws`, `gdacs`, `eccc` never queried |
| `marine` | `nws` returns an all-`None` `MarineResult` | `openmeteo` wave data never queried |
| `air_quality` | `openweathermap` returns a hollow result on an empty list | lower-ranked AQ providers never queried |
| `air_quality` | `openaq` station without PM2.5 yields `aqi=None`, prints "AQI N/A" | `openmeteo`, `iqair` never queried |
| `astronomy` | `openmeteo` populates no moon fields | `weatherapi` (rank 3) never queried |
| `wildfire` | `nifc` returns empty outside US coverage | `firms` never queried |
| `pollen` | `pollendotcom` non-coercible index, prints "No pollen data" | `openmeteo` never queried |
| `astronomy` | `sunrisesunset` HTTP 200 with an empty results object | chain ends on an empty answer |

The `alerts` row is safety relevant. With a free-tier Tomorrow.io key
configured, a severe weather warning that NWS is publishing can be silently not
shown, because Tomorrow.io's 401 became an empty-but-successful
`AlertsResult`. This is the failure mode to check first when a user reports
that `.alerts` said nothing during an active warning.

### Operator mitigations available today

- Do not configure a Tomorrow.io key unless the tier actually serves alerts. It
  ranks above `gdacs`/`eccc` (rank 9 versus 10 and 11) and below `nws` (rank
  1), so the `alerts` exposure is to the non-US and global-hazard providers
  behind it. Removing the key drops Tomorrow.io from the registry entirely.
- Force a known-good provider per query when a result looks hollow: `.al -nws`,
  `.sea -om`, `.fire -firms`. `force_provider` bypasses the ranking and returns
  that provider's answer directly.
- Watch for the shape in the log: a chain that ends after one provider with no
  `dispatch_fail` line and an all-`N/A` reply is this defect, not an outage.

The codebase already recognizes the pattern in one place:
`tests/test_provider_fixes.py` pins a Tomorrow.io air-quality fix that raises
instead of returning empty. The remedy was applied to one provider rather than
to the contract.

Fix shape, **owner decision, not applied**: give every result dataclass an
`is_empty()`, or invert the dispatcher guard so a result must positively signal
that it carries data. Either is a behaviour change on a live weather path and is
out of scope for documentation. Tracked in `RECONSTRUCTION-LEDGER.md`.

## Reliability ranking

The chain order comes from `Dispatcher.sort_chain(capability, provider_ids)`,
which sorts on a three-element key:

1. **Static reliability rank** from `_dispatch.py - DEFAULT_RELIABILITY`, a
   per-capability map of provider id to integer rank, lower being better.
   Unlisted providers get **99**. This is the dominant key: the ranking encodes
   the scientific quality of the underlying model or observation network, which
   is what a user actually wants from "weather".
2. **Health score**, descending. Among providers of comparable rank, prefer the
   one currently up and fast.
3. **Registration order**, ascending, which is `provider_priority` from
   `config.ini`.

The ranking rationale is recorded as a comment block above the table: government
and ECMWF-driven models lead current and forecast; high-resolution rapid-refresh
models lead hourly; the NWS CAP/IPAWS feed is authoritative for US alerts; CAMS
leads air quality; ERA5 reanalysis leads historical; radar-blended products lead
nowcast; astronomy is deterministic ephemeris so it is ranked by field
completeness.

### Changing the ranking

Edit `DEFAULT_RELIABILITY` in `weather_providers/_dispatch.py`. Two invariants
are enforced by tests and must hold:

- Ranks are positive integers
  (`tests/test_dispatcher.py - test_ranks_are_positive_ints`).
- Ranks are unique within a capability
  (`test_ranks_are_unique_per_capability`). A duplicate would silently demote
  one of the two providers, since the health tie-break would decide an ordering
  the author intended to be explicit.

There is no configuration knob for the ranking. `provider_priority` only moves
the third sort key, so it can reorder providers that share a rank but cannot
promote a rank-14 provider above a rank-1 one. That is intentional: accuracy
ordering is a code decision, not an operator decision.

### Known defect: unranked providers sort at 99 silently

A provider that implements a capability but is missing from that capability's
rank table gets rank 99 and sorts to the back of the chain with no warning, no
log line, and no test failure.

One live instance, verified: **`stormglass` implements `get_weather` (the
`current` capability) but does not appear in `DEFAULT_RELIABILITY["current"]`**.
With a Stormglass key configured, it is last in the `current` chain regardless
of health.

`tests/test_dispatcher.py - test_every_registered_capability_is_ranked` exists
precisely to catch this, and does not, because it calls `configure()` with an
empty `ConfigParser`. Every keyed provider declines to register without a
credential, so keyed providers are never in the set the test iterates. The test
covers keyless providers only.

When adding or extending a provider, check the rank table by hand. See
[writing-providers](writing-providers.md#reliability-ranking-entry).

## Health tracking and the circuit breaker

`weather_providers/_health.py` keeps one `ProviderHealth` per provider id in a
process-global `health_registry`. The registry is a singleton and is **not**
cleared by `configure()`, so EMA scores and breaker state survive a module
reload. A provider you just fixed may still be inside its cooldown.

### Composite score

`ProviderHealth.health_score` is a weighted blend, 0.0 to 1.0:

| Component | Weight | Definition |
| --- | --- | --- |
| Success rate | 0.70 | EMA of success/failure, smoothing 0.1 |
| Latency | 0.20 | `1 - avg_latency / 10s`, floored at 0 |
| Rate limiting | 0.10 | `1 - decayed_429_count / 5`, floored at 0 |

Details that matter operationally:

- A failure also charges the latency EMA a synthetic 10 seconds, so a provider
  returning HTTP 500 in 50 ms cannot look "fast" and outrank a healthy peer.
- The rate-limit counter decays with a 300 second half-life, and a success
  additionally steps it down by 1. A 429 storm fades on its own rather than
  locking a provider out.
- Below `_MIN_SAMPLES = 3` recorded calls the score interpolates from a
  cold-start default of 0.90 toward the live score, so a brand-new provider does
  not instantly outrank a proven one on thin evidence.

### Circuit breaker

The breaker is a coarse discrete guard layered on top of the score. States and
transitions:

| Transition | Trigger |
| --- | --- |
| `closed` to `open` | 5 consecutive failures within a 60 second window |
| `closed` to `open` | any 401/403, immediately, via `mark_auth_failure()` |
| `open` to `half_open` | 60 second cooldown elapsed, on the next `is_callable()` |
| `half_open` to `closed` | the probe call succeeded |
| `half_open` to `open` | the probe call failed |

Thresholds are `_CB_THRESHOLD = 5`, `_CB_WINDOW = 60.0`, `_CB_COOLDOWN = 60.0`
seconds, all `ProviderHealth` constructor fields rather than config keys.

While open, `health_score` is pinned to 0.0 and `is_callable()` returns `False`,
so `Dispatcher.dispatch()` skips the provider entirely without spending latency
on it. The 401/403 fast trip exists so a revoked or unentitled API key costs one
request rather than one request per dispatch; it logs at ERROR naming the
provider, and the breaker still re-probes after the cooldown so the provider
recovers on its own once the key is fixed.

Two upstream behaviours defeat the fast trip, both verified: `waqi` and `iqair`
signal a bad key inside an HTTP 200 body, so no `HTTPError` is raised, the
breaker never trips, and a dead credential burns one request per dispatch
indefinitely. `weatherstack`'s invalid-key envelope similarly yields
`status=None`.

`dispatcher.health_summary()` renders one line per tracked provider with score,
success rate, latency, call and failure counts, decayed rate-limit count, and
breaker state. `.providers` prints it.

## Quota accounting

`weather_providers/__init__.py` keeps a per-provider daily call counter in the
module-level `quota` dict, guarded by `_quota_lock`, rolling over at UTC
midnight. `quota_status(pid)` returns `used`, `limit`, `remaining` and `pct`.
`provider_status()` embeds it per provider.

`Dispatcher.dispatch()` calls `record_call(pid)` once per **attempt**, before
the provider method runs. Two consequences:

- The counter is visibility only. Nothing throttles or refuses a call on the
  basis of quota. An operator near a ceiling must act on it manually.
- It counts dispatch attempts, not HTTP requests. A provider that makes several
  sequential upstream hops per call (NWS, AccuWeather) under-counts its real
  consumption. The `record_call` docstring still says the dispatcher does not
  call it automatically; that is stale, `dispatch()` does.

Limits in `_DEFAULT_QUOTA_LIMITS` are best-effort markers, and several do not
mean what the field name implies. Verified mismatches: `weatherapi` and
`weatherstack` store a **monthly** cap in the per-day field; `pirateweather`
likewise; `airnow`'s 500 is a per-**hour** upstream limit stored per day, so it
understates usage by roughly 24x. Treat a limit as a rough marker, not a
contract, and confirm the real ceiling with the vendor before relying on it.

Providers with no published cap (`nws`, `openmeteo`, `meteomatics`,
`weatherkit`, `purpleair`) carry `None`, which `quota_status()` reports as
unlimited with `pct = 0.0`.

## Source attribution

Every result dataclass carries a `source: str` set by the provider, and every
formatter appends it to the IRC line as `[<source>]`. The value is the
human-readable provider name, not the dispatcher id: `[NWS]`, `[Open-Meteo]`,
`[Apple Weather]`.

When the dispatcher gap-fills a `current` result from more than one provider,
`WeatherResult.fill_gaps()` rewrites the source to name both, producing
`[NWS + Open-Meteo]`. Attribution is never dropped, and a merged answer is never
presented as coming from one source. Up to three contributors can appear.

Attribution is also a licence obligation for some upstreams. `currentuvindex`
serves CC-BY data and the credit in the source tag is what satisfies it.

## The single-source rule

**One reading comes from one observation.** Multi-provider mean or median
blending of readings was proposed on 2026-07-22 and rejected. Providers are not
independent samples of a single truth: they disagree mostly about *which*
location, elevation and grid cell they measured, not about measurement noise.
Averaging them manufactures a number no instrument produced, for a location that
does not exist.

The rule has exactly one exception, and it is narrow.

### The current-only gap-fill exception

For the `current` capability only, the dispatcher fills fields that are
**missing** from the primary result using the next usable provider in the chain
(`_dispatch.py - Dispatcher.dispatch()`, `base.py -
WeatherResult.has_gaps/fill_gaps`). The fillable set is
`base.py - _CURRENT_GAP_FIELDS`:

`humidity`, `wind_kph`, `wind_dir`, `pressure_mb`, `visibility_m`,
`description`.

Constraints on the exception:

- Only missing fields are filled. A present value is never overwritten, so no
  reading is ever replaced by another provider's.
- `temperature` and `forecast` are never touched.
- `description` is filled only when empty (NWS station observations often null
  `textDescription`).
- Bounded to 3 contributing providers and to the chain deadline.
- Both sources are credited in the source tag.
- It applies to no other capability. Every other result is whole, from one
  provider.

### Why derived fields are excluded

`feels_like_c` and `dewpoint_c` are deliberately absent from
`_CURRENT_GAP_FIELDS`. Both are functions of an observation's own temperature,
humidity and wind, so importing one from a provider that measured a different
temperature yields a line that contradicts itself.

This was observed live, and the incident is recorded in the comment above
`_CURRENT_GAP_FIELDS`. At Yosemite, NWS reported 24.2C from a station at 2900 m
with no feels-like value, and Open-Meteo's model grid reported 13.8C with a
feels-like of 11.9C computed against *its* temperature. The bot printed
"Temperature 24.2C :: Feels like 11.3C" at 44% humidity and 6.6 mph wind, which
no apparent-temperature formula produces. The same query for San Dimas erred the
other way.

After gap-filling, `WeatherResult.derive_missing()` computes both fields from
the primary observation's own temperature plus whatever humidity and wind the
chain supplied. The inputs may be borrowed; the derivation is always anchored to
the temperature actually being displayed.

## Enabling and disabling providers

A provider is active when its factory returns an object. That is the entire
mechanism.

**Keyless providers are always active**: `nws`, `openmeteo`, `metno`, `gdacs`,
`eccc`, `nasapower`, `nifc`, `swpc`, `noaa_coops`, `sunrisesunset`,
`currentuvindex`, `pollendotcom`. Every one of the 14 capabilities has at least
one keyless provider, so a bot with zero credentials configured still answers
every weather command. There is no way to disable a keyless provider from
configuration; it requires removing its `_reg()` call.

**Keyed providers activate when their credential resolves.** `_cred()` reads the
secret store first and falls back to `config.ini [weather_providers]`:

1. Environment variable `INTERNETS_<NAME>` (uppercased secret name).
2. `[secrets]` in `config.ini`, mode 0600.
3. `[weather_providers] <ini_key>` in `config.ini`, the pre-migration fallback.

Adding a key and reloading the module activates the provider; blanking it and
reloading deactivates it. Nothing else is needed. `weatherkit` is the one
multi-part case: it needs all four of team id, service id, key id and key file,
plus `PyJWT`, and logs how many of the four are missing without naming them.

`meteomatics` takes a username and password rather than a key.

The secret name and the `[weather_providers]` ini key are identical for every
weather provider. Note that WAQI's is `waqi_token`, not `waqi_key`. Full list in
`secret_store.py - KNOWN_SECRETS` and `secret_store.py - CONFIG_LOCATIONS`;
`python -m secret_store list` shows which are set and from where.

`pollendotcom` is keyless but is constructed with the configured
`weather_user_agent`, because it reverse-geocodes lat/lon to a US ZIP through
Nominatim first. Note that the factory reads it from
`[weather_providers] weather_user_agent` while `modules/weather.py` reads
`[weather] user_agent`; the two ini locations diverge. In practice
`pollendotcom` fails open with a hardcoded non-identifying User-Agent, whereas
`modules/weather.py` fails closed and disables geocoding entirely without a
contactable UA.

### Checking what is active

- `.providers` (admin) prints health plus the live capability chains.
- `.<cmd> -l` lists the providers eligible for that command's capability.
- `provider_status()` returns a machine-readable snapshot of every *known*
  provider, active or not, with `state` in `unconfigured`, `cold`, `failing`,
  `active`, plus call counts, success rate, health score and quota.
- The startup log carries `Provider chain: ...` and the full capability matrix
  at INFO.

## Provider capability matrix

All 32 registered providers, generated by reading the `_reg()` factories in
`weather_providers/__init__.py` and the `get_*` methods each provider class
defines. This is the same `hasattr` discovery the dispatcher performs, so the
table is the eligibility set, not an aspiration.

`Key` is the class attribute `requires_key`. `Name` is the class attribute
`name`, which is what appears in the `[source]` tag.

### General forecast providers

| id | Name / key | Capabilities |
| --- | --- | --- |
| `nws` | NWS, keyless | current, forecast, hourly, alerts, marine |
| `meteomatics` | Meteomatics, keyed | current, forecast, hourly |
| `weatherkit` | Apple Weather, keyed | current, forecast, hourly, alerts |
| `openmeteo` | Open-Meteo, keyless | current, forecast, hourly, air_quality, astronomy, historical, marine, nowcast, uv, pollen |
| `visualcrossing` | Visual Crossing, keyed | current, forecast, hourly, alerts, historical |
| `accuweather` | AccuWeather, keyed | current, forecast, hourly, alerts |
| `openweathermap` | OpenWeatherMap, keyed | current, forecast, hourly, alerts, air_quality |
| `weatherbit` | WeatherBit, keyed | current, forecast, hourly, alerts, air_quality, historical |
| `weatherapi` | WeatherAPI, keyed | current, forecast, hourly, alerts, air_quality, astronomy, historical |
| `pirateweather` | Pirate Weather, keyed | current, forecast, hourly, alerts, nowcast |
| `stormglass` | Stormglass, keyed | current, hourly, marine |
| `tomorrowio` | Tomorrow.io, keyed | current, forecast, hourly, alerts, air_quality |
| `worldweatheronline` | World Weather Online, keyed | current, forecast, hourly, astronomy, historical, marine |
| `weatherstack` | Weatherstack, keyed | current, forecast, historical |
| `metno` | MET Norway, keyless | current, forecast, hourly, alerts, nowcast |

### Specialists

| id | Name / key | Capabilities |
| --- | --- | --- |
| `airnow` | AirNow, keyed | air_quality |
| `purpleair` | PurpleAir, keyed | air_quality |
| `waqi` | WAQI, keyed | air_quality |
| `openaq` | OpenAQ, keyed | air_quality |
| `iqair` | IQAir, keyed | air_quality |
| `sunrisesunset` | SunriseSunset, keyless | astronomy |
| `currentuvindex` | currentuvindex, keyless | uv |
| `pollendotcom` | Pollen.com, keyless | pollen |
| `google_pollen` | Google Pollen, keyed | pollen |
| `gdacs` | GDACS, keyless | alerts |
| `eccc` | ECCC, keyless | alerts |
| `nasapower` | NASA POWER, keyless | historical |
| `nifc` | NIFC, keyless | wildfire |
| `firms` | NASA FIRMS, keyed | wildfire |
| `swpc` | NOAA SWPC, keyless | space_weather |
| `noaa_coops` | NOAA CO-OPS, keyless | tides |
| `tidecheck` | TideCheck, keyed | tides |

Counts derived from the table: 32 providers, 12 keyless and 20 keyed.

### Static chain order per capability

Ranked order from `DEFAULT_RELIABILITY`. Live order also factors health and
registration order, and only providers that are actually registered appear.

| Capability | Ranked order |
| --- | --- |
| `current` | nws, meteomatics, weatherkit, openmeteo, visualcrossing, accuweather, openweathermap, weatherapi, weatherbit, pirateweather, tomorrowio, worldweatheronline, metno, weatherstack, then **stormglass at 99** |
| `forecast` | nws, meteomatics, weatherkit, openmeteo, visualcrossing, accuweather, openweathermap, weatherbit, weatherapi, pirateweather, tomorrowio, worldweatheronline, metno, weatherstack |
| `hourly` | nws, meteomatics, weatherkit, openmeteo, pirateweather, visualcrossing, openweathermap, weatherbit, weatherapi, tomorrowio, accuweather, worldweatheronline, metno, stormglass |
| `alerts` | nws, weatherkit, openweathermap, pirateweather, accuweather, weatherbit, visualcrossing, weatherapi, tomorrowio, gdacs, eccc, metno |
| `air_quality` | airnow, waqi, openaq, openmeteo, iqair, openweathermap, weatherbit, weatherapi, tomorrowio, purpleair (accuweather ranked but unimplemented) |
| `astronomy` | sunrisesunset, openmeteo, weatherapi, worldweatheronline |
| `historical` | openmeteo, visualcrossing, weatherbit, weatherapi, worldweatheronline, weatherstack, nasapower |
| `marine` | stormglass, nws, openmeteo, worldweatheronline |
| `nowcast` | pirateweather, openmeteo, metno (meteomatics ranked but unimplemented) |
| `uv` | openmeteo, currentuvindex |
| `pollen` | google_pollen, pollendotcom, openmeteo |
| `wildfire` | nifc, firms |
| `space_weather` | swpc |
| `tides` | noaa_coops, tidecheck |

## Per-provider detail

Upstream endpoints, response shapes, per-provider quirks and known defects are
documented one page per provider under
[internals/weather-providers/providers/index](internals/weather-providers/providers/index.md).
The framework files have their own pages:
[init](internals/weather-providers/init.md),
[base](internals/weather-providers/base.md),
[dispatch](internals/weather-providers/dispatch.md),
[health](internals/weather-providers/health.md),
[http](internals/weather-providers/http.md).

The IRC command surface, flag table and formatters are documented at
[internals/modules/weather](internals/modules/weather.md).

## Related documents

- [writing-providers](writing-providers.md) - implementing a new provider.
- [design-decisions ADR-010](design-decisions.md#adr-010-single-source-weather-rule) -
  the recorded decision behind the single-source rule.
- [configuration](configuration.md) - config file and secret store reference.
- [troubleshooting](troubleshooting.md) - diagnosing a failing command.
- [modules](modules.md) - the module system the IRC layer sits in.
