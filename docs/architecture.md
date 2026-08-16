# System Architecture

How the bot is put together at the system level: what runs where, what crosses
which boundary, and what is bounded by what. It is written for an engineer who
has never seen this codebase and needs a correct mental model before touching
anything.

This page stops at the boundary of each subsystem. Line-level behavior, per-symbol
contracts, failure branches, and known defects per file live in the implementation
reference under [internals/index](internals/index.md); every section below links
down to the page that carries the detail rather than restating it.

Citations are symbol-primary (`file.py - Class.method()`). The source is
authoritative; if this page and the code disagree, the code wins and the page is
a defect.

---

## 1. Component map

One process. Thirteen root modules, two packages, no service dependencies beyond
the IRC server and whatever HTTP endpoints the loaded modules call.

| Source | Owns | Detail |
|---|---|---|
| `internets.py` | event loop, connection, dispatch, module loader, entry point | [internets](internals/internets.md) |
| `admin_cmds.py` | the `_CORE` table and every `cmd_*` handler | [admin_cmds](internals/admin_cmds.md) |
| `sender.py` | priority queue, token bucket, line serialization | [sender](internals/sender.md) |
| `protocol.py` | pure IRC parsers, no state, no I/O | [protocol](internals/protocol.md) |
| `console.py` | operator stdin REPL on a daemon thread | [console](internals/console.md) |
| `config.py` | layered ini read, CLI args, frozen constants | [config](internals/config.md) |
| `secret_store.py` | two-tier credential resolution, 0600 gate | [secret_store](internals/secret_store.md) |
| `hashpw.py` | admin password hash and verify | [hashpw](internals/hashpw.md) |
| `botlog.py` | handlers, per-subsystem debug, startup validation | [botlog](internals/botlog.md) |
| `store.py` | three JSON datasets, flush thread, `RateLimiter` | [store](internals/store.md) |
| `audit_log.py` | hash-chained privileged-action log | [audit_log](internals/audit_log.md) |
| `process_lock.py` | PID lockfile with stale detection | [process_lock](internals/process_lock.md) |
| `metrics.py` | metric registry and optional HTTP exporter | [metrics](internals/metrics.md) |
| `modules/` | 70 command modules plus shared helpers | [modules](internals/modules/index.md) |
| `weather_providers/` | 32 providers behind one dispatcher | [providers](internals/weather-providers/index.md) |

Guides that sit beside this one: [irc-protocol](irc-protocol.md) for the wire
protocol, [state-and-persistence](state-and-persistence.md) for the on-disk
formats, [security-model](security-model.md) for the threat model,
[providers](providers.md) for the weather layer, [modules](modules.md) and
[writing-modules](writing-modules.md) for the extension surface.

---

## 2. Process and execution model

### 2.1 One loop, four kinds of thread

All protocol processing, dispatch, and sending happen on a single asyncio event
loop. Threads exist only where a blocking call would otherwise stall it.

| Thread | Created by | Daemon | Present when |
|---|---|---|---|
| event loop (main) | `internets.py - _entry()` via `asyncio.run` | n/a | always |
| `store-flush` | `store.py - Store.__init__()` | yes | always |
| `console-input` | `console.py - run_console()` | yes | stdin is a TTY and `--no-console` unset |
| metrics exporter | `metrics.py - MetricRegistry.expose()` | yes | `[metrics] enable = true` |
| `to_thread` workers | `asyncio.to_thread` in handlers | no | on demand, default executor |

The `to_thread` workers are the reason the shared state in section 6 is guarded by
`threading.Lock` rather than `asyncio.Lock`: a synchronous worker cannot acquire an
`asyncio.Lock`, and the design is intended to hold under free-threaded builds where
the GIL cannot be relied on as an implicit lock.

The `console-input` thread being a raw `threading.Thread(daemon=True)` rather than
an `asyncio.to_thread` worker is load-bearing, not stylistic. A `to_thread` worker
is non-daemon and lives on the default executor, so `asyncio.run`'s
`loop.shutdown_default_executor()` waits forever for a worker parked in `input()`
and the process hangs on its last log line. The rationale is recorded at the call
site in `internets.py - _main()` and in `console.py`; see
[ADR-008](design-decisions.md#adr-008-daemon-thread-for-the-interactive-console).

### 2.2 Offloading is a convention, not an enforcement

Command handlers are coroutines. Anything blocking inside one - HTTP through
`requests`, disk, password hashing - must be wrapped in `asyncio.to_thread`. One
hundred call sites do this (95 in `modules/`, 5 in `admin_cmds.py`). Nothing in
the loader or the dispatcher checks it.

:::{warning}
**Known defect: .isprime blocks the event loop.**

`modules/mathx.py - MathxModule.cmd_isprime()` runs its primality test
synchronously on the loop thread (the sibling `cmd_bignum` uses `to_thread`
correctly). A composite that survives trial division falls into an unbounded
Pollard rho, so a pasted 100-digit semiprime stalls the entire bot for every user.
Verified; recorded in [known issues](known-issues.md). The 60 s command timeout does
not help: `asyncio.wait_for` cannot interrupt a synchronous call.
:::

### 2.3 Bounds

Every resource an inbound line can consume is capped. These are class attributes on
`internets.py - IRCBot` unless noted, so tests can assert them without an instance.

| Bound | Value | Symbol | Guards against |
|---|---|---|---|
| concurrent command tasks | 50 | `IRCBot._MAX_TASKS` | task-slot exhaustion |
| per-command wall time | 60 s | `IRCBot._CMD_TIMEOUT` | a wedged handler holding a slot |
| command argument length | 400 | `IRCBot._MAX_ARG_LEN` | oversized handler input |
| outbound body chunk | 400 bytes | `IRCBot._MAX_BODY` | line-limit overflow |
| inbound stream buffer | 8192 bytes | `IRCBot._READ_LIMIT` | unbounded line buffering |
| read inactivity | 300 s | `IRCBot._READ_TIMEOUT` | a silently dead link |
| outbound queue depth | 200 | `sender.py - Sender.MAX_QUEUE` | OOM while disconnected |
| outbound line | 512 bytes | `sender.py - Sender._MAX_IRC_LINE` | RFC 2812 violation |
| state file read | 10 MB | `store.py - Store._MAX_FILE_SIZE` | startup memory exhaustion |
| audit log size | 5 MiB | `audit_log.py - _MAX_BYTES` | unbounded growth (rotates) |
| weather chain / call | 45 s / 30 s | `weather_providers/_dispatch.py` | chain outliving the 60 s timeout |

Rate limiting is a separate layer, described in sections 4 and 5.

---

## 3. Startup

`_entry()` is the `pyproject.toml` console-script entry point and the `__main__`
guard. Importing `internets.py` transitively imports `config.py`, which reads
`config.ini`, parses `sys.argv`, and can terminate the process before any of the
below runs; see [config](internals/config.md) for that import-time contract.

```{graphviz}
digraph startup {
  rankdir=TB;
  node [shape=box, fontname="Helvetica", fontsize=10];
  edge [fontname="Helvetica", fontsize=9];

  imp   [label="import config / botlog\nini read, argv parse,\nfail-closed validation", shape=ellipse];
  root  [label="_entry(): drop-root guard\neuid 0 refused unless\nINTERNETS_ALLOW_ROOT=1"];
  lock  [label="ProcessLock('./internets.pid')\nacquire"];
  run   [label="asyncio.run(_main(lock))"];
  ctor  [label="IRCBot()\nStore + flush thread,\nRateLimiter, shadow-bans"];
  met   [label="metrics registry\nexpose(host, port)", style=dashed];
  con   [label="console task\n(TTY only)", style=dashed];
  bot   [label="bot.run()"];
  sig   [label="signal handlers\nSIGTERM/SIGINT/SIGHUP"];
  load  [label="autoload_modules()"];
  conn  [label="_connect()\nTLS ctx, open_connection,\nper-connection reset, new Sender"];
  reg   [label="registration burst (priority 0)\nPASS? CAP LS 302 / NICK / USER"];
  loop  [label="read loop\nreadline raced against _stop", shape=box, style=bold];
  motd  [label="376/422 once:\nCAP END, user modes,\nNickServ, OPER,\nkeepalive + rejoin tasks"];

  imp -> root -> lock -> run -> ctor;
  ctor -> met [style=dashed];
  ctor -> con [style=dashed];
  ctor -> bot;
  bot -> sig -> load -> conn -> reg -> loop -> motd;
  conn -> conn [label="fail: jittered backoff\n15s..300s, +/-25%"];
}
```

Points that matter beyond the picture:

- **Root refusal** is in `_entry()` before anything else, overridable only by
  `INTERNETS_ALLOW_ROOT=1`, which logs a warning.
- **The lock is acquired around the whole loop** and the instance is passed into
  `_main()` so the restart path can release it early. See section 9.
- **The console is gated twice**: `--no-console` and `console.py -
  should_skip_console()` (stdin must be an interactive TTY). The console is an
  unauthenticated admin surface, so a non-TTY stdin must never reach it.
- **The initial connect loop is interruptible**: it sleeps on `self._stop.wait()`,
  not `asyncio.sleep`, so shutdown during backoff breaks out at once.
- **The MOTD gate fires once per connection**, keyed on the `identified` flag in
  `IRCBot.run()`. Credential sends inside it are each gated by
  `IRCBot._tls_or_refuse()`.

:::{warning}
**Known defect: multiline CAP LS 302.**

`IRCBot.run()` sends `CAP LS 302`, which invites the server to split its
capability list across several lines, but `IRCBot._handle_cap()` and `_RE_CAP`
treat the `*` continuation marker as a capability token, leave a leading colon on
the first real capability, and answer each line independently. A server whose
list does not fit one line can therefore get a premature `CAP REQ` or `CAP END`.
Verified by regex probe; see the findings in
[internals/internets](internals/internets.md#findings).
:::

---

## 4. Inbound path

One synchronous pass per server line, then a task per recognized command. Because
`IRCBot._process()` is synchronous, all protocol state updates (nick tracking,
session revocation, chanop changes) complete before the next line is read. Command
handlers give no ordering guarantee among themselves, by design: they may block on
network I/O for up to 60 s.

```{graphviz}
digraph inbound {
  rankdir=TB;
  node [shape=box, fontname="Helvetica", fontsize=10];
  edge [fontname="Helvetica", fontsize=9];

  rd   [label="readline (limit 8192, timeout 300s)\nraced against _stop.wait()", shape=ellipse];
  ovs  [label="oversized:\ncount, drain to \\n, skip", shape=note, style=dashed];
  dec  [label="decode utf-8 errors=replace\nstrip CRLF, log << (redacted)"];
  tags [label="strip_tags()\nIRCv3 @tags removed FIRST"];
  ping [label="PING -> PONG :payload[:400]\npriority 0, return", shape=note];
  pong [label="PONG -> _last_pong = monotonic\nreturn", shape=note];
  sb   [label="prefix nick shadow-banned?\nskip module fan-out"];
  fan  [label="module on_raw fan-out\n(snapshot under _mod_lock,\nper-module try/except)"];
  hnd  [label="_handle_cap / _handle_numeric /\n_handle_membership\nfirst match returns"];
  pm   [label="_handle_privmsg\nrecord hostmask, CTCP drop,\nextract command word"];
  disp [label="_dispatch: gate chain", shape=box, style=bold];
  task [label="_run_cmd task\nwait_for(handler, 60s)"];
  mod  [label="module handler\n-> bot.reply/privmsg"];

  rd -> ovs [style=dashed, label="ValueError"];
  rd -> dec -> tags;
  tags -> ping; tags -> pong;
  tags -> sb -> fan -> hnd -> pm -> disp -> task -> mod;
}
```

### 4.1 The dispatch gate chain

`IRCBot._dispatch()` applies gates in a fixed order. The order is the policy.

| # | Gate | On refusal |
|---|---|---|
| 1 | shadow-banned nick | silent drop, no rate-limit spend, no audit |
| 2 | `auth`/`deauth` outside PM | told to use PM |
| 3 | per-nick flood (`RateLimiter.flood_check`, admins bypass) | NOTICE "slow down" |
| 4 | per-channel burst (`RateLimiter.channel_check`) | silent, log only |
| 5 | argument longer than `_MAX_ARG_LEN` | NOTICE "input too long" |
| 6 | `_active_cmd_tasks >= _MAX_TASKS` | NOTICE "bot is busy" |
| 7 | handler resolution: `_CORE` first, then `_commands` | nothing runs |

Gate 1 is silent so a shadow-banned user cannot distinguish being ignored from the
bot being offline. Gate 4 is silent because a throttle notice would itself add to
the flood. Gate 6 is an O(1) counter check, not a scan of the task list.

Only after gate 7 does the bot increment the counter, bump the Prometheus
`commands_total`, and create the `cmd-<name>` task. A done-callback decrements the
counter and removes the task from `_tasks`.

`IRCBot._run_cmd()` is the only place a handler exception can surface. It maps
`TimeoutError` to a per-command notice, re-raises `CancelledError` (shutdown must
propagate), and turns anything else into a counted, logged traceback plus a generic
notice. No exception text and no internal state reach IRC.

### 4.2 Command recognition

`IRCBot._handle_privmsg()` builds the valid-command set as `_CORE | _commands`
under `_mod_lock`. In a channel the text must start with the live prefix from
`IRCBot._cmd_prefix()`; in PM a bare first token that matches a known command also
dispatches, so `weather 10001` works without the prefix. `_cmd_prefix()` reads
`cfg["bot"]["command_prefix"]` at use time rather than the frozen import-time
constant, so a rehash changes the prefix for the core and for modules together.

Deeper detail: [internals/internets](internals/internets.md) for the handlers,
[internals/protocol](internals/protocol.md) for the parsers and why every
wire-facing one is total, [irc-protocol](irc-protocol.md) for the wire view.

---

## 5. Outbound pipeline

Nothing except `sender.py - Sender` touches the `StreamWriter`. Flood control
therefore exists in exactly one place.

```{graphviz}
digraph outbound {
  rankdir=LR;
  node [shape=box, fontname="Helvetica", fontsize=10];
  edge [fontname="Helvetica", fontsize=9];

  src  [label="IRCBot.send()\nprivmsg / notice / reply\n(any thread)", shape=ellipse];
  spl  [label="_split_msg()\n400-byte UTF-8-safe chunks"];
  enq  [label="Sender.enqueue()\nseq under _seq_lk,\ncall_soon_threadsafe"];
  put  [label="_safe_put()\n(loop thread only)"];
  q    [label="PriorityQueue\n(priority, seq, msg)\nmaxsize 200", shape=cylinder];
  drop [label="overflow:\npri>0 dropped;\npri 0 evicts worst entry", shape=note, style=dashed];
  drn  [label="_drain()\ntoken bucket:\n5 burst, 1 per 1.5s\npri 0 bypasses"];
  wl   [label="_write_line()\nstrip CR/LF/NUL,\ncap 510 bytes + CRLF,\nredact log only"];
  w    [label="writer.write + drain", shape=ellipse];

  src -> spl -> enq -> put -> q -> drn -> wl -> w;
  put -> drop [style=dashed];
}
```

- **Priority 0** is protocol traffic (PONG, CAP, NICK, PASS, AUTHENTICATE, QUIT,
  keepalive PING). It bypasses the token bucket and, on a full queue, evicts the
  worst-ranked existing entry rather than being dropped. Losing a PONG causes a
  ping timeout and a reconnect storm, which is strictly worse than losing a chat
  line.
- **Priority 1** is everything user-visible and is rate-limited to a 5-line burst
  then roughly one line per 1.5 s.
- **`seq`** is a monotonic counter under `Sender._seq_lk`. It makes the heap a
  stable FIFO within a priority and keeps the non-comparable message string out of
  the tuple comparison.
- **Redaction is log-only.** `sender.py - redact_secrets()` masks the argument
  after a credential verb in the `>>` debug line; the wire carries the real value.
  The same function is reused inbound by `internets.py - _redact_inbound()`, scoped
  to the PRIVMSG/NOTICE trailing text so an `ident@host` prefix cannot false-match.

Drop accounting is double-entry: `Sender._drop()` bumps the Prometheus counter and
calls the bot's `on_drop` callback, which is what makes the shutdown summary's
`dropped=` figure real rather than always zero.

Detail, including the eviction algorithm and the known overstatement in its
docstring: [internals/sender](internals/sender.md).

---

## 6. Concurrency model

### 6.1 Every lock in the process

| Lock | Guards | Owner | Taken from |
|---|---|---|---|
| `ChannelSet._lock` | joined-channel set | `internets.py` | loop and workers |
| `IRCBot._mod_lock` | `_modules`, `_commands` | `internets.py` | loop and workers |
| `IRCBot._auth_lock` | `_authed`, `_auth_fails`, `_nick_hosts` | `internets.py` | loop and workers |
| `IRCBot._chanops_lock` | `_chanops` | `internets.py` | loop and workers |
| `Sender._seq_lk` | outbound sequence counter | `sender.py` | any producer thread |
| `Store._loc_lock` | saved locations dataset | `store.py` | loop, workers, flush thread |
| `Store._chan_lock` | channel-rejoin dataset | `store.py` | loop, workers, flush thread |
| `Store._user_lock` | user-tracking dataset | `store.py` | loop, workers, flush thread |
| `RateLimiter._lock` | flood, API, channel windows | `store.py` | loop and workers |
| `AuditLog._lock` | chain tip, key, append, verify | `audit_log.py` | loop and `to_thread` |
| `audit_log._default_lock` | singleton construction | `audit_log.py` | first caller |
| `_Metric._lock` | one metric's samples | `metrics.py` | any |
| `MetricRegistry._lock` | registry contents | `metrics.py` | any |
| `_quota_lock` | per-provider daily counters | `weather_providers/__init__.py` | loop and workers |
| `ProviderHealth._lock` | EMA scores, breaker state | `weather_providers/_health.py` | loop |
| `_session_lock` (asyncio) | cached aiohttp session | `weather_providers/_http.py` | loop only |

Module-local locks exist as well and guard only that module's own state:
`modules/channels.py`, `modules/seen.py`, `modules/remind.py`, `modules/tell.py`,
`modules/notes.py`, `modules/steam.py`, `modules/twitch.py`, `modules/ipintel.py`,
and `modules/geocode.py`.

Two properties hold across the set. No method takes two dataset locks, so the three
`Store` locks cannot deadlock against each other. Every lock is a `threading.Lock`
except the aiohttp session lock, which is loop-only by construction.

### 6.2 Cross-thread signalling

There are exactly two paths from a foreign thread into the loop, both explicit:
`IRCBot.request_shutdown()` and `Sender.enqueue()` each use
`loop.call_soon_threadsafe`. `request_shutdown()` is idempotent, first reason wins,
so a second SIGINT during a clean shutdown cannot rewrite the QUIT message.

Known-benign looseness: `IRCBot.send()` increments a statistics counter on whatever
thread called it. A lost increment skews a display counter and nothing else.

---

## 7. Extension boundaries

### 7.1 Module system

A module is a file in `modules/` exposing `setup(bot) -> BotModule`. The loader,
`internets.py - IRCBot.load_module()`, runs entirely under `_mod_lock` and applies
five gates before anything executes: name must match `^[a-z][a-z0-9_]*$`, the
module must not already be loaded, the file must exist, its resolved path must stay
inside `MODULES_DIR` (which also defeats a symlink out of the tree), and the
instance's `COMMANDS` must not collide with a command owned by another module.

The load itself is `spec_from_file_location` plus `module_from_spec` plus
`exec_module`. **No `sys.modules` entry is created for the loaded module.** That is
the whole reload story:

- `.reload weather` re-executes `modules/weather.py` fresh from disk, so source
  edits take effect without `importlib.reload` and without stale bytecode.
- Nothing module-internal survives: globals, caches, and class objects are all new.
  Cancelling background tasks in `on_unload()` is the module author's job.
- Imports made *by* the module (`from modules.base import ...`, third-party
  libraries, sibling helpers like `modules/geocode.py`) go through the normal
  import machinery and *are* cached in `sys.modules`. Editing a helper needs
  `.restart`, not `.reload`.

The developer-facing contract - handler signature, `COMMANDS` validation at
class-definition time, the `on_load` / `on_unload` / `on_raw` / `forget` hooks, and
the bot accessors modules may use - is in
[internals/modules/base](internals/modules/base.md) and
[writing-modules](writing-modules.md).

Loading a module is arbitrary code execution by design. Only admins can invoke
`.load`, and the autoload list comes from the operator's config file.

:::{danger}
**Known defect: API keys published to the channel.**

`modules/stocks.py - _try_providers()` builds its "all providers failed" reply by
joining `f"{name}: {e}"` for every provider and returns it to the user. urllib3
transport errors embed the full request URL including `token=` and `apikey=`
query parameters, so a network outage while keys are configured publishes every
finance API key to the channel.
`sender.py - redact_secrets()` is log-only and does not scrub PRIVMSG bodies.
Verified empirically and reproduced; see [known issues](known-issues.md).
:::

### 7.2 Weather provider layer

`weather_providers/` is the one subsystem with its own internal architecture. Its
boundary to the rest of the bot is narrow: `modules/weather.py` calls
`weather_providers.configure(cfg)` on load and then one `get_*` coroutine per
command. Everything else is internal.

| Layer | File | Responsibility |
|---|---|---|
| facade | `__init__.py` | 32 factories, `configure()`, `get_*` wrappers, quota counters |
| routing | `_dispatch.py` | capability discovery, ordering, fallback chain, gap-fill |
| health | `_health.py` | EMA scoring and the per-provider circuit breaker |
| transport | `_http.py` | size-capped async JSON fetch, `HTTPError` contract |
| shapes | `base.py` | frozen result dataclasses and the provider protocol |

Four properties define the boundary:

- **Capabilities are discovered, not declared.** A provider opts into a capability
  by defining the method named in `_dispatch.py - CAPABILITY_METHODS`.
- **Ordering is accuracy-dominant.** `Dispatcher.sort_chain()` keys on
  `(static reliability rank, -health score, registration order)`. Live health
  breaks ties among equally-ranked providers; it never promotes a less accurate
  provider over a more accurate healthy one.
- **The chain is serial, not a fan-out.** One upstream request per attempt keeps
  quota use and rate-limit exposure minimal, under a 45 s chain budget and a 30 s
  per-call cap that fit inside the 60 s command timeout.
- **No data is not failure.** A provider that does not cover a point returns
  `None`, which falls through with no health record at all. Only exceptions reach
  `record_failure()`. Without this, non-US queries would trip the NWS breaker and
  degrade US alerts.

Cross-provider merging is deliberately narrow, and only for current conditions:
missing secondary fields are filled from later providers, temperature never is, and
derived fields are recomputed from the primary observation. See
[ADR-010](design-decisions.md#adr-010-single-source-weather-rule) and
[providers](providers.md).

---

## 8. Persistence boundary

Nothing in this system uses a database. State is a small set of files in the
process working directory, each owned by exactly one component.

| Artifact | Owner | Shape | Written |
|---|---|---|---|
| `locations.json` | `store.py - Store` | v2 envelope, nick to location | dirty flush, 30 s |
| `channels.json` | `store.py - Store` | v2 envelope, sorted channel list | dirty flush, 30 s |
| `users.json` | `store.py - Store` | v2 envelope, per-channel tracking (PII) | dirty flush, 30 s |
| `shadow_bans.json` | `internets.py - IRCBot` | bans plus reasons, 0600 | on mutation |
| `audit.log` + `.key` | `audit_log.py - AuditLog` | JSON lines, hash-chained, 0600 | per privileged action |
| `internets.pid` | `process_lock.py` | `pid\|start_time\|hostname` | acquire and release |
| `config.ini` | operator, `secret_store.py` | ini, must be exactly 0600 | operator or CLI only |
| `internets.log` | `botlog.py` | rotating text | continuously |
| module state | individual modules | per-module JSON | per module |

Three invariants hold across the `Store` datasets:

1. **Integrity is checked, not assumed.** Each file is a v2 envelope
   (`{"schema": 2, "checksum": ..., "data": ...}`) whose SHA-256 is taken over
   canonical JSON, so key ordering cannot change it.
2. **A bad file is quarantined, never overwritten.** Any read failure renames the
   file to `<name>.corrupt.<unix-ts>` and starts from the default, so the next
   flush cannot destroy the only copy of saved locations, rejoin state, and privacy
   opt-out flags.
3. **Writes are atomic.** Write to a temp file in the same directory, `chmod 0600`
   before the rename so the final file is never momentarily world-readable, copy
   the current good file to `<path>.bak`, then `os.replace`.

Crash exposure is up to 30 s of unflushed mutation. There is no `fsync` before the
rename, so a power loss can still lose the newest version; the envelope plus
quarantine plus `.bak` turn that into a detected, recoverable event rather than
silent corruption.

:::{warning}
**Known defects in the persistence path.**

- `store.py - Store._write()` creates `<path>.bak` with `Path.write_bytes` and
  never chmods it, so on first creation it takes umask-default permissions
  (commonly 0644). The PII in `users.json` that the 0600-before-replace sequence
  protects is world-readable in `users.json.bak`.
- `modules/health.py` reads `_dirty_locations` / `_dirty_channels`, but the fields
  are `_dirty_locs` / `_dirty_chans`, so `.health` permanently reports `?` for two
  datasets while looking wired.
- `audit_log.py - AuditLog.verify()` dispatches the hash scheme on each record's
  own `v` field, so records rewritten without `v` verify under unkeyed SHA-256 at
  any chain position. A writer to `audit.log` can rewrite the chain from any point
  and `verify()` still reports it intact, reducing tamper evidence to the
  pre-3.0.0 scheme. All three are recorded in [known issues](known-issues.md).
:::

Full formats, retention rules, and recovery procedures:
[state-and-persistence](state-and-persistence.md),
[internals/store](internals/store.md), [internals/audit_log](internals/audit_log.md).

---

## 9. Shutdown and restart

`IRCBot.graceful_shutdown()` runs eight ordered steps, each in its own try/except
so one failure cannot abort the rest. The ordering is load-bearing: channels are
persisted before anything else can fail, and the QUIT is enqueued before the sender
stops.

1. Save the channel list.
2. Unload every module, giving each its chance to flush.
3. `Store.stop()`: stop the flush thread, force a final write.
4. Enqueue QUIT at priority 0, then sleep `_SHUTDOWN_DRAIN_S` (2 s) so the sender
   drains it. This is a fixed window, not a drain-to-empty.
5. Stop the sender.
6. Close the socket.
7. Cancel and gather remaining tasks; stop the metrics server.
8. Log the metrics summary and flush every log handler.

Step 8 is explicit because `os.execv` replaces the process image without running
atexit handlers, so unflushed records would be lost across a restart.

`.restart` sets `_restart_flag` and requests shutdown. Back in `_main()`, after the
task drain, the flag triggers: close log handlers, **release the process lock**,
then `os.execv` on POSIX or a `subprocess.Popen` self-relaunch plus `sys.exit(0)`
on Windows. The lock release is not optional. `execv` preserves the PID, so a
lockfile left on disk would make the new image probe its own live PID and refuse
to start.

Restart is also the only way to pick up an edit to a helper module, `modules/base.py`,
or any third-party library, for the `sys.modules` reason in section 7.1.

`SIGHUP` is rehash, not restart. `IRCBot._on_sighup()` calls
`config.reload_config()`, which re-reads `config.ini` and `config.local.ini`
together (re-reading only the template would clobber overlay values), then clears
admin sessions defensively. It deliberately does not refresh the import-time
credential constants `NS_PW` / `OPER_PW` / `SERVER_PW`.

---

## 10. Trust boundaries

Six boundaries, in the order an attacker meets them.

**Network to process.** Every byte from the IRC server is untrusted. It is bounded
by `_READ_LIMIT`, decoded with `errors="replace"` so no parser needs defensive
decoding, and handed to parsers in `protocol.py` that are total: they return a
value for any string and never raise, so a hostile or truncated line degrades to a
partial result instead of killing the read loop. Malformed ISUPPORT tokens return
`None` so the caller keeps its existing table rather than adopting a degraded one.

**Unauthenticated user to command.** Gates 1 to 6 of the dispatch chain in section
4.1 run before any handler, so flood control, argument bounds, and the task cap
apply to unauthenticated traffic. Failed-auth auditing is offloaded to a thread
precisely because that path is reachable by unauthenticated users under flood.

**User to admin.** `IRCBot.is_admin()` is fail-closed: it grants only when the
nick's current hostmask is known, is not the `"unknown"` sentinel, and equals the
hostmask bound at authentication time. A changed binding actively revokes. Sessions
are also revoked on QUIT, on NICK change (never migrated, so a nick takeover cannot
inherit a session), on disconnect, and on SIGHUP. Brute force is bounded by the
lockout in `admin_cmds.py - AdminCommandsMixin.cmd_auth()`.

**Process to network (credentials).** Every credential send is gated by
`IRCBot._tls_or_refuse()`, which logs CRITICAL and suppresses the send on a
plaintext link. TLS 1.3 is the floor unless `INTERNETS_ALLOW_TLS12=1`. Credential
values are redacted from logs in both directions but are never altered on the wire.

**Process to third-party HTTP.** Two enforcement points for one policy: no outbound
fetch may buffer an unbounded body. `modules/base.py - fetch_json()` caps module
fetches at 256 KiB by default; `weather_providers/_http.py - get_json()` caps
weather fetches at 1 MiB. Neither validates its destination, which is safe only
because those hosts are developer-chosen. A user-influenceable URL must go through
`modules/_netsafe.py - safe_open()`, which pins DNS per-thread so urllib3 cannot
re-resolve to an internal address between validation and connect, and re-validates
every redirect hop. See
[ADR-003](design-decisions.md#adr-003-thread-local-dns-pinning-for-ssrf-not-an-ip-literal-adapter).

**Operator to process.** The console is admin-equivalent with no authentication at
all: the trust boundary is physical access to the process's stdin, which is why it
is refused on a non-TTY. Config and secrets are operator-trusted input; the only
mechanical check is the permission gate on `config.ini`.

:::{warning}
**Known inconsistency: two different permission targets for config.ini.**

`botlog.py` warns on a world-readable `config.ini` and advises
`chmod 640 config.ini`, while `secret_store.py - perms_ok()` requires the mode to
be **exactly** 0600 and fails closed otherwise. An operator following the log's own
advice makes the `[secrets]` section unreadable, and the bot then runs keyless with
only an error line to say so. Verified against both sources.
:::

The full threat model, including what is explicitly out of scope, is in
[security-model](security-model.md).

---

## 11. Where to go next

- Wire protocol, CAP/SASL negotiation, numerics: [irc-protocol](irc-protocol.md)
- On-disk formats and recovery: [state-and-persistence](state-and-persistence.md)
- Threat model and controls: [security-model](security-model.md)
- Why the design is this shape: [design-decisions](design-decisions.md)
- Per-file implementation detail and per-file findings:
  [internals/index](internals/index.md)
