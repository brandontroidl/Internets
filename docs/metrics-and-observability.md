# Metrics and observability

The bot exposes operational state through four surfaces, in increasing order
of setup cost:

1. **The application log** - `internets.log`, always on. Structured
   `event=...` lines for lifecycle transitions
   (`event=shutdown_complete reconnects=%d dropped=%d ...`). See
   [logging-and-auditing](logging-and-auditing.md).
2. **IRC admin commands** - `.stats`, `.health`, `.uptime`. No configuration,
   no listener, no scrape target.
3. **The Prometheus exporter** - `metrics.py`, off by default, one config
   section to turn on.
4. **The audit log** - `.audit verify`, a separate privileged system covered
   in [logging-and-auditing](logging-and-auditing.md).

This page covers 2 and 3. Line-level detail is in
[internals/metrics](internals/metrics.md).

## The registry

`metrics.py` is a hand-rolled, pure-stdlib registry of counters and gauges
rendered in Prometheus text exposition format 0.0.4, plus an optional HTTP
exporter serving `GET /metrics`. There is no `prometheus_client` dependency;
the module states this as a design choice and its imports confirm it
(`ipaddress`, `logging`, `re`, `threading`, `http.server`, `typing`).

The module is inert by default. Importing it builds the module-level
singleton `registry` - dicts and locks only. Network exposure requires two
explicit steps, `registry.enable()` then `registry.expose(host, port)`, both
driven from `internets.py - _main()` and only when `config.ini` has
`[metrics] enable = true`.

If metrics are never enabled, increments are still accepted and stored. Only
the listener is gated.

All state is in-memory and process-lifetime. Nothing is persisted, so every
counter resets to zero on restart. `rate()`-style queries are unaffected;
raw totals are not comparable across a restart.

## Every metric

The ten canonical series are pre-registered by
`metrics.py - MetricsRegistry._register_defaults()` so call sites can use
attribute access without a registration race. Labels are supplied per call,
not declared at registration.

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `internets_commands_total` | counter | `module`, `command` | bot commands dispatched |
| `internets_provider_calls_total` | counter | (intended `provider`, `outcome`) | weather provider calls |
| `internets_provider_quota_used` | counter | none | estimated provider quota consumed |
| `internets_reconnects_total` | counter | none | IRC reconnect attempts |
| `internets_dropped_messages_total` | counter | none | outbound messages dropped |
| `internets_audit_records_total` | counter | none | audit records appended |
| `internets_module_loaded` | gauge | (intended: module name) | 1 if the module is loaded |
| `internets_provider_active` | gauge | (intended `provider`, `state`) | provider state flag |
| `internets_sender_queue_depth` | gauge | none | outbound sender queue depth |
| `internets_authed_admins_count` | gauge | none | authenticated admin sessions |

Only four are actually updated anywhere in the codebase:

| Name | Update point |
| --- | --- |
| `internets_commands_total` | `internets.py - _dispatch()`, before the command task is created; `module` is the owning module name, or `"core"` for built-in admin commands |
| `internets_reconnects_total` | `internets.py` connection loop, in the transport-error branch, alongside the private `_metrics["reconnects"]` bump |
| `internets_dropped_messages_total` | `sender.py - _bump_dropped()`, called from the queue-overflow path |
| `internets_audit_records_total` | `audit_log.py - AuditLog.record()`, after a successful append |

> **Known defect** (`metrics.py - MetricsRegistry._register_defaults()`,
> ledger entry "six of ten default metrics have no update call site"):
> **`internets_provider_calls_total`, `internets_provider_quota_used`,
> `internets_module_loaded`, `internets_provider_active`,
> `internets_sender_queue_depth`, and `internets_authed_admins_count` are
> never updated.** Verified by repository-wide search: no `.inc()`, `.set()`,
> or `.dec()` call exists for any of them outside `_register_defaults()` and
> the test suite.
>
> A metric with no samples renders as a single unlabeled `name 0` line
> (`render()`), so all six appear to a scraper as a healthy, idle series
> rather than as "not instrumented". **Do not build a dashboard panel or an
> alert rule on any of the six** - a panel reading "0 modules loaded" or
> "0 authenticated admins" is reporting the absence of instrumentation, not
> the state of the bot. This is built-but-not-wired code, not a
> configuration problem, and no config setting turns it on.
>
> The underlying data exists elsewhere and is reachable today: sender queue
> depth and authenticated admin count via `.stats` and `.health`, provider
> call and quota accounting via `weather_providers.provider_status()` (which
> `.health` renders), and the loaded-module list via `.modules`.

## Enabling the exporter

```ini
[metrics]
; Optional Prometheus text-format exporter.  Off by default.
enable = false
host   = 127.0.0.1
port   = 9779
```

`internets.py - _main()` checks `cfg.has_section("metrics")` and
`getboolean("enable", False)`. When true it calls `registry.enable()`, then
`registry.expose(host, port)` with `host` defaulting to `127.0.0.1` and
`port` to `9779`, and logs `event=metrics_enabled host=... port=...`.

Scrape `http://127.0.0.1:9779/metrics` from a local Prometheus, or front it
with a reverse proxy for off-host scraping.

## The exporter

- A stdlib `http.server.HTTPServer` on a daemon thread named `metrics-http`.
- The nested handler serves only `GET /metrics`. The query string is
  stripped before comparison, so `/metrics?x=y` works; every other path
  returns 404 with the body `not found\n`.
- Responses carry `Content-Type: text/plain; version=0.0.4; charset=utf-8`
  and a `Content-Length`.
- The default per-request stderr access log is redirected to `log.debug` on
  the `internets.metrics` logger.
- A second `expose()` while a server is running is a logged no-op.
- `registry.shutdown()` swaps the handles out under the lock, stops the
  server outside it, and joins the thread with a 2 s cap so shutdown cannot
  hang. It is called from shutdown step 7b, guarded by `is_enabled()`.

### Bind guard semantics

`expose()` refuses to start in three cases:

1. `enable()` was not called - logs and returns, no exception.
2. **All-interfaces bind** - the host string is stripped, parsed with
   `ipaddress.ip_address()`, IPv4-mapped IPv6 addresses are unwrapped, and a
   `ValueError` is raised if the result `is_unspecified` or the stripped host
   is empty. This catches `0.0.0.0`, `::`, `::0`, `::ffff:0.0.0.0`, `""`, and
   whitespace-padded forms.
3. Bind failure - `OSError` from `HTTPServer()` is logged and re-raised.

> **Read the guard precisely**: it rejects **only unspecified addresses**. A
> specific non-loopback IP such as `192.168.1.5`, or a hostname that resolves
> to one, passes the guard and binds. The inline comment says loopback is
> allowed "for the documented reverse-proxy front-end" and that only
> unspecified is rejected - but the `ValueError` message and the docstring
> both say the endpoint "must remain loopback-only", which overstates what is
> enforced. The endpoint is unauthenticated; the guard plus the loopback
> default are the entire access control, and binding it to a routable
> interface is a deliberate operator choice the code will not stop.

The `# nosec B104` marker suppresses a Bandit false positive: the `0.0.0.0`
literal appears only as a rejected value, never as a bind target.

### Known exporter limitations

> **Known issue** (`metrics.py - MetricsRegistry.expose()`): the server is
> the plain single-threaded `HTTPServer`, not `ThreadingHTTPServer`, and no
> socket timeout is set. One scrape is handled at a time, and a stalled
> client connection blocks every subsequent scrape indefinitely. The loopback
> default limits who can do this. A scrape timeout on the Prometheus side
> does not release the server-side connection.

> **Known issue** (`metrics.py - MetricsRegistry.render()`): `help_text` is
> emitted unescaped. Every current help string is a static literal, so this
> is latent, but a newline in a help string passed through the public
> `counter()` / `gauge()` API would corrupt the exposition output. Label
> *values* are escaped per exposition rules (backslash, double quote,
> newline) and label and metric *names* are allowlisted by regex, so
> attacker-influenced values such as a command name cannot break the line
> format.

## Failure isolation

The property "metrics must never crash the bot" is enforced at the call
sites, not centrally, and it holds at every current call site:

- `internets.py - _dispatch()`, the reconnect path, and the shutdown path
  each wrap both the import and the increment in
  `try/except Exception: pass`.
- `sender.py - _bump_dropped()` and the `audit_log.py` increment do the same,
  each with a comment stating the intent.
- `internets.py - _main()` wraps `enable()` and `expose()`, degrading to
  `log.error("event=metrics_start_failed err=%s", e)` on any exception,
  including the `ValueError` from the bind guard and an `OSError` from a busy
  port. The bot continues without metrics.

Inside the module, `Counter.inc()` raises `ValueError` on a negative amount,
and `_normalize_labels()` and `_Metric.__init__()` raise `ValueError` on an
invalid label or metric name. These surface programming errors to the
(wrapped) caller rather than storing a bad series.

Every `_Metric` has its own lock guarding its samples; the registry lock
guards registration, the enable flag, and server start and stop. `render()`
snapshots the name list under the registry lock and then reads each metric
under its own lock, which is safe because metrics are never removed.

## The other observability surfaces

### `.stats` (admin)

Reads live process state, no exporter required: process and connection
uptime, modules configured versus loaded, channel count, command and PRIVMSG
in/out counters, sender queue depth against `MAX_QUEUE`, audit record count,
and RSS in MiB.

### `.health` (admin)

A per-subsystem snapshot delivered privately, with every probe wrapped so one
broken subsystem degrades a single line instead of failing the command:
uptime, module list with `is_configured()` badges, weather provider status
(state, call and fail counts, health score, quota), sender queue depth, store
dirty flags, geocode cache statistics, authenticated admin count, audit chain
integrity, and the bot's private counters.

> **Known defect** (`modules/health.py - cmd_health()`): the store block
> reads `_dirty_locations` and `_dirty_channels`, but `store.Store` defines
> `_dirty_locs` and `_dirty_chans`. The `getattr` default means those two
> fields print `?` permanently while looking wired. Only `_dirty_users` is
> real.

Note also that `.uptime` measures from the health module's own load time, not
from process start, so a `.reload health` resets it.

### The private counter dict

`internets.py - IRCBot._metrics` is a plain `dict[str, int]` holding
`reconnects`, `dropped_messages`, `command_timeouts`, `oversized_lines`,
`sasl_failures`, and `unexpected_errors`. It feeds the
`event=shutdown_complete` summary line and the `.health` counters block.
Despite the name it has nothing to do with `metrics.py`; only `reconnects`
and `dropped_messages` have Prometheus counterparts, and only the first is
bumped in the same place.

## Operational use

### Worth alerting on

| Signal | Source | Why |
| --- | --- | --- |
| `rate(internets_reconnects_total[15m])` above baseline | exporter | link instability or a rejected registration; correlate with `internets.conn` and `internets.sasl` logs |
| `increase(internets_dropped_messages_total[5m]) > 0` | exporter | the outbound queue overflowed, so users are silently not receiving replies |
| absence of `internets_commands_total` samples | exporter | the process is up and scrapeable but dispatching nothing |
| `up == 0` for the scrape target | Prometheus | process down, or the single-threaded exporter wedged by a stalled client |
| audit chain not intact | `.health`, `.audit verify` | tamper evidence, subject to the verification limits in [logging-and-auditing](logging-and-auditing.md) |
| `event=metrics_start_failed` | log | the exporter never started; every other metric-based alert is now vacuous |

### Not worth alerting on

- Any of the six unwired series. They are constant zero by construction.
- Absolute counter values across a restart. Nothing is persisted, so every
  total resets; alert on rates and on `increase()` over a window, never on a
  raw total crossing a threshold.
- `internets_commands_total` volume by itself. It counts dispatch attempts,
  incremented before the command task is created, so it says nothing about
  whether handlers succeeded.

### Deployment notes

- Bind loopback and put authentication in a reverse proxy if the endpoint
  must leave the host. The bind guard will not stop you binding a routable
  address, and the endpoint has no authentication of its own.
- Keep the scrape interval modest and the scrape timeout short. The exporter
  handles one request at a time.
- `internets_commands_total` is labelled by `module` and `command`, so
  cardinality grows with the command surface (roughly 165 module commands
  plus the core set) multiplied by the number of owning modules. That is
  bounded and small, but it is the only labelled series, so it is the only
  one that can grow.

## Related reading

- [internals/metrics](internals/metrics.md) - registry, exposition format,
  exporter, and the full findings list.
- [internals/modules/health](internals/modules/health.md) - `.health` probe
  by probe.
- [internals/admin_cmds](internals/admin_cmds.md) - `.stats` and `.audit`.
- [logging-and-auditing](logging-and-auditing.md) - log stream and audit
  trail.
- [deployment](deployment.md) - enabling the endpoint in a real deploy.
- [configuration](configuration.md) - the `[metrics]` section in context.
