# Performance and capacity

The bounds in this document are read from source, not measured. Every number
with a symbol next to it is a constant you can go and check; every number
without one is a reasoned estimate, and the reasoning is shown so you can
disagree with it.

**No load testing has been done on this codebase.** There are no benchmark
results, no profiling runs, and no capacity measurements. [Section 6](#6-the-load-test-that-should-be-run)
describes the test that would replace the estimates in [section 5](#5-capacity-guidance)
with data. Until it is run, treat section 5 as arithmetic over documented
limits, which is useful for sizing and useless as a guarantee.

[architecture](architecture.md#6-concurrency-model) describes the concurrency
model structurally. This page is about where it runs out.

---

## 1. The concurrency model's practical limits

### 1.1 One event loop

The bot is a single `asyncio` event loop in a single process. Everything that
touches IRC state, dispatches commands, parses inbound lines, and drains the
outbound queue runs on it. There is no second loop and no process pool.

Three background threads exist alongside it: `store-flush` (`store.py`), the
`metrics-http` daemon when the exporter is enabled, and whatever
`asyncio.to_thread` allocates from the default executor. None of them can take
work off the loop unless a handler explicitly hands it over.

### 1.2 The four bounds that matter

| Bound | Value | Symbol |
| --- | --- | --- |
| Concurrent command tasks | 50 | `internets.py - IRCBot._MAX_TASKS` |
| Per-command wall clock | 60 s | `internets.py - IRCBot._CMD_TIMEOUT` |
| Command argument length | 400 chars | `internets.py - IRCBot._MAX_ARG_LEN` |
| Outbound body per line | 400 bytes | `internets.py - IRCBot._MAX_BODY` |

`IRCBot._dispatch()` checks `_active_cmd_tasks >= _MAX_TASKS` before creating
the task, logs `event=dispatch_rejected reason=at_capacity`, and notices the
caller with "bot is busy - try again shortly". The counter is decremented in a
done-callback, so a task that never finishes never returns its slot; that is
what `_CMD_TIMEOUT` exists to prevent.

`IRCBot._run_cmd()` wraps the handler in
`asyncio.wait_for(..., timeout=self._CMD_TIMEOUT)`, counts
`_metrics["command_timeouts"]`, logs `event=command_timeout`, and tells the
caller. The task slot is then released.

### 1.3 Offloading is a convention, not a mechanism

Nothing enforces that a blocking or CPU-bound handler runs off the loop. The
module contract states it - `internets.py`'s module docstring says a blocking
handler must use `asyncio.to_thread()` - but no loader check, no test, and no
lint rule verifies it. Correctness here rests entirely on the module author
remembering.

`asyncio.wait_for` cannot help. It cancels at an `await` point; a synchronous
call already executing on the loop has no await point to cancel at. So a
handler that breaks the convention is not merely slow: it is unbounded, and it
takes the whole bot with it.

> **Known defect** (`modules/mathx.py - MathxModule.cmd_isprime()`,
> [known-issues](known-issues.md#3-isprime-can-hang-the-entire-bot)): the live
> example of exactly this. `cmd_isprime` calls `_isprime()` directly on the
> loop while the sibling `modules/mathx.py - MathxModule.cmd_bignum()`
> correctly uses `await asyncio.to_thread(_bignum, ...)`. A composite that
> survives the 2^20
> trial-division cap falls into `_pollard_rho`, whose outer loop has no
> iteration bound, and the input cap permits 100 digits
> (`_MAX_ISPRIME_DIGITS`). One message from any user in any channel stalls
> every command from every user, indefinitely, and the 60 s timeout does not
> fire. This is the concrete cost of the convention being unenforced.

### 1.4 The thread pool behind `to_thread`

Nothing in the codebase calls `loop.set_default_executor()` or constructs a
`ThreadPoolExecutor`. `to_thread` therefore uses CPython's default executor,
and **the sizing formula differs across the supported Python range**:

| Python | Default `max_workers` |
| --- | --- |
| 3.10 - 3.12 | `min(32, (os.cpu_count() or 1) + 4)` |
| 3.13 - 3.14 | `min(32, (os.process_cpu_count() or 1) + 4)` |

The two agree on an unrestricted host. They diverge when the process is
constrained: `process_cpu_count()` honors CPU affinity and the
`PYTHON_CPU_COUNT` / `-X cpu_count` override, so a `taskset`-pinned bot on
3.13+ sizes its pool from the CPUs it may actually use while the same bot on
3.12 sizes from the machine's total. Neither reads a cgroup CPU quota, so a
container limited with `--cpus` is sized for the host on every supported
version.

Either way it is a host-dependent number, and on the small hosts this bot
typically runs on it is small (the table reads the same under both formulas
for an unconstrained host):

| Host cores | Worker threads | Task slots |
| --- | --- | --- |
| 1 | 5 | 50 |
| 2 | 6 | 50 |
| 4 | 8 | 50 |
| 8 | 12 | 50 |
| 28 or more | 32 | 50 |

The task cap is a fixed 50 while the pool that serves blocking work is a
function of the hardware. On a 2-core VPS, the seventh concurrent
`to_thread` call queues behind the pool. Nothing reports this: there is no
metric, no log line, and no admin command that shows executor depth. It
presents as unexplained latency.

The blocking work that lands there is real, not hypothetical: `modules/geocode.py`
runs every `requests` call through `asyncio.to_thread`, `internets.py -
_save_shadow_bans()` serializes in a worker, and several modules do the same
for their own JSON stores.

**Weather provider HTTP may or may not consume pool workers, depending on
whether an optional package is installed.** `weather_providers/_http.py` sets
`_HAS_AIOHTTP` from a guarded import at module scope and picks a transport per
call. With `aiohttp` present it is native async on the loop and costs no
worker. Without it, every provider request runs `requests` inside
`asyncio.to_thread` and takes a pool slot for the duration - and `aiohttp` is
the optional `async` extra, not a mandatory dependency
([dependencies](dependencies.md)), so a `pip install internets-irc` with no
extras gets the fallback path.

That flips the sizing conclusion on a keyless-but-busy bot. On a 2-core host
the pool is 6 workers; a weather command on the fallback transport can hold one
for up to the 30 s `_PER_CALL_BUDGET`, so six concurrent weather commands can
saturate the executor that geocoding and every module JSON write also depend
on. Confirm which path a deployment is on before using the table above:
`python -c "import aiohttp"` succeeding is the whole test.

---

## 2. The outbound path

Every reply the bot produces goes through one queue and one token bucket. This
is the binding constraint on the system under most realistic load.

### 2.1 Bounds

| Bound | Value | Symbol |
| --- | --- | --- |
| Queue slots | 200 | `sender.py - Sender.MAX_QUEUE` |
| Burst | 5 | `sender.py - Sender.CAPACITY` |
| Refill | 1 token per 1.5 s | `sender.py - Sender.REFILL` |
| Sustained rate | 40 messages/min | derived from `REFILL` |
| Wire line cap | 512 bytes incl. CRLF | `sender.py - Sender._MAX_IRC_LINE` |

The bucket is **global**, not per-channel and not per-user. Forty messages per
minute is the whole bot's outbound budget across every channel it is in.

Priority 0 bypasses the bucket entirely. That covers protocol traffic only:
`PONG`, `CAP`, `NICK`, `USER`, `PASS`, `AUTHENTICATE`, the keepalive `PING`,
and `QUIT`. Every user-visible reply - `privmsg`, `notice`, `reply`, `preply` -
is priority 1 and is metered.

### 2.2 Under a flood

`sender.py - Sender._safe_put()` handles a full queue asymmetrically, and the
asymmetry is deliberate:

- **Priority 1 full:** log `Send queue full - dropping message` at WARNING,
  count the drop, discard. The user who asked gets nothing and is told
  nothing.
- **Priority 0 full:** find the worst (highest priority, highest sequence)
  entry in the heap, evict it, re-heapify, count that as a drop, and insert
  the protocol message. Losing a `PONG` would cause a server-side ping timeout
  and a reconnect storm, which is worse than losing a reply.

So under sustained overload the bot sheds user replies to stay connected. That
is the right trade, and it means "the bot is still in the channel" is not
evidence that replies are being delivered.

### 2.3 What a large reply does to the budget

`.help all` is the worst case in the shipped command set, and the arithmetic
is instructive.

`admin_cmds.py - cmd_help()` collapses aliases to one name per handler method
and renders the result through `_help_grid(items, cols=4)`.
`admin_cmds.py - _CORE` contributes 4 public names (`_CORE_PUBLIC`) and, for an
authenticated admin, 23 more after `_core_admin_cmds()` collapses aliases.

**Two different module populations give two different answers, and only one of
them describes a shipped bot.** `.help all` enumerates the modules the bot has
actually *loaded*, not the files on disk:

| Population | Files declaring `COMMANDS` | Command names | Distinct handler methods |
| --- | --- | --- | --- |
| Every file in `modules/` | 69 | 202 | 165 |
| The shipped `config.ini.example` autoload | 66 | 195 | 158 |

The 67-entry autoload omits three `COMMANDS`-declaring modules - `example`,
`health` and `privacy` - and includes one that declares none (`linktitle`,
which is passive). Counting from the file population therefore overstates a
default install.

Resolved through the grid, after `sorted(set(...))` dedupes names across
modules:

| Population | Caller | Names | Grid rows | Reply lines |
| --- | --- | --- | --- | --- |
| Shipped autoload | Non-admin | 162 | 41 | 42 |
| Shipped autoload | Admin | 184 | 46 | 47 |
| Every file present | Non-admin | 169 | 43 | 44 |
| Every file present | Admin | 190 | 48 | 49 |

Reply lines are one header plus the grid rows. An admin also gets one more line
when any loaded module is unconfigured (the `(hidden, no key: ...)` row), and
the non-admin figure assumes every module reports `is_configured()`; an
unkeyed install shows fewer names to a non-admin. **Use 42 lines as the shipped
worst case** and 44 only if you have added the three unlisted modules.

At burst 5 (`CAPACITY`) plus 1 line per 1.5 s (`REFILL`), the first 5 lines go
out immediately and the remaining 37 are metered, so a 42-line reply takes
`(42 - 5) x 1.5 s` = about **56 seconds** to reach the wire in full, and
occupies 42 of the 200 queue slots for most of that. The 44-line file-population
case is `(44 - 5) x 1.5 s` = about **59 seconds**. Note the shape of that
expression: the burst is subtracted from the message count before the rate is
applied. Adding the burst to a duration (`5 + 39 x 1.5`) reaches a similar
number by adding a message count to seconds, and does not generalize.

Two consequences follow, and neither is obvious from reading the handler:

1. **The command "completes" almost instantly.** `preply()` enqueues and
   returns; the handler is done and its task slot is released long before the
   user has seen line 10. `_CMD_TIMEOUT` never enters into it, and no metric
   records the delivery delay.
2. **Four concurrent `.help all` calls put about 168 messages in a 200-slot
   queue** on a shipped autoload (4 x 42), or 176 on the full file population.
   A fifth, or any normal channel traffic arriving alongside them, starts
   dropping. There is no per-user output budget and no coalescing.

The same arithmetic applies to any multi-line output. `IRCBot._split_msg()`
encodes the body to UTF-8 and slices it at `_MAX_BODY` = 400 **bytes** (backing
off to a codepoint boundary), so the chunk count is `ceil(len(body_bytes) /
400)`. A 2 KiB response is 2048 bytes and becomes **6** queued messages, not 5;
5 is the answer only for exactly 2000 bytes. Sizes here are binary throughout,
matching the sibling documents. A module that emits one line per result row is
emitting one queue slot per row regardless.

---

## 3. The weather chain

The weather path is the longest-running command class, and it is where the
60 s command timeout comes closest to mattering.

### 3.1 Nested budgets

| Budget | Value | Symbol |
| --- | --- | --- |
| Command timeout | 60 s | `internets.py - IRCBot._CMD_TIMEOUT` |
| Whole fallback chain | 45 s | `weather_providers/_dispatch.py - _CHAIN_BUDGET` |
| Any single provider call | 30 s | `weather_providers/_dispatch.py - _PER_CALL_BUDGET` |
| One HTTP request | 10 s total | `weather_providers/_http.py - _TIMEOUT` |
| Response body | 1 MiB | `weather_providers/_http.py - _MAX_RESPONSE_BYTES` |

`Dispatcher.dispatch()` captures `deadline = time.monotonic() + _CHAIN_BUDGET`
once, before the loop. Each provider gets
`call_timeout = min(remaining, _PER_CALL_BUDGET)`, so time burnt by a slow
provider shrinks what the fallbacks behind it are allowed. When `remaining`
reaches zero the chain stops with
`dispatch_budget_exhausted capability=... budget=45s`.

The 15 s gap between `_CHAIN_BUDGET` and `_CMD_TIMEOUT` is the code's stated
headroom for formatting and the IRC send. The comment in `_dispatch.py` says
exactly this.

### 3.2 The gap the budgets do not cover

**Geocoding happens before the chain and is outside its deadline.**
`modules/weather.py` calls `geocode(...)` and only then reaches the
dispatcher, so the 45 s chain budget starts after geocoding has already
finished.

`modules/geocode.py` bounds its own request count but not its own wall clock:

- Each HTTP call uses `_get(..., timeout=10)`, a `requests` timeout that
  applies per phase (connect, then read), not to the call as a whole.
- A place-name query runs one `featureType=settlement` search plus a free-text
  search, then drops trailing tokens and retries up to `_MAX_DROPS` = 4 more
  times. Worst case is **6 sequential requests**.
- 6 requests at a nominal 10 s each is about **60 s**, which alone equals the
  whole command timeout, before a single provider has been called.

In practice the 24-hour, 1000-entry LRU cache (`_GEOCODE_CACHE_TTL`,
`_GEOCODE_CACHE_MAX`, negative results cached too) makes this rare, and a
transport failure sets the `stop` flag which ends the word-drop loop
immediately. But the worst case is unbounded relative to the command timeout,
and the timeout is the only thing that catches it.

### 3.3 Circuit breaking

`weather_providers/_health.py - ProviderHealth` trips a provider's breaker
after `_CB_THRESHOLD` = 5 consecutive failures inside `_CB_WINDOW` = 60 s, and
refuses calls for `_CB_COOLDOWN` = 60 s. `dispatch()` checks
`rp.health.is_callable()` and skips an open provider without spending chain
budget on it.

A hard 401/403 trips the breaker immediately via `mark_auth_failure()`, so a
dead key costs one call per cooldown rather than one call per command.

The gap: a provider that is **slow but successful** never trips. One answering
in 28 s consumes 28 of the 45 s chain budget on every single command and is
invisible to the breaker, which counts failures rather than latency. See
[service-objectives](service-objectives.md#53-provider-call-latency).

---

## 4. Memory and state

### 4.1 What is held in memory

All persistent state is loaded once and mutated in memory; disk is a periodic
mirror, not the working copy.

| Dataset | Bound | Symbol |
| --- | --- | --- |
| Saved locations | none | `store.py - Store._locs` |
| Rejoin channel list | channel count | `store.py - Store._channels` |
| User tracking | 90-day prune | `store.py - Store._users`, `_USER_MAX_AGE_DAYS` |
| Geocode cache | 1000 entries, 24 h TTL | `modules/geocode.py - _GEOCODE_CACHE_MAX` |
| AccuWeather location keys | 512 entries | `weather_providers/accuweather - _LRU_MAX` |
| Crypto symbol cache | 512 entries | `modules/crypto.py - _CACHE_MAX` |
| Notes | 20 per nick, 200 chars each | `modules/notes.py - _MAX_NOTES`, `_MAX_LEN` |
| Tells | 10 per recipient, 5 per sender | `modules/tell.py` |
| Seen records | 180-day prune | `modules/seen.py - _max_age_days` |

### 4.2 What grows without bound

Three things have no prune and no cap:

- **`Store._locs`.** One entry per nick that has ever registered a location,
  keyed by nick, never expired. The registering commands are `.regloc` and its
  alias `.register_location` (`modules/location.py - LocationModule.cmd_regloc()`);
  `.myloc` reads and `.delloc` removes. There is no `.setloc`. On a busy network
  this grows for the life of the deployment. Removal paths are `.delloc` for the
  owner's own entry and `.forgetme` via `modules/privacy.py`, both of which a
  user has to invoke.
- **`IRCBot._shadow_bans`.** Operator-managed, so bounded by operator
  behaviour rather than by code.
- **The audit chain, in aggregate.** The live segment rotates at
  `audit_log.py - _MAX_BYTES` = 5 MiB, but rotated segments accumulate
  indefinitely; nothing deletes them.

`IRCBot._chanops` is bounded by channel membership and cleared on `001` and on
part. `_authed` and `_auth_fails` are bounded by
`_AUTH_CLEANUP_THRESHOLD` = 50, which triggers an expiry sweep. Neither is a
growth concern.

The application log rotates at `config.py - LOG_MAX` (5 MB default) keeping
`LOG_BACKUPS` (3 default) segments, so it is bounded at roughly 20 MB per
stream.

### 4.3 The read cap and the flush interval

`store.py - Store._MAX_FILE_SIZE` = 10 MiB applies **per state file** at read
time. A file over the cap raises `_StoreRejected`, and
`Store._quarantine()` renames it to `<name>.corrupt.<epoch>` and starts from
empty. That is a data-loss event with a single ERROR line, and it is
size-triggered rather than corruption-triggered, so a legitimately large
dataset hits the same path as a damaged one.

`store.py - _FLUSH_INTERVAL` = 30 s. The `store-flush` thread wakes on that
interval and writes any dataset whose dirty flag is set. Each write serializes
the **entire** dataset, computes a SHA-256 over the canonical JSON, writes a
temp file, and replaces. There is no incremental write and no `os.fsync`
anywhere in the codebase
([known-issues](known-issues.md#12-concurrency-and-durability-gaps)).

The consequence for capacity: cost per flush is proportional to total dataset
size, not to the number of changes. A 5 MB users file that gets one new entry
re-serializes and re-hashes 5 MB every 30 s for as long as it stays dirty.

`Store._flush_loop()` catches every exception so a failed flush cannot kill the
persistence thread. Dirty flags stay set and the next cycle retries. This is
correct, and it means a persistently failing flush is visible only as a
repeating `Store flush failed` ERROR line, never as a stopped process.

---

## 5. Capacity guidance

**These are estimates derived from the constants above, not measurements.** No
load test has been run. The assumptions are listed so you can substitute your
own.

### 5.1 Assumptions

| # | Assumption |
| --- | --- |
| A1 | Sustained outbound ceiling is 40 messages/min bot-wide (`REFILL` = 1.5 s) |
| A2 | A typical command produces 1 to 3 reply lines after `_split_msg()` |
| A3 | Per-nick flood gate is 3 s (`config.py - FLOOD_CD`), so 20 commands/min/nick |
| A4 | Per-channel gate is 20 commands per 10 s window (`store.py - RateLimiter`), so 120/min/channel |
| A5 | Typical handler wall time is 1 to 3 s; the weather path is up to 45 s |

### 5.2 Which limit binds first

Compare the gates. The per-channel gate allows 120 commands/min in a single
channel and the per-nick gate allows 20/min per nick, but the outbound bucket
allows only 40 **messages**/min in total. At A2, that is 13 to 40
commands/min across the entire bot.

**The outbound token bucket is the binding constraint under normal load, by a
factor of 3 to 9 over the inbound gates.** Every inbound rate limit in the
system is looser than the outbound one. Two moderately active users are enough
to saturate the bot's whole outbound budget.

Task capacity binds only when handlers are slow. Applying Little's law to the
task pool, a sustained arrival rate `R` with mean handler time `T` needs
`R x T <= 50`:

| Mean handler time | Arrival rate at the 50-slot cap |
| --- | --- |
| 2 s (A5 typical) | 25 commands/s |
| 10 s | 5 commands/s |
| 45 s (chain-budget worst case) | about 1.1 commands/s |

25 commands/s is roughly 37 times the outbound ceiling, so at typical handler
times the task cap is unreachable. At the weather worst case it is about 66
commands/min, which is still above the outbound ceiling but close enough that a
provider brownout plus a burst of weather queries can reach it. That matches
the shape of the `event=dispatch_rejected` alert in
[service-objectives](service-objectives.md#23-task-capacity-rejections):
if you see it, suspect slow handlers, not high demand.

### 5.3 By channel count

Channel count is not itself a load variable. What scales is total inbound
message volume and the per-message work the loaded passive modules do.

| Deployment | Estimate | Reasoning |
| --- | --- | --- |
| 1 to 10 channels, low traffic | Comfortable | Outbound demand well under 40 msg/min; inbound parse cost negligible |
| 10 to 30 channels, mixed traffic | Workable, watch outbound | Bursts across channels contend for one global bucket; drops appear before anything else does |
| 30 or more channels, or any high-traffic channel | Outbound-bound | 40 msg/min divided across 30 channels is roughly 1.3 msg/min/channel; passive modules that emit per message (for example `linktitle`) consume that budget with no command involved |

The inbound path itself is cheap: one `readline()` with an 8192-byte limit
(`_READ_LIMIT`), a handful of compiled-regex matches, and a dict lookup.

The part that scales with what you have loaded is the `on_raw` fan-out.
`internets.py - IRCBot._process()` iterates a snapshot of **every** loaded
module instance and calls `inst.on_raw(line)` synchronously on the loop, for
every inbound line. `modules/base.py - BotModule.on_raw()` is a no-op, so with
the shipped 67-module autoload that is roughly 67 method calls per line, of
which four do real work: `channels`, `linktitle`, `seen`, and `tell` are the
only modules that override it.

Those four are where the real per-message cost sits. `seen` regex-matches every
line and updates its in-memory record plus a dirty flag on each channel
PRIVMSG (the disk write is on its own save cadence, not per message).
`linktitle` fetches and announces URLs, which spends outbound budget and
executor workers with no command involved. Neither the fan-out nor the
per-module cost is a plausible constraint at any channel count a
single-operator bot reaches, but it is the term that grows, and it is all on
the loop.

### 5.4 By command rate

| Sustained rate | Expected behaviour |
| --- | --- |
| Under 13 commands/min | No contention under any assumption in 5.1 |
| 13 to 40 commands/min | Contention depends on reply length; multi-line replies start queueing |
| 40 to 120 commands/min | Queue grows, delivery latency climbs, drops begin once 200 slots fill |
| Over 120 commands/min | The per-channel gate starts refusing before the queue does, silently (log only) |

None of this has been observed. It is arithmetic over `REFILL`, `MAX_QUEUE`,
`FLOOD_CD`, and `_CHANNEL_DEFAULT_BURST`.

---

## 6. The load test that should be run

The estimates above are the best that can be done by reading. Replacing them
needs a test, and the test is not large.

### 6.1 What to drive

Run against a local test ircd, never a production network - the traffic
patterns below would look like an attack, and the bot's outbound behaviour
under them is exactly what the test is measuring.

1. **Loop-bound baseline.** Commands with no external network dependency
   (`.version`, `.roll`, `.calc`) at a rising rate. This measures dispatch,
   the sender, and the loop in isolation.
2. **Blocking-path load.** Commands that go through `asyncio.to_thread`, driven
   past the executor's worker count (5 to 8 on a typical host), to characterize
   the pool queueing described in section 1.4 above.
3. **Provider-path load.** The weather commands against a **stubbed** provider
   endpoint with injected latency, including a case at 28 s (under
   `_PER_CALL_BUDGET`, so the breaker never trips) to reproduce the slow-but-
   successful gap in [3.3](#33-circuit-breaking).
4. **Output-volume load.** Concurrent `.help all` calls, 1 through 8, to find
   where the 200-slot queue starts dropping.
5. **Disconnect under load.** Kill the link while the queue is deep, to
   measure how much queued output is discarded uncounted on reconnect
   (see [7.3](#73-queue-saturation-while-disconnected)).
6. **Startup with a large state file.** A synthetic users file at 1, 5, and
   9.9 MB, to measure startup delay and per-flush cost.

### 6.2 What to measure

- End-to-end reply latency: PRIVMSG sent to reply observed on the wire.
  Percentiles, not means; the mean will hide the bucket.
- `internets_dropped_messages_total` and the `Send queue full` log count.
- `event=dispatch_rejected` and `event=command_timeout` counts.
- High-water mark of `_active_cmd_tasks` (needs the gauge proposed in
  [service-objectives](service-objectives.md#23-task-capacity-rejections)).
- Event-loop lag, sampled by a fixed-interval task. This is the measurement
  that distinguishes "slow network" from "blocked loop" and it does not exist
  yet.
- RSS from `.stats`, sampled through the run and for several minutes after
  load stops.
- Flush duration and count from the `store-flush` thread.

### 6.3 What would indicate the limit

- **First drop.** The first `Send queue full` line is the outbound saturation
  point and the most defensible single capacity number this system has.
- **First rejection.** The first `event=dispatch_rejected` is the task-capacity
  point. If it arrives before the first drop, handlers are the constraint, not
  the bucket, which inverts the conclusion in [5.2](#52-which-limit-binds-first).
- **Latency that keeps climbing after load stops.** Backlog draining at 40
  msg/min. Recovery time is a capacity number in its own right.
- **Event-loop lag above a few tens of milliseconds.** Something is running on
  the loop that should not be. This is the [1.3](#13-offloading-is-a-convention-not-a-mechanism)
  failure class being caught by instrumentation instead of by a user.
- **RSS that does not plateau across repeated identical runs.** Distinguish the
  bounded caches (which should plateau at their documented sizes) from
  `Store._locs`, which should not, by construction.

---

## 7. Known degradation modes

Each entry names the mechanism, not just the symptom, because the symptoms
overlap heavily: three of the four present as "the bot stopped answering".

### 7.1 An unoffloaded handler stalls the loop

**Mechanism.** One event loop. A handler that runs synchronous CPU-bound or
blocking code holds it. Nothing else runs: not other commands, not the read
loop, not the keepalive `PING`, not the sender drain.

**Progression.** Commands stop completing. The server's `PING` goes unanswered
because `PONG` cannot be written. After the server's ping timeout the link
drops. The bot reconnects, clearing authenticated admin sessions and
discarding the queue. If the triggering command is re-issued, it repeats.

**Detection today.** None specific. It presents as a reconnect, so
`internets_reconnects_total` moves and the cause is not recorded anywhere.

**Live example.** `.isprime`, [1.3](#13-offloading-is-a-convention-not-a-mechanism).

### 7.2 A slow provider chain holds a task slot

**Mechanism.** Each in-flight weather command holds one of the 50 slots for the
duration of geocoding plus the chain, up to 45 s of chain budget and
potentially more geocode time in front of it ([3.2](#32-the-gap-the-budgets-do-not-cover)).
A provider that is slow but not failing does not trip its breaker
([3.3](#33-circuit-breaking)), so the cost is paid on every command.

**Progression.** Slots fill. `_dispatch()` starts refusing with "bot is busy",
which affects **all** commands including admin ones, not only the weather
commands that caused it. Users experience an outage caused by an upstream they
cannot see.

**Detection today.** `event=dispatch_rejected` at the moment of breach. There
is no view of the approach: neither `.stats` nor any metric reports
`_active_cmd_tasks`.

### 7.3 Queue saturation while disconnected

**Mechanism.** `Sender.stop()` cancels the drain task but the queue object
survives, and `Sender.enqueue()` keeps accepting through
`loop.call_soon_threadsafe`. Modules with timers or passive handlers keep
producing output into a queue nothing is draining. At 200 items, priority-1
messages start being dropped and counted.

**Then it gets worse on recovery.** `IRCBot._connect()` constructs a **new**
`Sender`, and `Sender.start()` assigns a **new** `PriorityQueue`. Everything
still queued from the disconnected period is garbage-collected, not dropped
through `_safe_put()`, so it is **never counted**. The drop counter reports
only the overflow that occurred before the queue filled.

**Detection today.** `Send queue full - dropping message` WARNING lines and
`internets_dropped_messages_total`, both of which undercount. See
[service-objectives](service-objectives.md#22-outbound-queue-drops).

### 7.4 A large state file at startup

**Mechanism.** `Store.__init__()` reads all three state files synchronously.
`IRCBot()` is constructed inside `_main()` under `asyncio.run`, so this runs on
the loop thread before the connection is opened.

**Progression, by size:**

| Size | Behaviour |
| --- | --- |
| Well under 10 MiB | JSON parse plus a SHA-256 over the payload; startup delay proportional to size |
| Just under 10 MiB | Same, plus a full re-serialize and re-hash on **every** 30 s flush while dirty |
| Over 10 MiB | `_StoreRejected`, quarantined to `<name>.corrupt.<epoch>`, bot starts with that dataset **empty** |

The third row is the one to watch. It is a silent-to-users data-loss event
announced by one ERROR line, it is triggered by size rather than by damage, and
because the bot then starts from empty, the next flush writes an empty dataset
over the (now renamed) real one. Recovery means noticing the quarantine file.

Startup is the only time this blocks the loop; the periodic flushes run on the
`store-flush` thread. But the per-flush cost is proportional to total dataset
size, so a large file is a permanent tax rather than a one-time startup cost.

---

## 8. Related reading

- [service-objectives](service-objectives.md) - what to alert on, and the
  instrumentation that would make these modes detectable.
- [architecture](architecture.md#6-concurrency-model) - locks, threads, and
  cross-thread signalling.
- [state-and-persistence](state-and-persistence.md) - the state files, their
  envelope format, and the quarantine path.
- [providers](providers.md) - the dispatcher, ranking, and the provider set.
- [troubleshooting](troubleshooting.md) - diagnostics for the symptoms above.
- [known-issues](known-issues.md) - the verified defects referenced here.
- [internals/sender](internals/sender.md), [internals/store](internals/store.md),
  [internals/internets](internals/internets.md) - line-level detail.
