"""Provider health tracking with exponential moving average scoring.

Each provider maintains:
    - success_rate  : EMA of successful API calls (0.0–1.0)
    - avg_latency   : EMA of response times in seconds
    - rate_limited   : count of 429/rate-limit errors in current window
    - health_score  : composite score (0.0–1.0) used by the dispatcher

The dispatcher prefers providers with higher health_score values.
Scores recover over time — a temporary outage doesn't permanently
penalize a provider.
"""

from __future__ import annotations

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

# Rate-limit errors above this count get 0 points.
_RATELIMIT_CAP = 5

# Minimum calls before health score is considered meaningful.
_MIN_SAMPLES = 3


@dataclass
class ProviderHealth:
    """Tracks the health of a single weather provider."""
    provider_id: str
    success_rate: float = 1.0
    avg_latency: float = 0.5
    rate_limit_count: int = 0
    total_calls: int = 0
    total_failures: int = 0
    last_call: float = 0.0
    last_failure: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def health_score(self) -> float:
        """Composite score from 0.0 (dead) to 1.0 (perfect)."""
        if self.total_calls < _MIN_SAMPLES:
            return 0.90  # give new/untested providers a reasonable default

        success_component = self.success_rate

        # Latency: 1.0 at 0s, 0.0 at _LATENCY_CAP seconds.
        latency_component = max(0.0, 1.0 - (self.avg_latency / _LATENCY_CAP))

        # Rate limits: 1.0 at 0 errors, 0.0 at _RATELIMIT_CAP errors.
        rl_component = max(0.0, 1.0 - (self.rate_limit_count / _RATELIMIT_CAP))

        return (
            _W_SUCCESS * success_component
            + _W_LATENCY * latency_component
            + _W_RATELIMIT * rl_component
        )

    def record_success(self, latency: float) -> None:
        """Record a successful API call."""
        with self._lock:
            self.total_calls += 1
            self.last_call = time.time()
            self.success_rate = (1 - _ALPHA) * self.success_rate + _ALPHA * 1.0
            self.avg_latency = (1 - _ALPHA) * self.avg_latency + _ALPHA * latency
            # Decay rate-limit count on success.
            if self.rate_limit_count > 0:
                self.rate_limit_count = max(0, self.rate_limit_count - 1)

    def record_failure(self, rate_limited: bool = False) -> None:
        """Record a failed API call."""
        with self._lock:
            self.total_calls += 1
            self.total_failures += 1
            self.last_call = time.time()
            self.last_failure = time.time()
            self.success_rate = (1 - _ALPHA) * self.success_rate + _ALPHA * 0.0
            if rate_limited:
                self.rate_limit_count += 1

    def summary(self) -> str:
        """Human-readable health summary for status/debug output."""
        return (f"{self.provider_id}: score={self.health_score:.2f} "
                f"success={self.success_rate:.2f} "
                f"latency={self.avg_latency:.2f}s "
                f"calls={self.total_calls} "
                f"fails={self.total_failures} "
                f"rl={self.rate_limit_count}")


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
