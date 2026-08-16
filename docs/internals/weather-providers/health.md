# _health.py - EMA health scoring and per-provider circuit breaker

## Purpose

Tracks each provider's live reliability so the dispatcher can prefer healthy
upstreams and shed broken ones. Two mechanisms layered: a continuous composite
health score (EMA success rate + EMA latency + time-decayed rate-limit count)
used as a sort tie-break, and a discrete circuit breaker (closed / open /
half_open) used as a hard call gate.

## Responsibilities / boundaries

Belongs here: score math, breaker state machine, the process-global
`HealthRegistry`, and human-readable summaries. Not here: deciding *when* to
record (the dispatcher chooses what counts as success/failure/no-data), quota
counting (`__init__.py`), and any persistence - all state is in-memory and
resets on restart.

## Dependencies and dependents

Dependencies: stdlib only (`math`, `time`, `threading`, `dataclasses`,
`logging`).

Dependents: `_dispatch.py` (`is_callable` gate, `record_success`,
`record_failure`, `mark_auth_failure`, `health_score` in the sort key),
`__init__.py` (`provider_status()`, `format_health_score` re-export),
`modules/weather.py` (`.providers` admin command via
`dispatcher.health_summary()`), tests.

## Lifecycle

Imported with the package. `health_registry` is a module-level singleton;
`ProviderHealth` entries are created lazily on first `get(provider_id)` and
never removed - they deliberately outlive `configure()`'s re-registration so a
module reload does not amnesty a failing provider.

## State

Per provider (`ProviderHealth` dataclass): `success_rate` (EMA, starts 1.0),
`avg_latency` (EMA seconds, starts 0.5), `rate_limit_count` (float, decays),
`rate_limit_last_ts`, `total_calls`, `total_failures`, `last_call`,
`last_failure`, and breaker fields `cb_state`, `cb_consecutive_failures`,
`cb_first_failure_ts`, `cb_opened_at`, plus per-instance tunables
`cb_threshold` (5), `cb_window` (60s), `cb_cooldown` (60s). All in-memory,
process-local, non-persistent.

## Concurrency

Every mutator (`record_success`, `record_failure`, `mark_auth_failure`,
`is_callable`) takes the per-provider `threading.Lock`, so mixed access from
the event loop and threads is consistent. Two deliberate lock-free reads:

- `health_score` reads state without the lock; a concurrent transition can
  yield one stale value. The comment declares this acceptable ("coarse
  guardrail, not a strict consensus").
- Half-open is not a strict semaphore: multiple concurrent callers of
  `is_callable()` during half_open all get True, so more than one probe can
  fly. Documented in the code; harmless at the bot's one-call-per-user-request
  volume.

`HealthRegistry` guards its dict with its own lock; `all()` returns a snapshot
list.

## Failure behavior

This module records failures rather than experiencing them. Nothing raises
under normal use; `_decay_rate_limit_locked` clamps to 0 below 0.01 to avoid
denormal drift. Recovery is built in on three axes: EMA drift back on
successes, rate-limit half-life decay (300s), and the breaker's cooldown
re-probe - no provider is permanently benched.

## Security

None: no I/O, no secrets, provider ids only in logs.

## Classes

### `ProviderHealth`

One tracker per provider id.

Scoring:

- `health_score` (property) = `0.70 * success_rate + 0.20 * latency_component
  + 0.10 * rl_component`, where `latency_component = max(0, 1 - avg_latency/10)`
  and `rl_component = max(0, 1 - decayed_rate_limit/5)`.
- Cold start: below `_MIN_SAMPLES` (3) calls the score interpolates linearly
  between `_COLD_DEFAULT` (0.90) and the live score, so a brand-new provider
  cannot outrank an established one at score 1.0, but also is not punished
  for having no history.
- Breaker override: while `open` and inside the cooldown the property returns
  0.0; once the cooldown has elapsed it falls through to the live score (the
  actual open -> half_open transition happens lazily, under the lock, in
  `is_callable()`), so pollers do not see a stuck zero.
- Failure latency penalty: `record_failure` feeds `_FAILURE_LATENCY` (10s, the
  cap) into the latency EMA so a provider that fails fast cannot look "fast" -
  the docstring names the exact deception this prevents.

Rate-limit axis: `record_failure(rate_limited=True)` increments
`rate_limit_count` (float); `_decayed_rate_limit()` applies
`count * 0.5 ** (elapsed / 300)` as a pure read; `_decay_rate_limit_locked()`
materializes the decay on each record; each *success* additionally steps the
counter down by 1.0 so clean recovery actively clears a 429 storm.

Circuit breaker state machine (constants `_CB_THRESHOLD=5`, `_CB_WINDOW=60`,
`_CB_COOLDOWN=60`):

```text
closed    --5 consecutive failures within 60s-->  open
open      --60s cooldown elapsed (via is_callable)-->  half_open (one probe)
half_open --probe success-->  closed
half_open --probe failure-->  open (cooldown restarts)
```

- `is_callable()` - the dispatcher's gate. True in closed/half_open; in open,
  flips to half_open and returns True once the cooldown has elapsed, else
  False.
- `_cb_on_success_locked` - half_open -> closed; any success zeroes the
  consecutive-failure tally and window start.
- `_cb_on_failure_locked` - half_open -> open (probe failed); already-open is
  a no-op (`cb_opened_at` intentionally fixed so the cooldown means what was
  configured); closed starts/extends the failure window and opens at the
  threshold. Note the window restart logic: a failure arriving more than
  `cb_window` after the window started *restarts* the window at count 1, so
  "5 consecutive failures" must also be temporally clustered.
- `mark_auth_failure()` - immediate closed/half_open -> open on a 401/403,
  bypassing the threshold, because a bad key fails deterministically on every
  call; logs at ERROR the first time. The breaker still re-probes after the
  cooldown, so fixing the key recovers the provider with no restart
  (`tests/test_dispatcher.py -
  TestAuthFailure.test_401_trips_breaker_and_falls_through` verifies both the
  trip and the not-called-again behavior).
- `circuit_state` - public read; `summary()` - one-line human summary
  including the decayed rate-limit value and breaker state.

How a provider gets benched and recovers, end to end: failures recorded by the
dispatcher accumulate in the 60s window; at 5 the breaker opens and
`health_score` pins to 0; for 60s `Dispatcher.dispatch` skips the provider
entirely ("circuit open" debug log); the next dispatch after the cooldown gets
one probe through `is_callable()`'s open -> half_open transition; a successful
probe closes the breaker and the EMAs drift back up with further successes; a
failed probe re-opens for another full cooldown. Independently of the breaker,
a merely *degraded* provider (EMA sagging but breaker closed) keeps its
accuracy rank in the sort and only loses ties - static accuracy dominates
health by design (`_dispatch.Dispatcher.sort_chain`).

### `HealthRegistry`

Thread-safe get-or-create keyed by provider id, plus `all()` snapshot and
`summary()` (entries sorted by score, best first). `health_registry` is the
global instance shared by every `Dispatcher` - including test-constructed
dispatchers, which is why the dispatcher tests use unique provider ids to
avoid cross-test contamination (noted in
`tests/test_dispatcher.py - TestAuthFailure`).

## Functions and methods

### `format_health_score(score)`

`f"{score:.2f}"`. Exists so status output formats identically everywhere
(re-exported by `__init__.py`).

Everything else is on the classes above.

## Implementation walk

- Docstring + constants: all tunables are named module constants with
  rationale comments (EMA alpha 0.1 "slow adaptation"; weights 70/20/10;
  latency cap 10s; rate-limit cap 5 and half-life 300s; cold default 0.90;
  warmup 3 samples; breaker 5-in-60s / 60s cooldown). Policy, not mechanism -
  the values are the operator-relevant part.
- `format_health_score`: formatting.
- `ProviderHealth` fields: state definition; the comment marks the breaker as
  additive to (not a replacement for) the EMA score.
- `_decayed_rate_limit` / `_decay_rate_limit_locked`: pure-read vs lazy
  materialization split - the read side must not mutate because
  `health_score` is lock-free.
- `health_score`: score composition, cold-start interpolation, open-state
  pinning with lazy fall-through. The `if _MIN_SAMPLES <= 0` guard is
  defensive dead code under the current constant (see Findings).
- Breaker methods: the state machine, each transition logged with direction
  and reason.
- `record_success` / `record_failure`: the only EMA writers; both bump
  counters, apply decay, and drive the breaker in one locked section.
- `mark_auth_failure`: the fast-trip path.
- `summary` / `HealthRegistry`: reporting and the singleton.

## Findings

- test-gap | `_health.py - ProviderHealth` breaker state machine | No direct
  unit tests for closed -> open threshold counting, the 60s window restart,
  open -> half_open cooldown release, or half_open -> open on probe failure;
  only the `mark_auth_failure` fast path is covered end to end
  (`tests/test_dispatcher.py - TestAuthFailure`) plus indirect coverage via
  the chain-budget and no-data tests. The window-restart arithmetic in
  `_cb_on_failure_locked` is exactly the kind of off-by-one logic a targeted
  test would pin.
- questionable | `_health.py - ProviderHealth.health_score` | The
  `if _MIN_SAMPLES <= 0: return live_score` branch is unreachable while
  `_MIN_SAMPLES` is the literal 3; harmless, but it reads as if the constant
  were configurable when nothing configures it.
- questionable | `_health.py - HealthRegistry` | Entries are never evicted;
  arbitrary ids passed to `get()` (e.g. a typoed `force_provider` would not
  reach here, but test ids do) accumulate for process lifetime. Bounded in
  practice by the fixed provider set; worth a comment at most.
