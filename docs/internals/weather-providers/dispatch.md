# _dispatch.py - capability discovery, accuracy-then-health routing, fallback chain

## Purpose

The routing core. Discovers what each registered provider can do, orders the
candidates for a capability by static scientific-accuracy rank, then live
health, then registration order, and walks that chain under a time budget until
one provider returns usable data - with a current-conditions-only gap-fill pass
layered on top.

## Responsibilities / boundaries

Belongs here: `CAPABILITY_METHODS` (capability name -> provider method name),
`DEFAULT_RELIABILITY` (static rank tables), failure classification
(rate-limit vs auth vs provider-code bug), the chain/per-call time budgets, the
gap-fill accumulator, and log redaction. Not here: health-score math and the
circuit breaker (`_health.py` - this file only *consults*
`health.is_callable()` and calls `record_success` / `record_failure` /
`mark_auth_failure`), HTTP (`_http.py`), provider construction
(`__init__.py`), formatting (`modules/weather.py`).

## Dependencies and dependents

Dependencies: `_health` (`ProviderHealth`, `health_registry`), `_http`
(`HTTPError`), stdlib `asyncio` / `dataclasses` / `time`. At call time it
imports `record_call` from the parent package (late import, breaking the
`__init__` <-> `_dispatch` cycle).

Dependents: `__init__.py` (the `dispatcher` singleton and every `get_*`
wrapper), `modules/weather.py` (`capability_matrix`, `health_summary`,
`_sorted_for_capability` for the `-l` listing), `tests/test_dispatcher.py`
(despite the filename, that file is this module's test suite).

## Lifecycle

Imported with the package. A `Dispatcher` is constructed empty; providers are
registered by `configure()` at bot load/reload and cleared on each reconfigure.
No teardown.

## State

- `Dispatcher._providers: dict[provider_id, _RegisteredProvider]` - insertion
  order preserved (Python dict), which is what `reg_order` snapshots.
- `Dispatcher._next_order` - monotonic registration counter.
- Health state is *not* owned here: `_RegisteredProvider.health` is a reference
  into the process-global `health_registry`, so health survives
  `configure()`'s clear/re-register cycle across module reloads.

## Concurrency

`dispatch()` runs on the event loop and awaits provider calls sequentially -
the fallback chain is deliberately serial, not a racing fan-out (one upstream
request per attempt keeps quota use and rate-limit exposure minimal).
Registration is not locked; it is only mutated from `configure()`. Health
mutations go through `ProviderHealth`'s own lock. Two concurrent dispatches are
safe: they share only health state (locked) and the registry (read-only during
dispatch).

## Failure behavior

Per provider attempt, in order:

1. Circuit open (`health.is_callable()` False) -> skip without calling.
2. `asyncio.wait_for` timeout or any exception -> `record_failure(rate_limited=...)`,
   then: 401/403 `HTTPError` -> `mark_auth_failure()` (trips the breaker
   immediately, `TestAuthFailure.test_401_trips_breaker_and_falls_through`);
   `_BUG_EXC_TYPES` (TypeError, AttributeError, KeyError, IndexError,
   NameError, FrozenInstanceError) -> logged at ERROR as a provider code
   defect; everything -> one structured `dispatch_fail` WARNING line, then
   fall through.
3. `None` or `is_empty()` result -> fall through with *no* health record at
   all: a no-data answer must not reset the breaker or mask a brownout, and
   must not count as a failure either
   (`TestNoDataHandling.test_none_result_is_not_recorded_as_success`,
   `tests/test_new_weather_capabilities.py - TestNWSCoverage`).

Chain exhausted: return the partially gap-filled `primary` if one exists
(sparse beats nothing), else log "All providers failed" and return None. The
caller sees only `result | None`; no exception escapes `dispatch()`.

## Security

- `_redact()` scrubs `apikey|api_key|appid|key|token|secret|password=` query
  values from exception text before logging and truncates to 160 chars -
  defense in depth on top of `_http`'s own restraint.
- The failure log line deliberately omits args/kwargs (URL params can embed
  API keys).
- `force_provider` is attacker-influencable (IRC flag) but only ever used as a
  dict key against the registry; unknown ids fail closed with None.

## Classes

### `_RegisteredProvider`

Slots record binding a provider object to its id, registration order, its
`ProviderHealth` (fetched from the global registry, so it persists across
re-registration), and its discovered capability set. Discovery is
`hasattr(provider, method) and callable(...)` over `CAPABILITY_METHODS` - the
provider opts into a capability simply by defining the method
(`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`).

### `Dispatcher`

Registry + router. Constructor takes nothing; state described above.
Invariants: `_providers` keys equal the ids reported by `provider_ids`;
capability sets are computed once at registration (a provider that grows
methods later is invisible until re-registered - acceptable because providers
are static classes).

## Functions and methods

### Module constants

- `CAPABILITY_METHODS` - 14 capabilities mapping to `get_*` method names; the
  single source of truth for discovery, matched by the `WeatherProvider`
  protocol docstring in `base.py`.
- `DEFAULT_RELIABILITY` - per-capability `{provider_id: rank}`; lower is more
  accurate; unlisted providers rank 99. The 40-line comment above it records
  the per-capability ranking rationale (NWS/ECMWF-driven models over GFS
  derivatives, radar-blended nowcasts over pure model output, etc.).
  Extensively pinned by `tests/test_dispatcher.py - TestDefaultReliability`
  (every capability has a table, ranks unique and positive, NWS rank 1 for US
  capabilities, every *registered* capability ranked).
- `_RL_TOKEN_HINTS` - narrow substring set for rate-limit sniffing on
  non-HTTPError exceptions.
- `_BUG_EXC_TYPES` - exception types classified as provider code defects.
- `_CHAIN_BUDGET = 45.0` / `_PER_CALL_BUDGET = 30.0` - end-to-end fallback
  budget and single-call cap. The comment ties 45s to the 60s outer command
  timeout in `internets.py` (headroom for formatting + IRC send) and 30s to
  NWS's 2-3 sequential 10s hops.

### `_is_rate_limit_error(e)`

Structured first: `HTTPError.is_rate_limit` or `.status == 429`; then a
generic `.status` attribute (aiohttp raised directly by a provider that
bypassed `_http`); last resort substring sniff on message and type name.
Feeds the health tracker's rate-limit axis.

### `_redact(e, limit=160)`

One-line, truncated, key-redacted error string (see Security).

### `Dispatcher.register(provider, provider_id)` / `unregister` / `clear`

Registration wraps in `_RegisteredProvider`, assigns `reg_order`, logs the
capability set, returns it (used by `configure()`'s log line). `clear()` also
resets `_next_order` so a reconfigure reproduces stable ordering.

### `Dispatcher.provider_ids` / `capabilities()` / `capability_matrix()`

Read-only views. `capability_matrix()` renders each capability's chain in
dispatch order via `sort_chain` - shown by the admin `.providers` command and
logged at configure time.

### `Dispatcher.sort_chain(capability, provider_ids=None)`

The ordering policy. Sort key per provider:
`(reliability_rank, -health_score, reg_order)` - static accuracy dominates,
live health breaks ties among equally-ranked providers, registration order
(i.e. `provider_priority` from config) is the final tie-break. Evidence:
`TestAccuracySort` (lower rank beats higher, accuracy beats registration
order, unlisted providers sort last, 3-tuple shape).

### `Dispatcher._sorted_for_capability(...)`

Back-compat alias forwarding to `sort_chain`. Still the name
`modules/weather.py - WeatherModule._send_provider_list()` calls (see
Findings).

### `Dispatcher.dispatch(capability, *args, **kwargs)` - the core algorithm

1. Pop `force_provider` from kwargs (reserved, never forwarded).
2. Resolve `method_name`; unknown capability -> None.
3. Build `eligible` (registered providers with the capability); empty -> None.
4. Forced path: the named provider must be registered, support the
   capability, and have a closed/half-open breaker - otherwise return None
   with *no fallback* ("caller's explicit choice";
   `TestForceProvider.test_force_does_not_fall_back`). Chain = that single
   provider.
5. Otherwise chain = `_sorted_for_capability(capability, eligible)`.
6. `deadline = now + _CHAIN_BUDGET`, captured once so slow early providers
   shrink what later ones may spend.
7. Per provider: stop if the budget is exhausted; skip if the breaker is open;
   `record_call(pid)` (quota visibility, counts attempts including failures);
   `asyncio.wait_for(method(*args, **kwargs), timeout=min(remaining, 30))`.
8. Success handling: `None`/`is_empty()` -> continue (no health record).
   Usable -> `record_success(latency)`. For every capability except
   `current`, return immediately.
9. Gap-fill (current only): the first usable result becomes `primary`; while
   `primary.has_gaps()` and fewer than 3 contributing results have been
   merged, keep walking the chain and `primary = primary.fill_gaps(result)`
   with each further usable result; on completion return
   `primary.derive_missing()`. The 3-contributor cap counts the primary
   itself, so at most two fallback providers are consulted for fill. The
   `hasattr(result, "has_gaps")` guard keeps non-WeatherResult stubs (tests)
   and exotic results on the fast path. Evidence: `TestGapFill`
   (fills from next provider, complete primary never calls the filler, empty
   description filled, present description preserved, derived fields never
   imported).
10. Exception handling as described under Failure behavior.
11. Chain end: `primary.derive_missing()` if a sparse primary exists, else
    None.

Budget behavior is tested end to end:
`TestChainBudget.test_slow_provider_times_out_and_sheds` (a hang raises
TimeoutError, is recorded as failure, and the chain falls through to a healthy
provider).

### `Dispatcher.get_provider(provider_id)` / `health_summary()`

Convenience accessors; `health_summary()` delegates to
`health_registry.summary()`.

## Implementation walk

- Docstring + imports: the five-step architecture summary.
- `CAPABILITY_METHODS`, `DEFAULT_RELIABILITY`: routing policy tables (business
  logic; the rationale comment is normative for future rank edits).
- Failure-classification block (`_RL_TOKEN_HINTS`, `_BUG_EXC_TYPES`,
  `_is_rate_limit_error`, `_redact`): error handling and log hygiene.
- Budget constants: performance/resilience policy with rationale.
- `_RegisteredProvider`: capability discovery at construction.
- `Dispatcher` registry methods: state management.
- `sort_chain` + shim: ordering policy.
- `dispatch()`: the control-flow core walked above - selection, budgeting,
  breaker gating, quota, invocation, empty-result fall-through, gap-fill
  accumulation, failure classification.
- Accessors: trivial reads.

## Findings

- questionable | `_dispatch.py - DEFAULT_RELIABILITY["nowcast"]` | Ranks
  `meteomatics` at 2, but the meteomatics provider implements no `get_nowcast`
  (only current/forecast/hourly exist in
  `weather_providers/meteomatics/__init__.py`), so the entry is dead - either
  a planned endpoint never wired or a stale rank.
- questionable | `_dispatch.py - DEFAULT_RELIABILITY["current"]` | `stormglass`
  implements `get_weather` (`weather_providers/stormglass/__init__.py`) but is
  absent from the `current` table, silently sorting at rank 99 - exactly the
  failure shape `TestDefaultReliability.test_metno_ranked_for_its_capabilities`
  was added to catch for metno.
- test-gap | `tests/test_dispatcher.py -
  TestDefaultReliability.test_every_registered_capability_is_ranked` | Runs
  `configure(ConfigParser())`, which registers only keyless providers, so the
  completeness check never sees keyed providers - which is why the stormglass
  gap above survives it.
- questionable | `_dispatch.py - Dispatcher._sorted_for_capability()` | The
  shim's comment says "New code should call sort_chain directly", yet
  `modules/weather.py - _send_provider_list()` still calls the private name;
  the shim is load-bearing, not legacy.
- doc-drift | file naming | `tests/test_dispatcher.py` tests this module
  (`_dispatch.py`) plus package helpers from `__init__.py`; there is no
  `dispatcher.py`. Worth renaming or noting in the test header.
- questionable | `_dispatch.py - Dispatcher.dispatch()` forced path | A forced
  provider whose breaker is open returns None with a log the IRC user never
  sees ("try again shortly" appears only in the bot log); the user-visible
  reply is the generic capability-unavailable message from
  `modules/weather.py`.
