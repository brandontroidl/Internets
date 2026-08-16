# metrics.py - Prometheus text-format registry and loopback HTTP exporter

## Purpose

Provides the bot's operational metrics: a hand-rolled, pure-stdlib registry of
counters and gauges rendered in Prometheus text exposition format (version 0.0.4),
plus an optional HTTP exporter serving `GET /metrics`. There is no
`prometheus_client` dependency; the module states this as a design choice and the
imports confirm it (only `ipaddress`, `logging`, `re`, `threading`, `http.server`,
`typing`).

The module is inert by default. Importing it builds the module-level singleton
`registry` (dicts and locks only). Network exposure requires two explicit steps:
`registry.enable()` and then `registry.expose(host, port)`, both driven from
`internets.py - _main()` only when `config.ini` has `[metrics] enable = true`.

## Responsibilities / boundaries

Belongs here:

- Metric storage (`_Metric`), typed wrappers (`Counter`, `Gauge`), label
  normalization and escaping, exposition-format rendering.
- The HTTP exporter lifecycle (`expose()` / `shutdown()`) and its bind-address
  guard.

Deliberately not here:

- Deciding when metrics are on: `internets.py - _main()` reads the `[metrics]`
  config section and calls `enable()` / `expose()`.
- Incrementing metrics: call sites live in `internets.py`, `sender.py`, and
  `audit_log.py` (see the metric table below).
- Note that `internets.py - IRCBot` also keeps a private `self._metrics` dict of
  plain ints (reconnects, dropped_messages, command_timeouts, ...) used for the
  shutdown summary log line. That dict is unrelated to this module despite the
  name collision.

## Dependencies and dependents

- External: stdlib only.
- Dependents (all import lazily, inside `try/except`):
  - `internets.py - _dispatch()` increments `commands_total`.
  - `internets.py` connection loop increments `reconnects_total` on reconnect.
  - `internets.py - _main()` enables and exposes; the shutdown sequence calls
    `registry.shutdown()`.
  - `sender.py - _bump_dropped()` increments `dropped_messages_total`.
  - `audit_log.py` append path increments `audit_records_total`.
  - `tests/test_metrics.py` imports everything, including private helpers.

## Lifecycle

1. Import: `registry = MetricsRegistry()` at module bottom. The constructor
   pre-registers the ten default metrics via `_register_defaults()`.
2. Startup: `internets.py - _main()` checks `cfg["metrics"].getboolean("enable",
   False)`; if true, calls `enable()` then `expose(host, port)` with
   `host` defaulting to `127.0.0.1` and `port` to `9779`. Any exception is caught
   and logged (`event=metrics_start_failed`); the bot continues without metrics.
3. Runtime: call sites increment counters; the exporter thread serves scrapes.
4. Shutdown: bot shutdown step 7b calls `registry.shutdown()` if
   `is_enabled()`, wrapped in a broad `except Exception: pass`.

If never enabled, increments are still accepted and stored; only the network
listener is gated.

## Metrics exposed

All are pre-registered by `MetricsRegistry._register_defaults()` so call sites can
use attribute access (`registry.commands_total.inc(...)`) without registration
races. Labels are supplied per call, not declared at registration.

| Name | Type | Labels (at call site) | Meaning (HELP text) | Update point |
|---|---|---|---|---|
| `internets_commands_total` | counter | `module`, `command` | Bot commands dispatched | `internets.py - _dispatch()`; `module` label is the owning module or `"core"` |
| `internets_provider_calls_total` | counter | (intended: provider, outcome) | Weather provider calls | **None - never updated** (see Findings) |
| `internets_provider_quota_used` | counter | - | Estimated provider quota used | **None - never updated** |
| `internets_reconnects_total` | counter | none | IRC reconnect attempts | `internets.py` connection loop, alongside the private `self._metrics["reconnects"]` bump |
| `internets_dropped_messages_total` | counter | none | Outbound messages dropped | `sender.py - _bump_dropped()` |
| `internets_audit_records_total` | counter | none | Audit log records appended | `audit_log.py` append path, after a successful write |
| `internets_module_loaded` | gauge | (intended: module name) | 1 if module loaded | **None - never updated** |
| `internets_provider_active` | gauge | (intended: provider, state) | Provider state flag | **None - never updated** |
| `internets_sender_queue_depth` | gauge | - | Outbound sender queue depth | **None - never updated** |
| `internets_authed_admins_count` | gauge | - | Authenticated admin sessions | **None - never updated** |

A metric with no samples renders as a single unlabeled `name 0` line
(`render()`), so the six unwired metrics appear as constant zeros to scrapers.

## The exporter

- `expose(host="127.0.0.1", port=9779)` starts a stdlib `http.server.HTTPServer`
  on a daemon thread named `metrics-http`.
- The nested `_Handler` serves only `GET /metrics` (query string is stripped
  before comparison, so `/metrics?x=y` works); every other path returns 404 with
  body `not found\n`. Responses carry
  `Content-Type: text/plain; version=0.0.4; charset=utf-8` and Content-Length.
  The default per-request stderr access log is redirected to
  `log.debug`.
- `HTTPServer` is the plain single-threaded server, not `ThreadingHTTPServer`:
  one scrape is handled at a time and a stalled client blocks the next scrape
  (see Findings).

### Bind address restrictions (verified)

`expose()` refuses to start in three cases:

1. `enable()` not called: logs and returns (no exception).
2. All-interfaces bind: the host string is stripped, parsed with
   `ipaddress.ip_address()`, IPv4-mapped IPv6 addresses are unwrapped, and the
   call raises `ValueError` if the result `is_unspecified` or the stripped host
   is empty. This catches `0.0.0.0`, `::`, `::0`, `::ffff:0.0.0.0`, `""`, and
   whitespace-padded forms (`tests/test_metrics.py - TestExposeGuard.test_rejects_all_interfaces`
   parametrizes exactly these).
3. Bind failure: `OSError` from `HTTPServer()` is logged and re-raised.

Precisely: the guard rejects only *unspecified* addresses. A specific
non-loopback IP (`192.168.1.5`) or a hostname passes the guard; the inline
comment says loopback is allowed "for the documented reverse-proxy front-end"
and only unspecified is rejected, but the error message and docstring say the
endpoint "must remain loopback-only", which overstates what is enforced (see
Findings). The rationale for the restriction is that the endpoint has no
authentication; `internets.py - _main()` repeats the warning in its config
comment ("never expose to 0.0.0.0 - auth-less").

The `# nosec B104` marker suppresses a Bandit false positive: the `0.0.0.0`
literal appears only as a rejected value, never as a bind target.

Idempotence: a second `expose()` while a server is running is a logged no-op
(`tests/test_metrics.py - TestExposeGuard.test_expose_idempotent`).

## State

- `MetricsRegistry._metrics`: name to `_Metric` map; entries are added, never
  removed.
- `_Metric._samples`: normalized-label-tuple to float map. All state is
  in-memory and process-lifetime; nothing is persisted, so all counters reset on
  restart.
- `_server` / `_server_thread`: exporter lifecycle handles.

## Concurrency

- Each `_Metric` has its own `threading.Lock` guarding `_samples` for `_set`,
  `_inc`, and the `samples()` snapshot.
- `MetricsRegistry._lock` guards metric registration, the enable flag, and
  server start/stop. `render()` snapshots the name list under the lock, then
  reads each `_Metric` via its own lock; since metrics are never removed, the
  post-snapshot dict reads are safe.
- The exporter runs on a daemon thread; `shutdown()` swaps the handles out under
  the lock, then calls `srv.shutdown()` and joins the thread with a 2 s timeout
  outside the lock.
- `counter()` / `gauge()` read and write the `_counters` / `_gauges` wrapper
  caches outside any lock; two racing first-callers could each build a wrapper
  for the same `_Metric` (harmless: both share the same storage, but the
  same-instance guarantee the tests assert holds only single-threaded).

## Failure behavior

The "metrics must never crash the bot" property is enforced at the call sites,
not centrally, and it holds at every current call site (verified):

- `internets.py - _dispatch()`, the reconnect path, and the shutdown path wrap
  the import and increment in `try/except Exception: pass`.
- `sender.py - _bump_dropped()` and the `audit_log.py` increment do the same,
  each with a comment stating the intent.
- `internets.py - _main()` wraps `enable()`/`expose()` and degrades to a log
  line on any exception, including the `ValueError`/`OSError` that `expose()`
  raises.

Inside the module, `Counter.inc()` raises `ValueError` on negative amounts, and
`_normalize_labels()` / `_Metric.__init__()` raise `ValueError` on invalid label
or metric names; these surface programming errors to the (wrapped) caller
rather than storing bad series.

## Security

- Trust boundary: the HTTP endpoint is unauthenticated; the unspecified-address
  guard plus the loopback default are the entire access control. Off-host access
  is expected to go through an operator-provided reverse proxy.
- Label values are escaped per exposition rules (`_escape_label_value()`:
  backslash, double quote, newline), so attacker-influenced label values (e.g. a
  command name) cannot break the line format. Label *names* are allowlisted by
  regex. Metric names likewise.
- `help_text` is not escaped in `render()`; all current help strings are static
  literals (see Findings for the latent risk via the public registration API).

## Classes

### `_Metric`

Shared storage for one named time-series family: name, help text, kind
(`"counter"` or `"gauge"`), a samples dict keyed by normalized label tuples, and
a lock. Validates the metric name on construction. `__slots__` keeps instances
small. Not exported conceptually, but imported directly by tests.

### `Counter`

Thin wrapper over a `_Metric` exposing only `inc(amount=1.0, labels=None)` and
rejecting negative increments, preserving counter monotonicity per series.

### `Gauge`

Wrapper exposing `set()`, `inc()`, `dec()` (implemented as `inc(-amount)`).
Values may go negative (`tests/test_metrics.py - TestGauge.test_dec_below_zero`).

### `MetricsRegistry`

Owns the metric map, wrapper caches, the enabled flag, and the exporter.
Constructed once at module load as `registry`; tests construct private
instances to avoid mutating the singleton. `_make_metric()` is
get-or-create and raises `ValueError` on a kind conflict for an existing name.
`_register_defaults()` binds the ten canonical metrics as instance attributes.

## Functions and methods

| Symbol | Notes |
|---|---|
| `_escape_label_value(v)` | Backslash first, then quote, then newline, so escapes are not re-escaped; coerces non-strings via `str()` |
| `_normalize_labels(labels)` | Validates names against `_LABEL_NAME_RE`, coerces values to `str`, sorts by name, returns a tuple; identical dicts in any insertion order coalesce onto one series |
| `_format_labels(pairs)` | `{}`-wrapped comma-joined `k="v"` rendering; empty tuple renders as empty string |
| `_format_value(v)` | bools as `1`/`0` (bool is an int subclass), integral floats as plain digits, other floats via `repr` |
| `MetricsRegistry.enable()` / `is_enabled()` | Sets/reads the flag; `enable()` takes the lock, `is_enabled()` does not (benign flag read) |
| `MetricsRegistry.counter()` / `gauge()` | Public registration API; idempotent per name |
| `MetricsRegistry.render()` | Sorted metric names; per metric: HELP line, TYPE line, then samples sorted by label tuple, or a single `name 0` when empty; ends with a newline |
| `MetricsRegistry.expose()` | See "The exporter" above |
| `MetricsRegistry.shutdown()` | Idempotent; exceptions from `srv.shutdown()`/`server_close()` are swallowed at debug level; joins the thread with a 2 s cap so shutdown cannot hang |
| `enabled_metrics()` | Test/introspection helper returning registered names; reaches into `registry._lock`/`_metrics` with an acknowledging `noqa` |

## Implementation walk

- Lines 1-38: module docstring (design choices), imports, logger, and the two
  Prometheus name regexes (label names, metric names with `:` allowed).
- Lines 41-66: pure formatting helpers (escape, normalize, format).
- Lines 69-129: `_Metric` storage plus the `Counter`/`Gauge` wrappers.
- Lines 132-229: `MetricsRegistry` construction, enable gate, registration
  helpers, and `_register_defaults()` with the ten canonical metrics.
- Lines 232-252: `render()`.
- Lines 256-341: `expose()` (guard, nested handler, server + thread start) and
  `shutdown()`.
- Lines 344-362: `_format_value()`, the `registry` singleton, and
  `enabled_metrics()`.

## Operational use

- Turn on via `config.ini` (or `config.local.ini`):
  `[metrics]` with `enable = true`, optional `host` (default `127.0.0.1`) and
  `port` (default `9779`).
- Scrape `http://127.0.0.1:9779/metrics` from a local Prometheus, or front it
  with a reverse proxy for off-host scraping.
- Nothing persists across restarts; rate() style queries are unaffected, raw
  totals reset.

## Findings

- **questionable** | `metrics.py - MetricsRegistry._register_defaults()` | Six of
  the ten default metrics (`provider_calls_total`, `provider_quota_used`,
  `module_loaded`, `provider_active`, `sender_queue_depth`,
  `authed_admins_count`) have no update call site anywhere in the codebase
  (verified by repository-wide search); they render as constant `0`, which reads
  as "healthy but idle" on a dashboard rather than "not instrumented".
- **doc-drift** | `metrics.py` module docstring | The
  `# TODO(internets.py): from metrics import registry; registry.enable(...)`
  line is stale: `internets.py - _main()` already wires `enable()`/`expose()`
  from the `[metrics]` config section.
- **questionable** | `metrics.py - MetricsRegistry.expose()` | The guard rejects
  only unspecified addresses while the docstring and the `ValueError` message
  claim "loopback-only"; a specific non-loopback IP, or a hostname resolving to
  one, binds successfully
  (`tests/test_metrics.py - TestExposeGuard.test_non_ip_host_passes_guard_then_bind_fails`
  shows a hostname passing the guard). The inline comment says non-loopback
  specific binds are deliberate; the message text is what misleads.
- **questionable** | `metrics.py - MetricsRegistry.render()` | `help_text` is
  emitted unescaped; a newline in a help string passed to the public
  `counter()`/`gauge()` API would corrupt the exposition output. All current
  help strings are static literals, so this is latent.
- **questionable** | `metrics.py - MetricsRegistry.expose()` | Single-threaded
  `HTTPServer` with no socket timeout: one stalled scraper connection blocks
  subsequent scrapes indefinitely. Loopback-default exposure limits who can do
  this.
- **test-gap** | `metrics.py - MetricsRegistry.shutdown()` | The 2 s thread-join
  timeout path (thread refusing to exit) is untested; tests only cover the
  clean and idempotent shutdown paths.
