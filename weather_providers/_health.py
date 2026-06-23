"""Provider health tracking with exponential moving average scoring.

Each provider maintains:
    - success_rate  : EMA of successful API calls (0.0–1.0)
    - avg_latency   : EMA of response times in seconds
    - rate_limited  : count of 429/rate-limit errors, time-decayed
    - health_score  : composite score (0.0–1.0) used by the dispatcher

The dispatcher prefers providers with higher health_score values.
Scores recover over time — a temporary outage doesn't permanently
penalize a provider, and a rate-limit storm also fades on its own.
"""

from __future__ import annotations

import math
import time
import threading
import logging
from dataclasses import dataclass, field

log = logging.getLogger("internets.weather.health")

# EMA smoothing factor.  0.1 = slow adaptation (stable).
_ALPHA = 0.1

# Weights for composite health score.
_W_SUCCESS = 0.70
_W_LATENCY = 0.20
_W_RATELIMIT = 0.10

# Latency above this (seconds) gets 0 points in the latency component.
_LATENCY_CAP = 10.0
# Failures are penalised with a synthetic latency cost so a provider
# that keeps timing out gets dinged on the latency axis too — without
# this, a provider that returns 500s in 50ms looks "fast".
_FAILURE_LATENCY = _LATENCY_CAP  # i.e. zero latency-component points

# Rate-limit errors above this count get 0 points.
_RATELIMIT_CAP = 5

# Half-life for the rate-limit counter (seconds).  A 429-storm of 5
# decays to ~2.5 after 5 minutes, ~1.25 after 10, etc., so a provider
# isn't permanently locked out by a transient quota burst.
_RATELIMIT_HALFLIFE = 300.0

# Cold-start default — what new providers score before they have data.
_COLD_DEFAULT = 0.90

# Minimum calls before health score is fully "live" (i.e. ignores the
# cold-start default entirely).  Below this we interpolate.
_MIN_SAMPLES = 3

# ── Circuit-breaker defaults ───────────────────────────────────────────
# Layered on top of EMA scoring.  The breaker is a coarse, discrete
# guardrail: a provider with a transient EMA dip stays callable, but a
# provider that's burning failures in a tight loop gets shed entirely
# until a cooldown elapses, then probed once.
#
# State machine:
#   closed  — normal operation.  Failures accumulate in a rolling window.
#   open    — N consecutive failures within W seconds → open.  All calls
#             refused (health_score==0.0) for COOLDOWN seconds.
#   half_open — after cooldown, allow exactly one probe.  Success → closed;
#               failure → re-open.
#
# Tuning: defaults are conservative for the bot's typical "1 call per
# user request" pattern.  Tune via the dataclass constructor if needed.
_CB_THRESHOLD = 5        # consecutive failures to trip the breaker
_CB_WINDOW = 60.0        # seconds — failures must be within this span
_CB_COOLDOWN = 60.0      # seconds — refuse calls for this long after open

# Circuit-breaker states.
_CB_CLOSED = "closed"
_CB_OPEN = "open"
_CB_HALF_OPEN = "half_open"

# The dispatcher honours this gate: it calls
# ``health_registry.get(pid).is_callable()`` before invoking each provider
# and skips it when the breaker is open (see ``Dispatcher.dispatch``).


def format_health_score(score: float) -> str:
    """One-line formatter for a health score, used by status output."""
    return f"{score:.2f}"


@dataclass
class ProviderHealth:
    """Tracks the health of a single weather provider."""
    provider_id: str
    success_rate: float = 1.0
    avg_latency: float = 0.5
    rate_limit_count: float = 0.0       # float so time-decay can shrink it
    rate_limit_last_ts: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    last_call: float = 0.0
    last_failure: float = 0.0
    # ── circuit-breaker state ───────────────────────────────────────
    # See module-level constants for the state machine.  The breaker
    # is *additive* to the EMA health score — it doesn't replace the
    # score, it overrides ``health_score`` to 0.0 while ``open`` and
    # exposes ``is_callable()`` for the dispatcher to gate calls.
    cb_state: str = _CB_CLOSED
    cb_consecutive_failures: int = 0
    cb_first_failure_ts: float = 0.0    # start of the current failure window
    cb_opened_at: float = 0.0           # when we entered ``open`` state
    cb_threshold: int = _CB_THRESHOLD
    cb_window: float = _CB_WINDOW
    cb_cooldown: float = _CB_COOLDOWN
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── score components ────────────────────────────────────────────

    def _decayed_rate_limit(self, now: float | None = None) -> float:
        """Return the rate-limit counter after applying time-decay.

        Pure read — does *not* mutate state.  Mutation happens lazily
        on the next record_*() call (see ``_decay_rate_limit_locked``).
        """
        if self.rate_limit_count <= 0:
            return 0.0
        if self.rate_limit_last_ts <= 0:
            return self.rate_limit_count
        now = now if now is not None else time.time()
        elapsed = max(0.0, now - self.rate_limit_last_ts)
        # count * 0.5 ** (elapsed / halflife)
        factor = math.pow(0.5, elapsed / _RATELIMIT_HALFLIFE)
        return self.rate_limit_count * factor

    def _decay_rate_limit_locked(self, now: float) -> None:
        """Apply time-decay to the rate-limit counter.  Caller holds lock."""
        decayed = self._decayed_rate_limit(now)
        self.rate_limit_count = decayed if decayed > 0.01 else 0.0
        self.rate_limit_last_ts = now

    @property
    def health_score(self) -> float:
        """Composite score from 0.0 (dead) to 1.0 (perfect).

        For providers with fewer than ``_MIN_SAMPLES`` recorded calls
        we *interpolate* between the cold-start default and the live
        score, so a brand-new provider doesn't immediately outrank a
        100-call provider with a slightly lower live score.

        When the circuit breaker is ``open`` the score is force-pinned
        to 0.0 so any ranking-based dispatcher sees the provider as
        dead until the cooldown expires.
        """
        # Cheap, lock-free read.  A concurrent state transition may
        # produce a stale answer for one call — that's acceptable: the
        # breaker is a coarse guardrail, not a strict consensus.
        if self.cb_state == _CB_OPEN:
            # Auto-advance to half_open if cooldown has elapsed so
            # consumers polling health_score don't see a stuck zero.
            if (time.time() - self.cb_opened_at) < self.cb_cooldown:
                return 0.0
            # Cooldown expired — fall through and report the live
            # score.  The transition to half_open happens lazily in
            # is_callable() / record_*() under the lock.

        success_component = self.success_rate
        latency_component = max(0.0, 1.0 - (self.avg_latency / _LATENCY_CAP))
        rl_decayed = self._decayed_rate_limit()
        rl_component = max(0.0, 1.0 - (rl_decayed / _RATELIMIT_CAP))

        live_score = (
            _W_SUCCESS * success_component
            + _W_LATENCY * latency_component
            + _W_RATELIMIT * rl_component
        )

        if self.total_calls >= _MIN_SAMPLES:
            return live_score

        # Interpolate from cold default → live score across the warmup
        # window.  total_calls==0 → cold default; ==_MIN_SAMPLES → live.
        if _MIN_SAMPLES <= 0:
            return live_score
        t = self.total_calls / _MIN_SAMPLES
        return (1.0 - t) * _COLD_DEFAULT + t * live_score

    # ── circuit breaker ─────────────────────────────────────────────
    # The dispatcher calls ``is_callable()`` before invoking the provider
    # and skips it when the breaker is open (see ``Dispatcher.dispatch``).
    #
    # State transitions:
    #   closed → open:       cb_threshold consecutive failures within cb_window
    #   open → half_open:    cb_cooldown seconds elapsed since cb_opened_at
    #   half_open → closed:  probe call succeeded
    #   half_open → open:    probe call failed

    def is_callable(self) -> bool:
        """Return True if the dispatcher should attempt this provider.

        Side effect: transitions ``open`` → ``half_open`` when the
        cooldown has elapsed, releasing exactly one probe call.
        """
        with self._lock:
            now = time.time()
            if self.cb_state == _CB_OPEN:
                if (now - self.cb_opened_at) >= self.cb_cooldown:
                    # Cooldown elapsed — release a probe.
                    self.cb_state = _CB_HALF_OPEN
                    log.info("circuit_breaker[%s]: open → half_open "
                             "(cooldown elapsed, releasing probe)",
                             self.provider_id)
                    return True
                return False
            # closed or half_open: callable.  Note in half_open the
            # caller is expected to make exactly one probe; further
            # callers will still see ``True`` here — the breaker is
            # not a strict semaphore, it's a coarse guardrail.
            return True

    @property
    def circuit_state(self) -> str:
        """Public read of the current breaker state."""
        return self.cb_state

    def _cb_on_success_locked(self, now: float) -> None:
        """State machine transitions on success.  Caller holds lock."""
        if self.cb_state == _CB_HALF_OPEN:
            log.info("circuit_breaker[%s]: half_open → closed "
                     "(probe succeeded)", self.provider_id)
            self.cb_state = _CB_CLOSED
        # Any success clears the consecutive-failure tally.
        self.cb_consecutive_failures = 0
        self.cb_first_failure_ts = 0.0

    def _cb_on_failure_locked(self, now: float) -> None:
        """State machine transitions on failure.  Caller holds lock."""
        if self.cb_state == _CB_HALF_OPEN:
            log.warning("circuit_breaker[%s]: half_open → open "
                        "(probe failed)", self.provider_id)
            self.cb_state = _CB_OPEN
            self.cb_opened_at = now
            return
        if self.cb_state == _CB_OPEN:
            # Already open — just refresh the timestamp so a flood of
            # failures during cooldown doesn't accidentally let the
            # next probe through earlier than intended.  (No-op here:
            # we intentionally keep cb_opened_at fixed so the cooldown
            # window means what the operator configured.)
            return
        # closed → maybe open.
        if (self.cb_first_failure_ts == 0.0
                or (now - self.cb_first_failure_ts) > self.cb_window):
            # Start (or restart) the failure window.
            self.cb_first_failure_ts = now
            self.cb_consecutive_failures = 1
        else:
            self.cb_consecutive_failures += 1
        if self.cb_consecutive_failures >= self.cb_threshold:
            log.warning(
                "circuit_breaker[%s]: closed → open "
                "(%d failures in %.1fs, cooldown=%.0fs)",
                self.provider_id, self.cb_consecutive_failures,
                now - self.cb_first_failure_ts, self.cb_cooldown,
            )
            self.cb_state = _CB_OPEN
            self.cb_opened_at = now

    # ── mutators ────────────────────────────────────────────────────

    def record_success(self, latency: float) -> None:
        """Record a successful API call."""
        with self._lock:
            now = time.time()
            self.total_calls += 1
            self.last_call = now
            self.success_rate = (1 - _ALPHA) * self.success_rate + _ALPHA * 1.0
            self.avg_latency = (1 - _ALPHA) * self.avg_latency + _ALPHA * latency
            # Apply time-decay, plus a one-tick step-down on success so
            # a clean recovery actively shrinks the counter.
            self._decay_rate_limit_locked(now)
            if self.rate_limit_count > 0:
                self.rate_limit_count = max(0.0, self.rate_limit_count - 1.0)
            # Circuit breaker — success closes half_open, resets streak.
            self._cb_on_success_locked(now)

    def record_failure(self, rate_limited: bool = False) -> None:
        """Record a failed API call.

        Penalises the latency EMA too: a provider that fails (timeout,
        500, oversize body) is implicitly *slow* — without this it
        could keep a deceptively-low avg_latency and out-rank healthier
        peers on the latency axis.
        """
        with self._lock:
            now = time.time()
            self.total_calls += 1
            self.total_failures += 1
            self.last_call = now
            self.last_failure = now
            self.success_rate = (1 - _ALPHA) * self.success_rate + _ALPHA * 0.0
            self.avg_latency = (1 - _ALPHA) * self.avg_latency + _ALPHA * _FAILURE_LATENCY
            self._decay_rate_limit_locked(now)
            if rate_limited:
                self.rate_limit_count += 1.0
                self.rate_limit_last_ts = now
            # Circuit breaker — may trip closed → open or half_open → open.
            self._cb_on_failure_locked(now)

    def mark_auth_failure(self) -> None:
        """Trip the breaker immediately on an auth/permission error (401/403).

        A bad or unentitled API key fails deterministically on every call, so
        rather than retrying it on every request (tripping only after the
        normal consecutive-failure threshold), open the breaker now and log
        loudly.  The breaker still re-probes after its cooldown, so the
        provider recovers on its own once the key is fixed/reconfigured.
        """
        with self._lock:
            now = time.time()
            if self.cb_state != _CB_OPEN:
                log.error(
                    "provider[%s]: auth/permission error (401/403) — opening "
                    "circuit (check the API key/entitlement); re-probes after "
                    "%.0fs", self.provider_id, self.cb_cooldown)
            self.cb_state = _CB_OPEN
            self.cb_opened_at = now
            self.cb_consecutive_failures = self.cb_threshold

    def summary(self) -> str:
        """Human-readable health summary for status/debug output."""
        return (f"{self.provider_id}: score={format_health_score(self.health_score)} "
                f"success={self.success_rate:.2f} "
                f"latency={self.avg_latency:.2f}s "
                f"calls={self.total_calls} "
                f"fails={self.total_failures} "
                f"rl={self._decayed_rate_limit():.1f} "
                f"cb={self.cb_state}")


class HealthRegistry:
    """Thread-safe registry of provider health trackers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, ProviderHealth] = {}

    def get(self, provider_id: str) -> ProviderHealth:
        """Get or create the health tracker for a provider."""
        with self._lock:
            if provider_id not in self._providers:
                self._providers[provider_id] = ProviderHealth(provider_id=provider_id)
            return self._providers[provider_id]

    def all(self) -> list[ProviderHealth]:
        """Return a snapshot of all health trackers."""
        with self._lock:
            return list(self._providers.values())

    def summary(self) -> str:
        """Multi-line summary of all provider health for status output."""
        entries = self.all()
        if not entries:
            return "No providers tracked."
        entries.sort(key=lambda h: h.health_score, reverse=True)
        return "\n".join(h.summary() for h in entries)


# Global singleton — shared by all dispatcher calls.
health_registry = HealthRegistry()
