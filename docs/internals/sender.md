# sender.py - outbound IRC pipeline: priority queue, flood control, line serialization

## Purpose

`sender.py` owns everything between "the bot decided to send a line" and "bytes were
handed to the transport": priority assignment, a bounded send queue, token-bucket flood
control, IRC line sanitization/truncation, credential redaction of the debug log, and the
buffered write + drain to the `asyncio.StreamWriter`. It exists so that no other component
touches the writer directly and so flood control is enforced in exactly one place.

The module also hosts `redact_secrets()`, the single source of truth for credential
redaction in logs. It lives here rather than in `internets.py` because the outbound log
line (`>> ...`) is written here, but `internets.py` imports it for the inbound direction
too (`internets.py - _redact_inbound()`), so one verb list covers both directions.

## Responsibilities / boundaries

Belongs here:

- Priority semantics (0 = protocol, 1 = normal) and their ordering guarantee.
- Queue bounding and overflow policy (`Sender._safe_put()`).
- Token-bucket rate limiting (`Sender._drain()`).
- Wire-safety of a single line: CR/LF/NUL stripping, 512-byte cap, UTF-8-safe
  truncation (`Sender._write_line()`).
- Log-only credential redaction (`redact_secrets()`).
- Drop accounting hooks (`_bump_dropped()`, the `on_drop` callback).

Deliberately not here:

- Message splitting into multiple lines - `internets.py - IRCBot._split_msg()` chunks a
  long PRIVMSG/NOTICE body *before* it reaches the sender; the sender's truncation is a
  last-resort hard cap, not the normal wrapping path.
- Target validation (`IRCBot.privmsg()` / `IRCBot.notice()` reject targets containing
  spaces before calling `send()`).
- Connection lifecycle - `internets.py` creates a fresh `Sender` per connection and
  decides when to stop it.
- TLS gating of credentials (`internets.py - IRCBot._tls_or_refuse()`).

## Dependencies and dependents

Dependencies:

- stdlib only at import time: `asyncio`, `logging`, `re`, `threading`.
- `metrics.registry` - imported lazily inside `_bump_dropped()` so a metrics failure can
  never affect sending.
- `heapq` - imported lazily inside the eviction branch of `_safe_put()`.

Dependents:

- `internets.py` - constructs `Sender` in `IRCBot._connect()` (one per connection,
  `internets.py:777-779`), funnels all output through `IRCBot.send()` ->
  `Sender.enqueue()`, and imports `redact_secrets` for inbound log redaction.
- `tests/test_sender.py` - exercises the whole module against a fake `StreamWriter` with
  a real event loop and real queue.

## Lifecycle

1. Imported once at bot startup (`internets.py` top-level import).
2. `IRCBot._connect()` stops any previous sender, constructs `Sender(loop,
   on_drop=IRCBot._bump_dropped_metric)`, then calls `start(writer)`.
3. `start()` replaces the queue, resets the sequence counter, and spawns the `_drain()`
   task (named `"sender"`).
4. On reconnect the old instance is stopped and a new one is built; on shutdown
   `IRCBot.graceful_shutdown()` enqueues `QUIT` at priority 0, sleeps
   `IRCBot._SHUTDOWN_DRAIN_S` (2.0 s), then awaits `Sender.stop()`.
5. There is no destructor; abandoned instances are garbage-collected once their drain
   task is cancelled.

## State

All state is in-memory and per-instance; nothing is persistent.

| Field | Type | Notes |
|---|---|---|
| `_loop` | event loop | captured at construction; used for `call_soon_threadsafe` |
| `_q` | `asyncio.PriorityQueue[(int, int, str)]` | bounded at `MAX_QUEUE` (200); replaced on `start()` |
| `_seq` | `int` | monotonically increasing tiebreaker; guarded by `_seq_lk` |
| `_seq_lk` | `threading.Lock` | protects `_seq` against concurrent `enqueue()` |
| `_writer` | `StreamWriter | None` | the transport; only read on the event loop |
| `_task` | `Task | None` | the drain task |
| `_on_drop` | callback or None | the bot's in-process dropped-message counter |

The token bucket (`tokens`, `last`) is local state of the `_drain()` coroutine, not
instance state; it resets to a full bucket every `start()`.

## Concurrency

- `enqueue()` is the only cross-thread entry point. Module command handlers run in
  worker threads (`internets.py - IRCBot._run_cmd()` awaits handlers that use
  `asyncio.to_thread`), so `enqueue()` takes `_seq_lk` for the sequence number and hands
  the actual queue mutation to the event loop via `loop.call_soon_threadsafe(_safe_put)`.
  `asyncio.PriorityQueue` itself is not thread-safe; this design means it is only ever
  touched on the loop thread.
- `_safe_put()`, `_drop()`, `_write_line()`, and `_drain()` all run on the event-loop
  thread; no locks are needed among them.
- Ordering guarantee: items are `(priority, seq, msg)` tuples in a priority queue, so
  dequeue order is priority-0 before priority-1, and FIFO (by `seq`) within a priority.
  `tests/test_sender.py - TestSafePut.test_priority_ordering` and
  `TestDrainRateLimiting.test_priority0_bypasses_bucket_and_orders_first` pin both
  properties.
- Cross-thread ordering caveat: `seq` is assigned in `enqueue()` under the lock, but the
  `call_soon_threadsafe` callbacks from different threads are serialized by the loop, and
  seq order matches lock-acquisition order, so within-priority ordering holds even for
  concurrent producers.
- `start()` resets `_seq` under `_seq_lk`; a worker thread calling `enqueue()` during a
  reconnect can race the queue swap (its `_safe_put` may land on the old, now-orphaned
  queue object). The message is silently lost with no drop accounting. In practice the
  window is the two adjacent statements in `IRCBot._connect()`.

## Failure behavior

- Queue full, priority 1: the *new* message is dropped, a warning is logged, and
  `_drop()` runs (Prometheus counter + bot callback). See `Sender._safe_put()`.
- Queue full, priority 0: the worst-ranked existing entry (largest `(priority, seq)`,
  i.e. the lowest-priority, most-recently-enqueued message) is evicted from the heap,
  `heapq.heapify` restores the invariant, `_drop()` is called for the evicted message,
  and the priority-0 item is inserted. Rationale in the docstring: dropping a PONG causes
  a ping-timeout disconnect and a reconnect storm, strictly worse than losing one chat
  line. If eviction itself fails, an error is logged loudly ("UNABLE to enqueue
  priority-0 message") rather than failing silently.
- Dead / closing transport: `_write_line()` checks `writer.is_closing()` and skips the
  write; `_drain()` likewise skips `writer.drain()`. Any exception from `write()` or
  `drain()` is caught and logged at WARNING; the drain loop never dies from a transport
  error. Consequence: while the transport is closing, the queue keeps draining and
  messages are discarded (see Findings).
- Drop accounting is double-entry: `_bump_dropped()` increments the Prometheus
  `dropped_messages_total` (best-effort, exception-swallowed), and `_on_drop` increments
  `IRCBot._metrics["dropped_messages"]` so the shutdown summary reports a real number
  (`internets.py - IRCBot._bump_dropped_metric()`). A raising callback is swallowed
  (`tests/test_sender.py - TestDropAccounting.test_drop_swallows_callback_exception`).
- `stop()` cancels the drain task and swallows the `CancelledError`; messages still in
  the queue at that point are abandoned without drop accounting. Shutdown compensates
  with a fixed 2.0 s drain window before stopping, not a drain-to-empty.

## Security

- Protocol injection: `_write_line()` strips embedded `\r`, `\n`, and `\x00` before
  writing, so a module cannot smuggle a second IRC command (e.g. `QUIT`) inside one
  message. `tests/test_sender.py - TestWriteLine.test_strips_cr_lf_nul_injection` pins
  the collapse onto one line.
- Line-length enforcement: the encoded line is capped at 510 bytes + CRLF (RFC 2812
  limit of 512 including the terminator), with continuation-byte backoff so a multi-byte
  UTF-8 character is never split (`TestWriteLine.test_truncates_multibyte_without_overrun`).
- Credential redaction is LOG-ONLY: the wire always gets the full line
  (`TestRedaction.test_secret_redacted_in_log` asserts both halves). `redact_secrets()`
  masks everything after the first credential verb (`AUTHENTICATE`, `IDENTIFY`,
  `REGISTER`, `IDENT`, `OPER`, `PASS`, `AUTH`), case-insensitively, word-boundaried,
  longest-verb-first, keeping the verb and replacing the remainder of the line with
  `[REDACTED]`. Because the match runs to end of line (`\S.*`), a second credential later
  in the same line is masked too. A bare verb with no argument is left alone.
- Applied at: (a) the outbound debug log in `_write_line()`; (b) inbound
  PRIVMSG/NOTICE trailing text via `internets.py - _redact_inbound()` (scoped to the
  trailing so `ident@host` in a prefix cannot false-match `IDENT`); (c) command audit
  logging (`internets.py:1149-1150`), which fully masks `auth`/`deauth` arguments and
  runs `redact_secrets` over every other command argument (notably `.raw`).
- Known over-redaction (documented in the module comment): a standalone verb in relayed
  chat text ("pass", "auth") masks the rest of the logged line; accepted as the safe side.
- `redact_secrets` on a full inbound line is unsafe (a `ident@host` prefix would match);
  the docstring pushes that scoping duty onto the caller, which `_redact_inbound`
  honors.

## Classes

### `Sender`

- **Responsibility**: sole owner of the outbound path from queue to transport.
- **Lifecycle**: one instance per connection; `start()` may only be called from the
  event loop; `enqueue()` from any thread; `stop()` from the event loop.
- **Constructor**: `Sender(loop, on_drop=None)`. Takes the loop explicitly because
  `enqueue()` needs it from foreign threads where `get_running_loop()` is unavailable.
- **Class constants**: `CAPACITY = 5` (burst tokens), `REFILL = 1.5` (seconds per
  token, ~40 msg/min sustained), `MAX_QUEUE = 200` (queue bound, tagged BUG-056: prevent
  OOM during disconnects), `_MAX_IRC_LINE = 512` (RFC 2812 cap including CRLF, tagged
  BUG-026).
- **Invariants**: queue mutated only on the loop thread; queue size never exceeds
  `MAX_QUEUE`; priority-0 items are enqueued even at capacity (via eviction); `_seq`
  strictly increases between `start()` calls.
- **Extension constraints**: the eviction branch reaches into
  `asyncio.PriorityQueue._queue` (a CPython implementation detail); a Python upgrade
  that changes the internal attribute breaks eviction, which then degrades to the loud
  "UNABLE to enqueue" error path rather than crashing.

## Functions and methods

### `redact_secrets(text) -> str` (module-level)

Masks the argument after the first credential verb; see Security. Pure function, no
state. Callers: `Sender._write_line()`, `internets.py - _redact_inbound()`,
`internets.py` command audit logging. `count=1` substitution; sufficient because the
mask consumes the rest of the line.

### `_bump_dropped()` (module-level)

Best-effort increment of `metrics.registry.dropped_messages_total`. Lazy import,
blanket `except Exception: pass` - a metrics failure must never affect sending. Called
only from `Sender._drop()`.

### `Sender.__init__(loop, on_drop=None)`

Builds the bounded queue, the sequence lock, and stores the callback. No side effects
beyond allocation.

### `Sender.start(writer)`

Event-loop only. Stores the writer, **replaces** the queue with a fresh bounded one
(discarding anything enqueued before start - no drop accounting), resets `_seq` under
the lock, and creates the `_drain()` task. Restart semantics verified by
`tests/test_sender.py - TestLifecycle.test_start_creates_task_and_resets_state`.

### `Sender.stop()` (async)

Cancels the drain task if present, awaits it, swallows `CancelledError`, clears
`_task`. Idempotent; a never-started sender is a no-op
(`TestLifecycle.test_stop_without_start_is_noop`).

### `Sender._drop()`

Runs on the loop thread from `_safe_put()`. Calls `_bump_dropped()` then the bot
callback, each independently exception-guarded.

### `Sender._safe_put(item)`

Loop-thread queue insert with the overflow policy described under Failure behavior.
The eviction algorithm: linear scan of the heap for the max `(priority, seq, msg)`
tuple, `list.pop` at that index, `heapq.heapify` to restore the heap invariant, then
`put_nowait` of the priority-0 item. O(n) at n=200, only on the already-exceptional
full-queue path. Note that tuple comparison falls through to the message string when
priority and seq tie, which cannot happen (seq is unique).

### `Sender.enqueue(msg, priority=1)`

The public, thread-safe producer API. Assigns `seq` under `_seq_lk`, then schedules
`_safe_put` onto the loop. Does not block and reports no result; a drop is only
observable through the counters. If the loop is already closed (late-shutdown race),
`call_soon_threadsafe` raises `RuntimeError` into the caller; the one shutdown-path
caller wraps `send()` in try/except (`internets.py - IRCBot.graceful_shutdown()`).

### `Sender._write_line(msg)`

Synchronous buffer-only write (no await): strips CR/LF/NUL, UTF-8-encodes with
`errors="replace"`, hard-caps at 510 bytes with continuation-byte backoff, logs
`>> {redact_secrets(msg)}` at DEBUG, and writes `msg + "\r\n"` if the writer exists and
is not closing. Transport exceptions are caught and logged at WARNING.

### `Sender._drain()` (async)

The consumer loop. Algorithm:

1. Start with a full bucket (`tokens = CAPACITY`, clock = `loop.time()`).
2. `wait_for(q.get(), timeout=0.25)`; on timeout, refill tokens
   (`min(CAPACITY, tokens + elapsed / REFILL)`) and loop - so the bucket refills while
   idle (`TestDrainRateLimiting.test_idle_refill_then_send`).
3. On an item: refill first, then if priority > 0, poll-wait in 50 ms sleeps until
   `tokens >= 1.0` (refilling inside the wait), then spend one token. Priority 0 skips
   the bucket entirely.
4. `_write_line(msg)`, then `await writer.drain()` (guarded, exceptions logged).

Effective policy: 5-line burst, then one line per 1.5 s (~40 msg/min sustained) for
priority-1 traffic; unlimited rate for priority 0. Pinned by
`TestDrainRateLimiting.test_priority1_burst_then_throttle` (exactly 5 written of 6 in
0.4 s) and `test_priority0_bypasses_bucket_and_orders_first` (20 priority-0 plus the
5-token burst).

## Implementation walk

- `sender.py:1-11` (module docstring): states the priority scheme, bucket parameters,
  and thread-safety contract. Matches the implementation.
- `sender.py:20-42` (redaction constants): comment block explaining the single-verb-list
  design and both directions of use; `_SECRET_VERBS` tuple; `_RE_SECRET` built
  longest-first so `IDENTIFY` beats `IDENT` and `AUTHENTICATE` beats `AUTH` at the same
  position. Security enforcement.
- `sender.py:45-54` (`redact_secrets`): formatting/security; one `re.sub` with
  `count=1`.
- `sender.py:56` (logger): the `internets.sender` child logger, targetable by the
  console's per-subsystem debug.
- `sender.py:59-69` (`_bump_dropped`): error-isolated metrics bump; lazy import keeps
  metrics optional.
- `sender.py:72-94` (`Sender` class header, constants, `__init__`): initialization;
  constants documented above.
- `sender.py:96-112` (`start` / `stop`): lifecycle; queue replacement and task
  cancel.
- `sender.py:114-126` (`_drop`): drop accounting, both counters, both guarded.
- `sender.py:128-170` (`_safe_put`): the overflow policy; the only place the queue
  bound is felt. Contains the deliberate private-attribute access (`_q._queue`) with
  its justification comment.
- `sender.py:172-178` (`enqueue`): thread-safe producer; lock-then-schedule.
- `sender.py:180-203` (`_MAX_IRC_LINE`, `_write_line`): protocol formatting and
  security enforcement (injection strip, byte cap, log redaction), then the guarded
  buffered write.
- `sender.py:205-240` (`_drain`): the token bucket and consumer loop; protocol
  processing and flow control. The 0.25 s `get` timeout exists solely to run the
  refill branch while idle so a post-idle burst has a full bucket.

## Findings

- questionable | sender.py - Sender._safe_put() | When the queue is full of 200
  priority-0 items (pathological, e.g. a wedged transport during a PING storm), a new
  priority-0 item evicts the highest-seq priority-0 entry, so the "never drop
  priority-0" guarantee is actually "never drop the *oldest* priority-0"; benign in
  practice, but the docstring overstates the guarantee.
- questionable | sender.py - Sender._safe_put() | Eviction depends on the private
  CPython attribute `asyncio.PriorityQueue._queue`; the code acknowledges this and
  degrades loudly if it breaks, but it is a Python-version hazard worth a canary test.
- questionable | sender.py - Sender._drain() / Sender._write_line() | While the writer
  is closing, the drain loop keeps consuming and silently discarding messages (and still
  spends priority-1 tokens) with no drop accounting; `tests/test_sender.py -
  TestDrainRateLimiting.test_closing_writer_skips_flush` pins the discard, but the
  dropped-message counters never see these losses.
- questionable | sender.py - Sender.start() | Replacing `_q` discards any items enqueued
  between construction and `start()` (or racing a reconnect) without drop accounting;
  the live wiring in `internets.py - IRCBot._connect()` makes the window two adjacent
  statements, so this is latent rather than live.
- questionable | sender.py - Sender._drain() | The token wait is a 50 ms poll loop
  rather than an event/computed sleep; wakes up to 30 times per queued message during
  throttling. Cheap, but a computed `await asyncio.sleep(needed)` would be exact.
- test-gap | sender.py - Sender.enqueue() | No test covers `enqueue()` after the loop is
  closed (the `RuntimeError` path from `call_soon_threadsafe`); the shutdown caller
  guards it, module worker threads do not.
