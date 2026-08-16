# Service objectives and alerting

This page defines what "healthy" means for a running Internets bot, proposes
objectives worth holding it to, and states honestly which of those objectives
the current code can actually measure.

**Scope.** This is a single-operator, self-hosted IRC bot. There is no
customer, no contract, and no error budget to spend. Treat everything below as
an operator's working definition of "is it doing its job", not as an SLA. The
value of writing it down is that it makes the gap between *what you want to
know* and *what the process will tell you* explicit, and that gap is currently
wide.

[metrics-and-observability](metrics-and-observability.md) describes the
surfaces (log, `.stats` / `.health`, the Prometheus exporter, the audit log)
and every metric that exists. This page does not repeat that inventory; it
builds objectives and alert rules on top of it.

---

## 1. What "healthy" means to a user

A user of this bot cannot see the event loop, the sender queue, or the
provider chain. Two things are observable from a channel, and they are the
only two that define availability:

1. **The bot is present.** Its nick is in the channel and it is registered
   with the server.
2. **A command gets a reply.** A user types `.w denver` and a reply appears
   before they give up on it.

Everything else in this document is a leading indicator of one of those two
failing.

### 1.1 The two-level definition

| Level | Definition | Bound in source |
| --- | --- | --- |
| Available | Connected and registered; the read loop is consuming lines | `internets.py - IRCBot.run()` reconnect loop |
| Responsive | A dispatched command completes and its reply reaches the wire | `IRCBot._CMD_TIMEOUT` = 60 s |

The responsiveness bound is real and enforced:
`internets.py - IRCBot._run_cmd()` wraps every handler in
`asyncio.wait_for(..., timeout=self._CMD_TIMEOUT)`, counts a timeout in
`_metrics["command_timeouts"]`, logs `event=command_timeout`, and notices the
caller. So "no reply within 60 s" is, for a well-behaved handler, a state the
bot itself detects and reports.

Two qualifications matter, and both are load-bearing for the objectives below.

**The 60 s cap does not cover delivery.** A handler finishes when it has
*enqueued* its reply. `internets.py - IRCBot.privmsg()` calls `send()`, which
calls `Sender.enqueue()` and returns. The rate limiter then meters the message
onto the wire at roughly 40 messages per minute
(`sender.py - Sender.REFILL` = 1.5 s per token). A multi-line reply can
therefore be "complete" by the timeout's definition and still be arriving a
minute later. See [performance](performance.md) for the arithmetic.

**The 60 s cap cannot interrupt synchronous work.** `asyncio.wait_for` can only
cancel at an `await` point. A handler that runs CPU-bound code directly on the
loop is not interruptible, and while it runs *no* command from *any* user
completes.

> **Known defect** (`modules/mathx.py - MathxModule.cmd_isprime()`,
> [known-issues](known-issues.md#3-isprime-can-hang-the-entire-bot)):
> `cmd_isprime` calls `_isprime()` synchronously on the loop while the sibling
> `cmd_bignum` correctly uses `asyncio.to_thread`. A 100-digit semiprime (the
> input cap permits it) can stall every user's commands indefinitely, and the
> command timeout does not fire. Any availability objective stated below is
> void while this path is reachable, because a single message from any user in
> any channel can take the whole bot to zero.

---

## 2. Proposed objectives

Each objective states the signal, why it is the right thing to watch, and
whether the current code can measure it. "Measurable today" means a number a
scrape or an admin command will actually produce. Read the verdicts as written:
several are **no**.

### 2.1 Reconnect frequency

**Objective.** No more than a small number of connection losses per day under
normal network conditions; treat a step change as the alert, not an absolute.

**Why.** Every reconnect is a user-visible gap. It also clears authenticated
admin sessions (`event=auth_sessions_cleared`), rebuilds the `Sender`, and
discards whatever was queued, so the cost is larger than the disconnect
window itself.

**Measurable today: yes, with a caveat.** `internets_reconnects_total` is
incremented in the transport-error branch of `internets.py - IRCBot.run()`,
alongside the private `_metrics["reconnects"]`. Read-inactivity
(`_READ_TIMEOUT` = 300 s, re-raised as `ConnectionResetError`) and
dead-link detection (`_PONG_TIMEOUT` = 240 s, which closes the writer) both
funnel into the same branch, so the counter covers those paths too.

The caveat: the counter fires once per **connection loss**, not once per
**reconnect attempt**. The inner retry loop logs `event=reconnect_failed` and
increments only its local `attempt`. A bot flapping through twenty failed
reconnects against one outage shows `1`. Alert on the log line as well as the
counter.

### 2.2 Outbound queue drops

**Objective.** Zero dropped outbound messages. This is the one objective where
a non-zero value is always wrong, because a drop is a reply a user asked for
and never received, with no error shown to them.

**Why.** `sender.py - Sender._safe_put()` drops a priority-1 message when the
200-slot queue is full, and logs `Send queue full - dropping message` at
WARNING. There is no retry and no notification to the requesting user.

**Measurable today: yes.** `internets_dropped_messages_total` is incremented
from `sender.py - _bump_dropped()`, and the bot's own
`_metrics["dropped_messages"]` is incremented through the `on_drop` callback,
which surfaces it in the `event=shutdown_complete` line and in `.health`.

**But it undercounts.** `internets.py - IRCBot._connect()` constructs a fresh
`Sender` on every connect, and `Sender.start()` also replaces `self._q` with a
new `PriorityQueue`. Anything still queued when the link dropped is discarded
by garbage collection, not by `_safe_put()`, so it is **never counted as a
drop**. After a reconnect the counter reports only the overflow that happened
before the queue filled, not the queue that was thrown away. Treat the metric
as a floor.

### 2.3 Task-capacity rejections

**Objective.** Zero dispatch rejections. Reaching the cap means users are being
told "bot is busy" while the bot is, from their point of view, idle.

**Why.** `internets.py - IRCBot._dispatch()` refuses a command when
`self._active_cmd_tasks >= self._MAX_TASKS` (50), logs
`event=dispatch_rejected reason=at_capacity active=%d cap=%d`, and notices the
caller. Fifty concurrent in-flight commands on a bot this size is not organic
load; it means handlers are not completing, which usually means a slow
provider chain or a wedged handler.

**Measurable today: no metric, yes log.** There is no counter for this. The
only signal is the `event=dispatch_rejected` WARNING line. `.stats` reports
neither `_active_cmd_tasks` nor the cap, so the *approach* to the cap is
invisible; you learn about it at the moment it is breached.

This is the single cheapest instrumentation gap to close: a counter next to
the existing log call, and a gauge for `_active_cmd_tasks`.

### 2.4 Provider success rate

**Objective.** Each configured weather provider stays above its useful success
rate, and no provider sits with its circuit breaker open for extended periods.

**Why.** The dispatcher falls back, so one bad provider degrades quality
silently rather than producing an error. A key that expired three weeks ago
looks exactly like a provider that is simply ranked lower.

**Measurable today: no, not through the exporter.**
`internets_provider_calls_total` and `internets_provider_active` are two of the
six metrics that have **no update call site anywhere in the codebase** and
render as a constant `0`
([known-issues](known-issues.md#13-lower-severity-items),
[metrics-and-observability](metrics-and-observability.md#every-metric)). A
Prometheus rule written against them will never fire, which is worse than no
rule.

The data exists but only in process memory, reachable through IRC:
`weather_providers/_health.py - ProviderHealth` carries `success_rate`
(an EMA), `avg_latency`, `total_calls`, `total_failures`, the decayed
`rate_limit_count`, and the breaker state machine (`cb_state`, tripping at
5 consecutive failures inside a 60 s window, cooling down for 60 s).
`.health` renders it via `weather_providers.provider_status()`. There is no
programmatic scrape path.

**Verdict:** this objective is enforceable only by a human running `.health`.
Do not claim it is monitored.

### 2.5 Audit write failures

**Objective.** Zero failed audit-log writes. An admin action that executes but
is not recorded destroys the property the audit chain exists to provide.

**Why.** Every admin command routes through
`admin_cmds.py - AdminCommandsMixin._audit()`, which calls
`audit_log.AuditLog.record()`. `record()` catches `OSError`, logs
`audit_log: write failed: <ExceptionType>` at ERROR, and re-raises. The caller
catches everything and logs `audit_log record failed: ...` at WARNING, because
an audit failure must not break the admin command it is recording.

**Measurable today: no.** `internets_audit_records_total` is incremented
**only after a successful append**, inside `record()`. A failure produces two
log lines and no counter movement. From the exporter's view, a disk-full
condition that silently stops all audit recording is indistinguishable from an
idle bot: both show a flat counter.

This is the most consequential of the measurement gaps, because it is the one
whose failure mode is *silence in the security record*. See
[logging-and-auditing](logging-and-auditing.md) for what the chain does and
does not prove.

### 2.6 Objective summary

| Objective | Signal today | Verdict |
| --- | --- | --- |
| Reconnect frequency | `internets_reconnects_total` + `event=connection_lost` | Measurable; counts losses, not attempts |
| Outbound drops | `internets_dropped_messages_total` + `.stats` | Measurable; undercounts on reconnect |
| Task-capacity rejections | `event=dispatch_rejected` log only | Log only, no metric, no headroom view |
| Provider success rate | `.health` only | Not scrapable; exporter series are constant zero |
| Audit write failures | two log lines only | Not measurable; success counter cannot show failure |

---

## 3. Alert definitions

Written for Prometheus + Alertmanager, since that is the exporter's format.
Where the signal is a log line rather than a series, the rule belongs in
whatever reads `internets.log` (journald, a log shipper, or a cron grep);
those rows are marked **log**.

Severity here is operational, not organizational: **page** means it needs
attention now, **ticket** means it needs attention this week, **info** means
record it and look at the trend.

### 3.1 Availability

| Condition | For | Severity | First check |
| --- | --- | --- | --- |
| `up{job="internets"} == 0` | 2m | page | [troubleshooting - Metrics endpoint unreachable](troubleshooting.md#metrics-endpoint-unreachable), then whether the process is alive at all |
| No `internets_*` samples at all (see [4](#4-the-dead-mans-switch)) | 5m | page | Process state, then [troubleshooting - Cannot connect](troubleshooting.md#cannot-connect) |
| `increase(internets_reconnects_total[15m]) >= 3` | 15m | ticket | `internets.conn` log for `event=connection_lost` and `event=reconnect_failed`; [troubleshooting - Cannot connect](troubleshooting.md#cannot-connect) |
| `increase(internets_reconnects_total[1h]) >= 10` | 1h | page | Same, plus [troubleshooting - SASL failure](troubleshooting.md#sasl-failure) if `event=reconnect_aborted reason=auth_failed` appears |

The `up == 0` rule cannot distinguish "process dead" from "exporter wedged".
The exporter is a single-threaded `http.server.HTTPServer` with no socket
timeout, so one stalled client blocks every subsequent scrape indefinitely
(see [metrics-and-observability](metrics-and-observability.md#known-exporter-limitations)).
Check the process before assuming a crash.

### 3.2 Responsiveness

| Condition | For | Severity | First check |
| --- | --- | --- | --- |
| `increase(internets_dropped_messages_total[5m]) > 0` | 0m | page | [troubleshooting - No outbound replies](troubleshooting.md#no-outbound-replies); then whether a module is emitting multi-line output in a loop |
| `event=dispatch_rejected` in the log | any | page | **log.** Count in-flight work; a slow provider chain or a wedged handler is holding the 50 task slots |
| `event=command_timeout` more than a few per hour | 1h | ticket | **log.** The `cmd=` field names the handler; check its provider chain in `.health` |
| `rate(internets_commands_total[10m]) == 0` while the bot is connected | 30m | info | Correct if the channel is quiet. Meaningful only against a known-busy baseline |

`internets_commands_total` counts **dispatch attempts**, not completions:
`_dispatch()` increments it before `create_task()` and never records an
outcome. A rule built on it says something about demand and nothing about
success. Do not phrase it as an error rate.

### 3.3 Correctness of the record

| Condition | For | Severity | First check |
| --- | --- | --- | --- |
| `audit_log record failed` or `audit_log: write failed` in the log | any | page | **log.** Disk space and permissions on `audit.log`; [troubleshooting - Audit verify reports a broken chain](troubleshooting.md#audit-verify-reports-a-broken-chain) |
| `.audit verify` reports a break | manual | page | [logging-and-auditing](logging-and-auditing.md), noting that `verify()` reads only the live segment |
| `event=metrics_start_failed` in the log | any | ticket | **log.** Until fixed, every rule in [3.1](#31-availability) and [3.2](#32-responsiveness) is vacuous |
| `event=store_flush_failed` equivalent (`Store flush failed` at ERROR) | any | ticket | **log.** Disk or permissions; state is dirty in memory and retries every 30 s |

### 3.4 Rules that should not be written

- Anything referencing `internets_provider_calls_total`,
  `internets_provider_quota_used`, `internets_module_loaded`,
  `internets_provider_active`, `internets_sender_queue_depth`, or
  `internets_authed_admins_count`. All six are constant zero. A rule on them
  is not a quiet rule; it is a rule that will never fire while looking like
  coverage.
- Anything comparing a raw counter total against a threshold. Nothing is
  persisted, so every counter resets to zero on restart. Use `rate()` or
  `increase()`.
- Any latency objective. There is no latency instrumentation of any kind; see
  [5](#5-instrumentation-worth-adding).

---

## 4. The dead man's switch

**The exporter reports only while the process is alive.** Every counter and
gauge lives in `metrics.py`'s in-memory registry, served by a daemon thread
inside the bot process. When the bot dies, the scrape fails; it does not report
zero, and it does not report anything at all.

That makes **absence of metrics the strongest health signal this system
produces**. It is stronger than any individual series, because it cannot be
faked by an unwired counter or by a subsystem that fails silently.

**Nothing currently watches for it.** There is no external heartbeat, no
watchdog, no push to a dead-man's-switch service, and no systemd `WatchdogSec`
integration. If the bot exits at 02:00, the first indication is a user asking
why it left the channel.

Closing this needs one of three things, in increasing order of cost:

1. **Prometheus `up == 0`,** which is free once the exporter is enabled and a
   scrape target exists. This is the rule in [3.1](#31-availability). It
   requires a Prometheus that is itself monitored, otherwise the watcher's
   death is as silent as the bot's.
2. **A systemd watchdog.** `Type=notify` with `WatchdogSec` makes the
   supervisor the observer, which removes the dependency on a scrape stack.
   The bot does not currently call `sd_notify`, so this needs code.
3. **An external heartbeat push** to a hosted dead-man's-switch. This is the
   only option that survives losing the whole host, and the only one that
   detects a network partition between the bot and its watcher.

Whichever is chosen, one property has to hold: a heartbeat must never be
advanced by a step that was skipped or short-circuited. A liveness marker that
ticks on a code path which no longer does the work is worse than none, because
it converts an outage into a green dashboard.

---

## 5. Instrumentation worth adding

Each entry names what it would catch that nothing currently catches. All six
are absent from the code today, not merely unconfigured.

`metrics.py` provides only `Counter` and `Gauge`; there is no histogram or
summary type. Latency instrumentation therefore needs either a new metric type
or explicit bucket counters, which is a real (if small) piece of work rather
than a call-site addition.

### 5.1 Command latency

**What it catches.** The difference between "the bot answers" and "the bot
answers eventually". A `.w` that takes 38 s is not a timeout and generates no
log line, but it is a user who stopped waiting. A latency distribution also
turns the `_CMD_TIMEOUT` = 60 s cap from a cliff into a curve, so you see the
approach instead of the breach.

**Shape.** Bucketed counters labelled by `command`, observed in
`internets.py - IRCBot._run_cmd()` around the existing `wait_for`, with the
timeout branch counted separately from the success branch. That one call site
also fixes the `commands_total` weakness noted in
[3.2](#32-responsiveness): recording an outcome there gives a real
success/timeout/error breakdown.

### 5.2 Event-loop lag

**What it catches.** The `.isprime` failure class, and every future instance of
it. The bot is single-loop; a handler that blocks the loop makes *every*
command slow simultaneously, which no per-command metric distinguishes from
"the network is slow today".

**Shape.** A task that sleeps a fixed interval and records
`actual_elapsed - expected_elapsed` as a gauge. On a healthy loop this sits near
zero. It is the one metric that would have made the `.isprime` defect visible
as a monitoring signal rather than as a user complaint.

This is the highest-value item on this list. It is cheap, it needs no new
metric type if exposed as a gauge of the last observed lag, and it detects an
entire defect class rather than one symptom.

### 5.3 Provider call latency

**What it catches.** A provider degrading before it fails. The circuit breaker
in `weather_providers/_health.py` trips on 5 consecutive failures in 60 s; a
provider that answers every request in 28 s never trips it, but it consumes
almost the whole `_PER_CALL_BUDGET` (30 s) and starves the fallbacks queued
behind it inside the 45 s `_CHAIN_BUDGET`.

**Shape.** `ProviderHealth.avg_latency` is already computed per provider on
every call; it is simply not exported. Exposing it as a gauge labelled by
provider is close to free, and it retires part of the objective in
[2.4](#24-provider-success-rate) at the same time.

### 5.4 State-flush duration

**What it catches.** The persistence thread falling behind. `store.py`'s
background thread flushes dirty datasets every `_FLUSH_INTERVAL` = 30 s. If a
write starts taking longer than the interval (a slow disk, a large user
dataset, a network filesystem), flushes serialize and the in-memory window of
unpersisted state grows without any log line, because nothing has failed.

**Shape.** A gauge of the last flush duration and a counter of flush failures,
set in `store.py - Store.flush()`. The failure counter matters independently:
`Store._flush_loop()` deliberately swallows exceptions so a bad flush cannot
kill the persistence thread, which is correct, but it means repeated failure is
visible only as a repeated ERROR line.

### 5.5 Thread-pool saturation

**What it catches.** Blocking work queueing behind a pool smaller than the task
cap. Nothing in the codebase calls `loop.set_default_executor()`, so
`asyncio.to_thread` uses the default `ThreadPoolExecutor`, sized
`min(32, cpu_count + 4)`. On a 2-core VPS that is **6 workers** against a
50-slot task cap. Seven concurrent `.g` lookups (blocking `requests` calls
offloaded via `to_thread`) put the seventh in a queue that no metric,
log line, or admin command reveals.

**Shape.** A gauge of active workers and of queued work items, sampled from the
executor. Alternatively, set the executor size explicitly at startup so at
least the number is a deliberate choice rather than a function of the host.

### 5.6 Audit write failures

**What it catches.** Exactly the gap in [2.5](#25-audit-write-failures): the
difference between "no admin actions happened" and "admin actions happened and
were not recorded". These are currently the same picture from the exporter.

**Shape.** A counter incremented in `audit_log.AuditLog.record()`'s `except
OSError` branch, mirroring the existing success increment. Four lines, and it
converts a silent security-relevant failure into an alertable one. Pair it with
a gauge for the last successful `verify()` result so chain integrity is
scrapable rather than only interactively checkable.

---

## 6. Related reading

- [metrics-and-observability](metrics-and-observability.md) - every metric,
  the exporter, and the other observability surfaces.
- [performance](performance.md) - the capacity envelope these objectives sit
  inside, and the degradation modes they would detect.
- [troubleshooting](troubleshooting.md) - the diagnostic procedure behind each
  "first check" above.
- [operations](operations.md) - running, restarting, and routine maintenance.
- [known-issues](known-issues.md) - the verified defects referenced here.
- [internals/metrics](internals/metrics.md) - registry and exposition
  internals.
