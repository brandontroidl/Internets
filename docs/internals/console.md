# console.py - interactive stdin console on a daemon thread

## Purpose

An operator console read from the bot's own stdin, running alongside the IRC connection.
It dispatches four command families - debug toggles, log-level changes, a status dump,
and graceful shutdown - without requiring IRC access or authentication. The file's most
load-bearing content is not the dispatch loop but the threading decision documented in
`run_console()`: why the blocking `input()` lives on a raw daemon `threading.Thread` and
not `asyncio.to_thread`.

## Responsibilities / boundaries

Belongs here:

- The TTY gate (`should_skip_console()`).
- The blocking read + dispatch loop (`_console_dispatch_loop()`).
- The async wrapper that bridges the thread to the event loop (`run_console()`).
- The status dump (`_print_status()`).

Deliberately not here:

- Whether the console runs at all - `internets.py - _main()` combines the operator
  opt-out (`--no-console`) with `should_skip_console()` (`internets.py:1416-1420`).
- The semantics of `debug` / `loglevel` - `botlog.py - apply_debug()` /
  `apply_loglevel()`, shared with the IRC-side admin commands so both surfaces mutate
  the same `botlog.log_filter`.
- Shutdown mechanics - `internets.py - IRCBot.request_shutdown()` /
  `graceful_shutdown()`; the console only requests.
- Unblocking the `input()` call at shutdown - `internets.py - _main()` closes
  `sys.stdin` (`internets.py:1454-1460`), which makes `input()` raise `EOFError`.

## Dependencies and dependents

Dependencies:

- `botlog` - `apply_debug`, `apply_loglevel`, `log_filter` (the process-global
  `DebugFilter`).
- `config` - `__version__` for the status line.
- `internets.IRCBot` - type-only import (`TYPE_CHECKING`); at runtime the bot instance
  is passed in, avoiding a circular import.
- stdlib: `asyncio`, `logging`, `sys`, `threading` (+ an inline `__import__("os")` for
  the pid in the startup warning).

Dependents:

- `internets.py - _main()` imports `run_console` and `should_skip_console` and owns the
  gating, task creation, and shutdown coupling.
- No test file imports console.py (see Findings).

## Lifecycle

1. `main()` creates the `run_console(bot)` task (named `"console"`) only if
   `--no-console` was not given AND stdin is an interactive TTY.
2. `run_console()` logs a loud `event=console_active` warning, spawns the
   `console-input` daemon thread, and awaits an `asyncio.Event`.
3. The thread loops in `_console_dispatch_loop()` until EOF, Ctrl-C, a closed stdin, or
   a `shutdown`/`quit` command, then sets the event via `call_soon_threadsafe`.
4. When `run_console()` returns for any reason, `main()`'s
   `asyncio.wait(..., FIRST_COMPLETED)` fires and it calls
   `bot.request_shutdown("Console exited")` - console exit is coupled to bot shutdown,
   so Ctrl-D on the console terminates the whole bot, by design
   (`internets.py:1423-1428`).
5. Conversely, when the *bot* exits first, `main()` closes `sys.stdin` to unblock the
   `input()` syscall and cancels the console task with a 3 s timeout; if the thread
   still never returns, `daemon=True` means it cannot hold up interpreter exit.

## State

Owns none. Reads process-global logging state (`botlog.log_filter`) and bot state
(`_nick`, `active_channels`, `_modules` under `_mod_lock`, `_authed` under
`_auth_lock`). Mutates only logging state (via the `botlog` helpers) and, indirectly,
the bot's stop event (via `request_shutdown`). Nothing persistent.

## Concurrency

- Two execution contexts: the async `run_console()` coroutine on the event loop, and the
  `console-input` daemon thread doing all reads and dispatches.
- Why a raw daemon thread and not `asyncio.to_thread` (docstring of `run_console()`,
  corroborated by the shutdown comment in `internets.py:1439-1453`): `input()` parks the
  thread on a blocking `read(0)` syscall that task cancellation cannot interrupt. A
  `to_thread` worker runs on the default executor as a *non-daemon* thread, and
  `asyncio.run()`'s cleanup calls `loop.shutdown_default_executor()`, which waits for
  all executor workers - so the process would hang on the final shutdown log line until
  the operator pressed Ctrl-C (the observed failure of the previous design). A daemon
  thread dies with the process, so cleanup completes.
- Thread-to-loop signaling: the thread never touches the `asyncio.Event` directly; it
  schedules `done.set` with `loop.call_soon_threadsafe`, guarded against `RuntimeError`
  for the loop-already-closed race.
- Thread safety of the dispatched work (docstring of `_console_dispatch_loop()`):
  `apply_debug`/`apply_loglevel` mutate logger state, which the `logging` module guards
  with its own RLocks; `request_shutdown()` uses `call_soon_threadsafe` internally;
  `_print_status()` takes `bot._mod_lock` and `bot._auth_lock` for the dict/set reads.
  Two reads are *not* lock-guarded: `bot._nick` (a bare attribute read, written on the
  loop thread on nick changes) and the `log_filter` fields; both are single-reference
  reads that are atomic in CPython, so this is benign, but the docstring's blanket
  "lock-guarded accessors" claim is broader than the code (see Findings).
- Output interleaving: `print()` to the same terminal the log handler writes to; a
  status dump can interleave with log lines. Cosmetic only.

## Failure behavior

- `EOFError` / `KeyboardInterrupt` / `ValueError` from `input()` end the loop cleanly
  (return, event set, bot shutdown follows). `ValueError` covers stdin closed mid-read.
- Any other exception in the dispatch loop is caught by `_wrap()` inside
  `run_console()`, logged with `log.exception`, and still sets the completion event in
  `finally` - a crashed console therefore also shuts the bot down (same path as a
  deliberate exit). The implementation implies this is accepted: there is no restart
  logic.
- Unknown commands print a hint; `apply_loglevel` returns an error string that is
  printed rather than raised.
- `should_skip_console()` fails safe: `AttributeError` (no `isatty`) or `ValueError`
  (closed stdin) both return True, i.e. no console.

## Security

Trust boundary: possession of the bot process's controlling TTY. There is no
authentication; the module docstring states the model - anyone with local stdin access
can already kill the process or read `config.ini`, so the console adds no *new* attack
surface in a single-user context, but it MUST NOT run with stdin shared with an
untrusted user (`--no-console` for daemonized deployments).

What it actually grants (verified against the dispatch table in
`_console_dispatch_loop()`):

- `debug` / `loglevel` - process-wide log verbosity, including per-subsystem DEBUG.
  Outbound credential lines are redacted before logging
  (`sender.py - redact_secrets()`), so enabling DEBUG does not expose IDENTIFY/PASS
  arguments, subject to that redaction's coverage.
- `status` - discloses version, current nick, joined channels, loaded modules, the
  *currently-authenticated admin nick list* (`bot._authed`), and log levels.
- `shutdown` / `quit` - graceful termination (denial of service, but nothing a local
  user could not do with a signal).

It does NOT allow sending IRC traffic, loading/unloading modules, granting admin,
touching config, or reading secrets. So "admin-equivalent" (the docstring's and startup
warning's phrase) is an upper-bound framing: the actual surface is
observe + verbosity + terminate, which is strictly less than the IRC-side admin command
set. The conservative label errs in the safe direction.

Defense-in-depth beyond the docstring:

- Auto-skip when stdin is not a TTY (`should_skip_console()`), preventing piped input
  from driving the console under systemd/Docker/redirection. Gated in
  `internets.py:1416` together with the explicit `--no-console` opt-out.
- A WARNING-level `event=console_active` line at startup so the log records that an
  unauthenticated control surface is live, with the pid.

Command parsing is `str.split()` on trusted-by-definition input; no injection surface
(nothing is passed to a shell, filesystem, or the IRC connection).

## Classes

None.

## Functions and methods

### `should_skip_console() -> bool`

Returns True when stdin is not an interactive TTY, or has no `isatty`, or is already
closed. The docstring is explicit that this is purely a security gate (not an
EOF-loop workaround, since the dispatch loop exits on the first `EOFError`). Caller:
`internets.py - _main()`. Pure read, no side effects.

### `_console_dispatch_loop(bot) -> None`

The synchronous read/dispatch loop; runs only on the daemon thread. Reads with
`input("> ")`, strips, skips empty lines, lowercases the command word, and dispatches:
`help` (prints `_CONSOLE_HELP`), `debug` -> `botlog.apply_debug(args)` (default
`reply=print`), `loglevel` -> `botlog.apply_loglevel(args)` (error string printed),
`status` -> `_print_status(bot)`, `shutdown`/`quit` -> logs the reason, calls
`bot.request_shutdown(reason)`, and returns. Unknown commands print a hint. Exits (and
thereby triggers bot shutdown via the lifecycle coupling) on EOF, Ctrl-C, closed stdin,
or shutdown/quit. Behavioral evidence for the dispatched helpers lives in
`tests/test_botlog.py` (apply_debug / apply_loglevel sections); the loop itself is
untested.

### `run_console(bot) -> None` (async)

Logs the `event=console_active` warning (WARNING level so it survives default INFO
filtering; includes pid via an inline `__import__("os").getpid()`), captures the running
loop, creates the `done` event, and starts `_wrap` on
`threading.Thread(daemon=True, name="console-input")`. `_wrap` runs the dispatch loop,
logs any escape exception, and in `finally` schedules `done.set` onto the loop, guarded
against a closed loop. The coroutine then awaits `done` and re-raises
`CancelledError` on cancellation with nothing to clean up (the thread is daemonized).
The docstring carries the full to_thread/`shutdown_default_executor` rationale
(mirrored at the call site, `internets.py:1447-1453`).

### `_print_status(bot) -> None`

Prints version, nick, sorted channels (`bot.active_channels.snapshot()`, an internally
locked copy), modules (under `bot._mod_lock`), authed admins (under `bot._auth_lock`),
base log level plus global-debug flag, and active debug subsystems from
`botlog.log_filter`. Reaches into `IRCBot` private fields (`_nick`, `_modules`,
`_authed`, both locks) rather than public accessors.

## Implementation walk

- `console.py:1-14` (module docstring): the security model; matches the implementation
  and the gating at the call site.
- `console.py:16-28` (imports): `TYPE_CHECKING` import of `IRCBot` avoids the runtime
  circular import with `internets.py`; compatibility.
- `console.py:30-37` (`_CONSOLE_HELP`): the help text; matches the dispatch table
  exactly (verified command-by-command).
- `console.py:39` (logger): uses the root `"internets"` logger, not a
  `internets.console` child, so console log lines cannot be targeted by per-subsystem
  debug; minor asymmetry with other modules.
- `console.py:42-59` (`should_skip_console`): validation / security enforcement;
  fail-safe returns.
- `console.py:62-102` (`_console_dispatch_loop`): control flow + dispatch; the
  docstring's thread-safety inventory is the load-bearing part.
- `console.py:105-151` (`run_console`): concurrency bridge; the daemon-thread
  rationale, the loud startup warning (security enforcement), `_wrap`'s
  exception/finally structure (error handling), and the `CancelledError` passthrough
  (cleanup).
- `console.py:154-169` (`_print_status`): formatting; lock usage as described above.

## Findings

- test-gap | console.py - (whole module) | No `tests/test_console.py` exists; nothing
  exercises `should_skip_console()`'s fail-safe branches, the dispatch loop's exit
  conditions, or the `run_console` thread/event bridge (only the `--no-console` CLI
  flag is tested, in `tests/test_config.py - test_no_console_flag`).
- doc-drift | console.py - _console_dispatch_loop() | The docstring claims
  `_print_status` "reads bot fields through their dedicated threading.Lock-guarded
  accessors", but `bot._nick` is a bare unguarded attribute read and the `log_filter`
  fields are unsynchronized; benign in CPython (atomic reference reads) but the claim
  is broader than the code.
- questionable | console.py - _print_status() | Reads `IRCBot` private fields and takes
  its private locks directly instead of going through accessors, coupling the console
  to the bot's internals; a rename of `_authed`/`_mod_lock` breaks the console with no
  test to catch it.
- questionable | console.py - run_console() | `__import__("os").getpid()` inline
  instead of a top-level `import os`; obscure for zero benefit.
- questionable | console.py - module logger | Logging to the root `"internets"` logger
  rather than a `"internets.console"` child makes console events untargetable by the
  per-subsystem `debug` facility the console itself controls.
