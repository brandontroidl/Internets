"""Prometheus text-format metrics registry and optional HTTP exporter.

Disabled by default — instantiating the module-level ``registry`` only
costs a couple of dicts.  To start the exporter, *someone* must call
``registry.enable()`` (no-op if already enabled) and then
``registry.expose(host, port)``.  Until then this module imposes zero
network footprint.

# TODO(internets.py): from metrics import registry; registry.enable(...)

Design choices:

  * Pure stdlib.  No prometheus_client dependency.
  * Label sets are normalized to a sorted tuple of ``(name, value)``
    pairs so identical label dicts (regardless of insertion order)
    coalesce onto the same time-series.
  * Renderer emits canonical Prometheus exposition format: HELP, TYPE,
    then one line per labeled sample.
  * HTTP exporter binds **127.0.0.1 only** by default.  Binding to
    0.0.0.0 is rejected explicitly — this is an internal endpoint and
    must not be exposed off-host without an explicit reverse proxy.
"""

from __future__ import annotations

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Iterable

log = logging.getLogger("internets.metrics")

# Prometheus label name rule: [a-zA-Z_][a-zA-Z0-9_]*
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# Metric name rule: [a-zA-Z_:][a-zA-Z0-9_:]*
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def _escape_label_value(v: str) -> str:
    """Escape per Prometheus exposition rules: backslash, dquote, newline."""
    return (str(v)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n"))


def _normalize_labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Canonicalize a label dict into a sorted tuple of (name, value) pairs."""
    if not labels:
        return ()
    out: list[tuple[str, str]] = []
    for k, v in labels.items():
        if not _LABEL_NAME_RE.match(k):
            raise ValueError(f"invalid label name: {k!r}")
        out.append((k, str(v)))
    out.sort(key=lambda kv: kv[0])
    return tuple(out)


def _format_labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs)
    return "{" + inner + "}"


class _Metric:
    """Common storage for counters and gauges."""

    __slots__ = ("name", "help_text", "kind", "_samples", "_lock")

    def __init__(self, name: str, help_text: str, kind: str) -> None:
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        self.name = name
        self.help_text = help_text
        self.kind = kind  # "counter" | "gauge"
        self._samples: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def _set(self, value: float,
             labels: dict[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._lock:
            self._samples[key] = float(value)

    def _inc(self, amount: float,
             labels: dict[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + float(amount)

    def samples(self) -> list[tuple[tuple[tuple[str, str], ...], float]]:
        with self._lock:
            return list(self._samples.items())


class Counter:
    """Monotonically increasing metric."""

    def __init__(self, m: _Metric) -> None:
        self._m = m

    def inc(self, amount: float = 1.0,
            labels: dict[str, str] | None = None) -> None:
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        self._m._inc(amount, labels)


class Gauge:
    """Arbitrary up-and-down value."""

    def __init__(self, m: _Metric) -> None:
        self._m = m

    def set(self, value: float,
            labels: dict[str, str] | None = None) -> None:
        self._m._set(value, labels)

    def inc(self, amount: float = 1.0,
            labels: dict[str, str] | None = None) -> None:
        self._m._inc(amount, labels)

    def dec(self, amount: float = 1.0,
            labels: dict[str, str] | None = None) -> None:
        self._m._inc(-amount, labels)


class MetricsRegistry:
    """Holds counters / gauges and renders them in Prometheus text format.

    Construct once at module load; call ``enable()`` from
    ``internets.py`` startup to opt in; then ``expose(host, port)`` to
    start the HTTP server.  If never enabled, the registry still
    accepts increments but never starts a network listener.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._enabled: bool = False
        self._server: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._register_defaults()

    # ── lifecycle gate ─────────────────────────────────────────────

    def enable(self) -> None:
        """Mark the registry as enabled.  Idempotent.  Required before ``expose()``."""
        with self._lock:
            self._enabled = True

    def is_enabled(self) -> bool:
        return self._enabled

    # ── registration helpers ───────────────────────────────────────

    def _make_metric(self, name: str, help_text: str, kind: str) -> _Metric:
        with self._lock:
            if name in self._metrics:
                m = self._metrics[name]
                if m.kind != kind:
                    raise ValueError(
                        f"metric {name!r} already registered as {m.kind}")
                return m
            m = _Metric(name, help_text, kind)
            self._metrics[name] = m
            return m

    def counter(self, name: str, help_text: str) -> Counter:
        m = self._make_metric(name, help_text, "counter")
        c = self._counters.get(name)
        if c is None:
            c = Counter(m)
            self._counters[name] = c
        return c

    def gauge(self, name: str, help_text: str) -> Gauge:
        m = self._make_metric(name, help_text, "gauge")
        g = self._gauges.get(name)
        if g is None:
            g = Gauge(m)
            self._gauges[name] = g
        return g

    # ── accessors for wired-in metrics ─────────────────────────────

    def _register_defaults(self) -> None:
        """Pre-register the canonical metric names so callers can simply
        do ``registry.commands_total.inc(labels={...})`` without
        worrying about registration races."""
        # Counters
        self.commands_total = self.counter(
            "internets_commands_total",
            "Total bot commands dispatched, labeled by module and command.")
        self.provider_calls_total = self.counter(
            "internets_provider_calls_total",
            "Total weather provider calls, labeled by provider and outcome.")
        self.provider_quota_used = self.counter(
            "internets_provider_quota_used",
            "Estimated provider quota consumed since process start.")
        self.reconnects_total = self.counter(
            "internets_reconnects_total",
            "Number of IRC reconnect attempts since process start.")
        self.dropped_messages_total = self.counter(
            "internets_dropped_messages_total",
            "Outbound messages dropped (queue full, oversized, etc).")
        self.audit_records_total = self.counter(
            "internets_audit_records_total",
            "Records appended to the audit log since process start.")
        # Gauges
        self.module_loaded = self.gauge(
            "internets_module_loaded",
            "1 if the named module is currently loaded, else 0.")
        self.provider_active = self.gauge(
            "internets_provider_active",
            "1 if the named provider is in the labeled state, else 0.")
        self.sender_queue_depth = self.gauge(
            "internets_sender_queue_depth",
            "Current depth of the outbound sender queue.")
        self.authed_admins_count = self.gauge(
            "internets_authed_admins_count",
            "Number of currently authenticated admin sessions.")

    # ── rendering ──────────────────────────────────────────────────

    def render(self) -> str:
        """Return the registry contents in Prometheus text exposition format."""
        out: list[str] = []
        # Iterate in stable name order so diffs are readable.
        with self._lock:
            names = sorted(self._metrics)
        for name in names:
            m = self._metrics[name]
            out.append(f"# HELP {m.name} {m.help_text}")
            out.append(f"# TYPE {m.name} {m.kind}")
            samples = m.samples()
            if not samples:
                # Emit a zero sample with no labels so scrapers see the series.
                out.append(f"{m.name} 0")
                continue
            # Stable per-metric ordering.
            samples.sort(key=lambda s: s[0])
            for label_pairs, value in samples:
                label_str = _format_labels(label_pairs)
                out.append(f"{m.name}{label_str} {_format_value(value)}")
        return "\n".join(out) + "\n"

    # ── HTTP exporter ──────────────────────────────────────────────

    def expose(self, host: str = "127.0.0.1", port: int = 9779) -> None:
        """Start the HTTP exporter on a background daemon thread.

        Refuses to start unless ``enable()`` was called.  Refuses to
        bind to ``0.0.0.0`` or ``::``.  Idempotent: a second call with
        a server already running is a no-op.
        """
        if not self._enabled:
            log.info("metrics.expose: refused — registry not enabled "
                     "(call registry.enable() first)")
            return
        # Defensive REFUSAL of all-interfaces binds — these literals appear
        # as a guard, never as a target.  Bandit B104 grep-matches the
        # strings regardless of context (false positive); suppress it here.
        if host in ("0.0.0.0", "::", ""):  # nosec B104
            raise ValueError(
                f"refusing to bind metrics endpoint to {host!r} — "
                "this endpoint must remain loopback-only")
        with self._lock:
            if self._server is not None:
                log.debug("metrics.expose: already running")
                return
            registry = self
            class _Handler(BaseHTTPRequestHandler):
                # Quiet the default access log — uses stderr otherwise.
                def log_message(self, format: str, *args: object) -> None:
                    log.debug("metrics http: " + format, *args)

                def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
                    if self.path.split("?", 1)[0] != "/metrics":
                        self.send_response(404)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b"not found\n")
                        return
                    body = registry.render().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            try:
                server = HTTPServer((host, port), _Handler)
            except OSError as e:
                log.error("metrics.expose: bind failed on %s:%d (%s)",
                          host, port, type(e).__name__)
                raise
            t = threading.Thread(
                target=server.serve_forever,
                name="metrics-http",
                daemon=True,
            )
            t.start()
            self._server = server
            self._server_thread = t
            log.info("metrics: exporter listening on http://%s:%d/metrics",
                     host, port)

    def shutdown(self) -> None:
        """Stop the HTTP exporter if running.  Safe to call multiple times."""
        with self._lock:
            srv = self._server
            self._server = None
            t = self._server_thread
            self._server_thread = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as e:
                log.debug("metrics.shutdown: %s", type(e).__name__)
        if t is not None:
            t.join(timeout=2.0)


def _format_value(v: float) -> str:
    """Render numbers compactly but Prometheus-compatible."""
    # Integers as plain digits, floats with enough precision.
    if isinstance(v, bool):
        # Defensive — bool is an int subclass; render as 0/1.
        return "1" if v else "0"
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v))


# Module-level singleton.  Nothing networked until enable() + expose().
registry = MetricsRegistry()


def enabled_metrics() -> Iterable[str]:
    """Return registered metric names — handy for tests / introspection."""
    with registry._lock:  # noqa: SLF001 (intentional test/inspection accessor)
        return list(registry._metrics)
