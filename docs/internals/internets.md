# internets.py - bot core: connection lifecycle, command dispatch, module loader, process entry

## Purpose

`internets.py` is the executable heart of the bot. It owns the single asyncio event
loop, the TCP/TLS connection to the IRC server, IRCv3 capability and SASL negotiation,
registration, liveness (PING/PONG), reconnection with jittered backoff, all in-memory
IRC state (channels, chanops, nick-to-hostmask map, admin sessions, shadow-bans), the
dynamic module loader, and the command dispatch pipeline that turns an inbound PRIVMSG
into a bounded, timed handler task. It also contains the process entry point
(`_entry()` / `_main()`), including the drop-root guard, the single-instance process
lock, and restart-via-`os.execv`.

Admin command handler bodies do NOT live here: they live in
`admin_cmds.py - AdminCommandsMixin`, which `IRCBot` inherits (see
[Classes](#classes)).

## Responsibilities / boundaries

Belongs here:

- Connection lifecycle: `IRCBot._connect()`, the read loop in `IRCBot.run()`,
  keepalive, reconnect/backoff, graceful shutdown ordering.
- Protocol state machine: CAP/SASL (`_handle_cap`), numerics (`_handle_numeric`),
  membership tracking (`_handle_membership`), PRIVMSG parsing (`_handle_privmsg`).
- Dispatch policy: shadow-bans, flood/API/channel gates, arg-length cap, task cap,
  per-command timeout (`_dispatch` / `_run_cmd`).
- Module load/unload/reload mechanics (`load_module` and friends).
- Process concerns: signals, restart, process lock, root refusal (`run`, `_main`,
  `_entry`).

Deliberately NOT here:

- Admin command handler bodies and the `_CORE` dispatch table definition -
  `admin_cmds.py - AdminCommandsMixin`.
- Outbound rate limiting and wire writes - `sender.py - Sender` (the core only
  enqueues).
- Persistence (locations/channels/users JSON, flush thread) and per-nick rate
  windows - `store.py - Store` / `store.py - RateLimiter`.
- Config parsing and credential resolution - `config.py` (import-time constants) and
  `secret_store`.
- Line-format parsing helpers with no bot state - `protocol.py`.
- Interactive stdin console - `console.py`.
- Logging setup - `botlog.py`.

## Dependencies and dependents

Internal imports (contracts consumed, documented in their own files):

| Collaborator | What the core uses |
|---|---|
| `config.py` | Import-time constants (`SERVER`, `PORT`, `NICKNAME`, `NS_PW`, `CMD_PREFIX`, `AUTO_LOAD`, `DESIRED_CAPS`, ...), the live `cfg` parser, `cli_args`, `reload_config()` (via SIGHUP). Importing `config` parses `config.ini` + `config.local.ini` and CLI args as a side effect. |
| `admin_cmds.py` | `AdminCommandsMixin` base class: `_CORE` dict (command word -> `cmd_*` method name), `_CORE_PUBLIC`, all `cmd_*` coroutines. |
| `sender.py` | `Sender` (priority queue + token bucket; `enqueue` is thread-safe; priority 0 bypasses the bucket and is never dropped on overflow), `redact_secrets()` for log redaction. |
| `store.py` | `Store` (JSON persistence with background flush thread), `RateLimiter` (`flood_check` / `api_check` / `channel_check`). |
| `protocol.py` | Pure parsers: `strip_tags`, `parse_isupport_chanmodes`, `parse_isupport_prefix`, `parse_mode_changes`, `parse_names_entry`, `sasl_plain_payload`. |
| `process_lock.py` | `ProcessLock` context manager + `LockHeld`; PID-file single-instance guard with stale detection; `release()` called explicitly on the restart path. |
| `console.py` | `run_console(bot)` coroutine, `should_skip_console()` TTY check. |
| `botlog.py` | `log` (the `internets` root logger), `log_filter` (re-exported for tests). |
| `metrics.py` | Optional Prometheus registry; imported lazily inside `try/except` at every touch point so a missing/broken exporter can never affect the bot. |

External: stdlib only (`asyncio`, `ssl`, `re`, `signal`, `threading`, `importlib`,
`secrets`, ...).

Dependents: `console.py` (drives a live `IRCBot`), `pyproject.toml` console script
(entry point `_entry`), every module in `modules/` (receives the bot instance through
`setup(bot)` and calls its public accessors: `privmsg`, `notice`, `reply`, `is_admin`,
`is_chanop`, `rate_limited`, `loc_get/set/del`, `channel_users`, ...), and the test
suites (`tests/run_tests.py` imports `IRCBot`, `ChannelSet`, `_backoff`,
`_redact_inbound` directly).

## Lifecycle

1. Import: pulls in `config` (which reads `config.ini` and parses argv), compiles all
   module-level regexes, creates the per-subsystem loggers. No network or file writes.
2. `_entry()` (console script / `__main__`): drop-root guard, `ProcessLock` acquire,
   `asyncio.run(_main(lock))`.
3. `_main()`: constructs `IRCBot` (which constructs `Store` - starting its flush
   thread - and loads shadow-bans), optionally starts the metrics exporter and the
   console task, then runs `bot.run()` until it or the console finishes.
4. `IRCBot.run()`: installs signal handlers, autoloads modules, connects (with
   backoff), registers, then loops on `readline` -> `_process()` until `_stop` is set
   or a permanent failure occurs; ends by awaiting `graceful_shutdown()`.
5. Teardown: `_main` closes stdin to unblock the console thread, cancels leftovers,
   flushes log handlers, and either returns (normal exit) or replaces the process via
   `os.execv` when `_restart_flag` is set.

The full startup and PRIVMSG traces are in the
[Implementation walk](#implementation-walk).

## State

All state is in-memory on the `IRCBot` instance unless noted.

| Field | Content | Guard | Persistent? |
|---|---|---|---|
| `active_channels` | `ChannelSet` of joined channels (lowercased) | internal lock | mirrored to `channels.json` via `_save_channels()` on every join/part/kick/join-error and at shutdown |
| `_modules` / `_commands` | loaded module instances; command word -> `(module_name, method_name)` | `_mod_lock` | no |
| `_authed` | nick -> hostmask bound at auth time (admin sessions) | `_auth_lock` | no; cleared on disconnect, SIGHUP, quit, nick change |
| `_auth_fails` | nick -> (fail count, last ts) for lockout | `_auth_lock` | no |
| `_nick_hosts` | nick -> last seen `user@host` | `_auth_lock` | no |
| `_chanops` | channel -> set of op nicks (from 353 NAMES + MODE) | `_chanops_lock` | no |
| `_nick` | the bot's CURRENT nick (433 bumps, NICK tracking) | loop thread only | no |
| `_caps`, `_cap_busy`, `_sasl_in_progress`, `_ns_identified`, `_sasl_failed_permanently` | CAP/SASL/registration flags | loop thread only | no (reset per connection in `_connect`, except `_sasl_failed_permanently`) |
| `_chanmode_types`, `_prefix_modes` | ISUPPORT-derived mode tables, seeded from `_DEFAULT_CHANMODE_TYPES` / `_DEFAULT_PREFIX_MODES` | loop thread only | no; re-seeded per connection |
| `_shadow_bans`, `_shadow_ban_reasons` | silently-ignored nicks | loop thread only | `shadow_bans.json`, 0600, atomic replace |
| `_store`, `_rate` | `Store` / `RateLimiter` instances | their own locks | `Store` persists to JSON files |
| `_metrics`, `_stats_*` | observability counters (`.stats` / `.debug` / shutdown summary) | loop thread (see Concurrency) | no |
| `_tasks`, `_active_cmd_tasks` | live background + command tasks; O(1) cap counter | loop thread only | no |
| `_loop`, `_reader`, `_writer`, `_sender`, `_stop` | runtime plumbing, None until `run()`/`_connect()` | - | no |
| `_quit_msg`, `_restart_flag`, `_shutdown_initiated` | shutdown intent | first-writer-wins via `_shutdown_initiated` | no |
| `_last_pong`, `_last_invite_time` | liveness clock (monotonic), invite cooldown | loop thread only | no |
| `_tls_active` | whether the live connection is TLS | set in `_connect` | no |

Note `_tls_active` is created in `_connect()` rather than `__init__`, which is why
`_tls_or_refuse()` reads it via `getattr(self, "_tls_active", False)` - fail-closed
when the attribute does not exist yet.

## Concurrency

One asyncio event loop owns: the read loop, `_process()` and all `_handle_*` parsing,
`_dispatch()`, every command task (`_run_cmd`), the `Sender._drain` task, `keepalive`,
`rejoin`, and the console's `run_console` coroutine wrapper.

Threads that exist alongside it:

- `Store`'s flush thread (owned by `store.py`).
- The console's `console-input` daemon thread (owned by `console.py`; blocked in
  `input()`).
- `asyncio.to_thread` workers spawned inside command handlers (module HTTP calls,
  `verify_password` in `cmd_auth`, audit writes).

The locks exist because those to_thread workers call back into bot accessors:

- `_mod_lock`: module registry reads (dispatch, `.modules`, `_process` fanout
  snapshot) vs. load/unload mutation.
- `_auth_lock`: `is_admin()` (called from worker threads via `flood_limited` and
  module code) vs. `_nick_hosts`/`_authed` mutation on the loop thread.
- `_chanops_lock`: `is_chanop()` from workers vs. 353/MODE/PART/QUIT/NICK mutation -
  the comment at `IRCBot.is_chanop()` names the torn-read/"dict changed size" race it
  prevents.
- `ChannelSet`'s internal lock: same pattern for channel membership.

Cross-thread signalling is explicit: `request_shutdown()` uses
`loop.call_soon_threadsafe` to set `_stop` (safe from signal handlers and the console
thread); `Sender.enqueue()` does the same to reach its queue.

Known-benign looseness: `send()` bumps `_stats_msg_out` on whatever thread called it
(module workers included); a lost increment only skews a stats counter.
`_metrics["dropped_messages"]` is safe because the Sender invokes `_bump_dropped_metric`
on the loop thread (documented at `IRCBot._bump_dropped_metric()`).

Ordering guarantees: `_process()` is synchronous, so protocol state updates (nick
tracking, auth revocation, chanops) complete before the next line is read. Command
handlers run as separate tasks and give no ordering guarantee among themselves - by
design, since they may block on network I/O for up to 60 s.

## Failure behavior

- Connect failure (initial): retried forever with `_backoff_jittered` (15 s doubling
  to a 300 s cap, +-25% equal jitter), interruptible by `_stop`.
- Read timeout (300 s of silence): converted to `ConnectionResetError` -> reconnect
  path.
- Half-open link: `_keepalive` PINGs every 90 s; if no PONG for 240 s it closes the
  writer, which makes the read loop fail into the reconnect path.
- Reconnect path (`run()` `except (ConnectionResetError, ...)`): bumps metrics,
  cancels background/command tasks, stops the sender, clears admin sessions and
  `_nick_hosts` (fail-closed: sessions never survive a disconnect), then loops
  wait-then-`_connect` with fresh backoff. Registration state resets so PASS/CAP/
  NICK/USER are re-sent.
- Permanent failure: SASL 904/905 sets `_sasl_failed_permanently`; if it has failed
  >= 3 times and there is no `NS_PW` fallback, the reconnect loop aborts
  (`event=reconnect_aborted`) instead of hammering the server with bad credentials.
- Oversized line (> 8192 bytes): the `ValueError` arm counts it, drains to the next
  newline so a truncated tail is not parsed as a spurious line, and continues.
- Command handler failure: `_run_cmd` converts timeout (60 s) and any exception into
  a user-facing generic notice plus a logged traceback; the generic message (not the
  raw exception) is asserted by SEC-008 in `tests/run_tests.py`.
- Module `on_raw` failure: swallowed per-module at debug level so one broken module
  cannot break line processing.
- Unexpected main-loop exception: counted, logged with traceback, 5 s pause, loop
  continues (`_UNEXPECTED_SLEEP_S`).
- Shutdown is best-effort at every step: `graceful_shutdown()` wraps each of its
  eight steps in its own try/except so a failing store flush cannot prevent the QUIT
  or the task cancellation.
- Shadow-ban file load/save failures degrade to an empty list / a warning, never a
  crash (`_load_shadow_bans` / `_save_shadow_bans`).

## Security

Trust boundaries and controls, in the order an attacker meets them:

- Process posture: refuses to start as root unless `INTERNETS_ALLOW_ROOT=1`
  (`_entry()`); single-instance `ProcessLock` prevents two processes corrupting the
  JSON state files.
- Transport: TLS on by default, TLS 1.3 minimum unless `INTERNETS_ALLOW_TLS12=1`
  (logged loudly); `ssl_verify=false` is honored but warned about on every connect.
- Credential egress: every credential send (PASS, SASL, NickServ IDENTIFY, OPER) is
  gated by `_tls_or_refuse()`, which refuses and logs CRITICAL on a plaintext link.
- Log redaction, both directions: outbound in `sender.py - Sender._write_line`;
  inbound via `_redact_inbound()` before the `<<` debug line. `_redact_inbound`
  deliberately scopes redaction to the PRIVMSG/NOTICE trailing text (so `ident@host`
  cannot trigger the IDENT verb) and deliberately does NOT require the target to be
  the bot's nick - the comment above `_RE_TRAILING_MSG` records the incident where
  broken nick tracking would otherwise have disabled redaction. `.auth`/`.deauth`
  arguments are masked wholesale in the command log (`_handle_privmsg`).
- Admin authorization: `is_admin()` is fail-closed - it grants only when the current
  hostmask is known, is not the `"unknown"` sentinel, and equals the hostmask bound
  at auth time; a sentinel or changed binding actively revokes the session. Sessions
  are also revoked on QUIT, on NICK change (identity change, never migrated - the
  comment in `_handle_membership` names the nick-takeover laundering risk), on
  disconnect, and on SIGHUP. Brute force is bounded by the lockout logic in
  `admin_cmds.py - cmd_auth` (5 fails / 300 s sliding window; behaviorally tested in
  `tests/run_tests.py` "auth lockout").
- Input hardening: PONG payload capped to 400 bytes (BUG-050 test), read buffer
  capped at 8192 (`_READ_LIMIT`, BUG-042 test), argument length capped at 400
  (`_MAX_ARG_LEN`), concurrent command tasks capped at 50 (`_MAX_TASKS`, BUG-030
  test), per-nick flood + per-channel burst gates before any handler runs.
- Module loading: name must match `^[a-z][a-z0-9_]*$` AND the resolved path must stay
  inside `MODULES_DIR` (`resolve().relative_to`), which also defeats a symlink
  pointing out of the tree. Loading a module is still arbitrary code execution by
  design - only admins can invoke `.load`, and `AUTO_LOAD` comes from the config
  file.
- Console: admin-equivalent, so it is enabled only when stdin is an interactive TTY
  (`console.should_skip_console`); piped stdin never gets a console.
- Shadow-ban file written 0600 via mkstemp + `os.replace` (atomic, no partial reads).
- Restart exec: `os.execv(sys.executable, sys.argv)` re-runs our own argv only; the
  nosec comments document why B603/B606 are false positives here.

## Classes

### ChannelSet

`internets.py - ChannelSet`. Thread-safe set of lowercased channel names. Every
operation (including `__contains__`, `__len__`, `__bool__`) takes the internal
`threading.Lock`; `snapshot()` returns a copy, and `__iter__` iterates the snapshot so
callers can never see a mutating set. Needed because module code running in to_thread
workers reads membership while the loop thread mutates it. Behaviorally tested
("ChannelSet: thread-safe add/discard/contains", "snapshot returns copy", "iteration
is safe" in `tests/run_tests.py`).

### IRCBot

`internets.py - IRCBot(AdminCommandsMixin)`. The one instance per process, created by
`_main()`.

Inheritance contract with the mixin:

- `_CORE` (command word -> `cmd_*` method-name string) and `_CORE_PUBLIC` are class
  attributes of `AdminCommandsMixin` (`admin_cmds.py`). They live there, next to the
  handlers, so the `.help` output derives from the same table the dispatcher uses -
  the comment at `admin_cmds.py - AdminCommandsMixin._CORE` records that a
  hand-copied help grid previously drifted. `IRCBot` reaches them through normal MRO
  attribute lookup (`self._CORE` in `_dispatch` and `_handle_privmsg`).
- The mixin declares typed field stubs (`_nick`, `_authed`, `_mod_lock`, ...) and
  ellipsis-bodied method stubs (`preply`, `send`, `is_admin`, `load_module`, ...) for
  type checkers only. Because `IRCBot` precedes the mixin in the MRO and defines all
  of them for real, the stubs are never executed.
- Dispatch binds handlers at call time: `getattr(self, self._CORE[cmd])` yields the
  mixin coroutine bound to the bot instance. `tests/run_tests.py` asserts every
  `_CORE` entry resolves to an async method ("IRCBot._CORE handlers are all async",
  by iterating `IRCBot._CORE`).

Class constants (all class-level so tests can assert them without an instance):
`_MAX_BODY=400` (PRIVMSG chunking), `_MAX_TASKS=50`, `_CMD_TIMEOUT=60`,
`_MAX_ARG_LEN=400`, auth lockout parameters, and the network timing block
(`_READ_LIMIT=8192`, `_READ_TIMEOUT=300`, `_PING_INTERVAL=90`, `_PONG_TIMEOUT=240`,
NickServ wait = 40 x 0.25 s, `_SHUTDOWN_DRAIN_S=2.0`, `_UNEXPECTED_SLEEP_S=5.0`).
About 30 precompiled regexes (`_RE_*`) cover the `_process()` hot path; notable ones
are documented inline where used below.

Invariants:

- `_authed[nick]` always holds the hostmask captured at auth time; any observed
  divergence revokes.
- `_chanmode_types` / `_prefix_modes` are never empty: malformed ISUPPORT keeps the
  previous table (tested: "a malformed ISUPPORT token never wipes the mode tables").
- `_active_cmd_tasks` equals the number of live `cmd-*` tasks (increment before
  create, decrement in the done callback, floored at 0).
- Priority 0 is reserved for protocol traffic (PONG, CAP, NICK, PASS, AUTHENTICATE,
  QUIT, keepalive PING); everything user-visible is priority 1.

Extension constraints: modules must interact only through the public accessors
(`privmsg`/`notice`/`reply`/`preply`, `is_admin`, `is_chanop`, `rate_limited`,
`loc_*`, `channel_users`, `send`); they receive the bot in `setup(bot)` and must not
touch `_`-prefixed state.

## Functions and methods

### Module-level helpers

| Symbol | Notes |
|---|---|
| `_RNG` | `random.SystemRandom` instance for jitter (non-crypto; the comment explains it exists to keep Bandit B311 quiet). Security tokens use `secrets` instead. |
| `_redact_inbound(line)` | Applies `sender.redact_secrets` only to the trailing text of an inbound PRIVMSG/NOTICE (`_RE_TRAILING_MSG`); other lines pass through. Tested directly in `tests/run_tests.py` (verbatim pass-through of a non-matching line, `secretpw` never surviving). |
| `_backoff(attempt, base=15, cap=300)` | Deterministic `min(base * 2**attempt, cap)`; kept jitter-free for testability (asserted exactly for attempts 0-10 in `tests/run_tests.py`). |
| `_backoff_jittered(...)` | Equal-jitter wrapper: uniform in `[delay*(1-0.25), delay*(1+0.25)]`, floored at 0. The comment names the rejected alternative (decorrelated jitter would compound state). |
| `_DEFAULT_CHANMODE_TYPES`, `_DEFAULT_PREFIX_MODES` | RFC-safe mode-table seeds, module-level because both `__init__` and `_connect` need them and a second hand-written copy would drift (per the comment). |

### Outbound messaging

- `send(msg, priority=1)`: enqueue on the `Sender` if one exists (silently a no-op
  pre-connection), bump `_stats_msg_out`.
- `privmsg(target, msg)` / `notice(target, msg)`: reject targets containing a space
  or empty (cheap injection/protocol-error guard), chunk via `_split_msg`.
- `reply(nick, reply_to, msg, privileged=False)`: PM stays PM; privileged output to a
  channel goes as a NOTICE to the nick (avoids leaking admin traffic into the
  channel); normal output goes to the channel. `preply` = `reply(privileged=True)`.
- `_split_msg(msg)`: splits on UTF-8 byte length at `_MAX_BODY=400`, backing up over
  continuation bytes (`(b & 0xC0) == 0x80`) so no chunk splits a code point; if a
  single code point exceeds the cap (impossible for real UTF-8, defensive), it falls
  back to a hard split with `errors="replace"`. Tested in `tests/run_tests.py` via
  `IRCBot.__new__` (no `__init__` needed).

### Accessors / gates

- `is_admin(nick)`: see Security. Note it MUTATES under the read: a revoked binding
  is deleted inside the check. Case-insensitivity and the fail-closed sentinel
  behavior are behaviorally tested ("admin nick matching is case-insensitive",
  "hostmask-change revokes admin session" in `tests/run_tests.py`).
- `is_chanop(channel, nick)`: locked set lookup.
- `flood_limited(nick)`: `RateLimiter.flood_check` with admin bypass;
  `rate_limited(nick)`: API window (consumed by modules before expensive calls);
  `channel_limited(channel)`: cross-nick burst gate (20 commands/10 s default).
- `is_shadow_banned(nick)`: case-insensitive set membership.
- `loc_get/loc_set/loc_del/channel_users`: one-line delegations to `Store`.

### Shadow-ban store

`_load_shadow_bans()` tolerantly parses `shadow_bans.json` (`{"bans": [...],
"reasons": {...}}`), lowercasing keys; any failure leaves the list empty and warns.
`_save_shadow_bans()` writes atomically: mkstemp in the destination directory, chmod
0600, `os.replace`; the temp file is unlinked on failure. Mutation happens in the
mixin's `cmd_shadow_*` handlers; this file owns only load/save/membership.

### Module management

`load_module(name)` - all under `_mod_lock`:

1. Name gate: `^[a-z][a-z0-9_]*$` (rejects dots, slashes, uppercase).
2. Duplicate-load check against `_modules`.
3. Path: `MODULES_DIR / f"{name}.py"` must exist AND
   `path.resolve().relative_to(MODULES_DIR.resolve())` must succeed - containment
   check that also defeats symlinks escaping the directory.
4. Import: `importlib.util.spec_from_file_location(f"modules.{name}", path)` ->
   `module_from_spec` -> `spec.loader.exec_module(mod)`. **No `sys.modules` entry is
   ever created for the loaded module.** Consequences (the precise reload
   semantics):
   - Every `.load` executes the file fresh from disk, so `.reload` picks up source
     edits without `importlib.reload` and without stale-bytecode concerns.
   - Nothing module-internal survives a reload: module-level globals, caches, and
     class objects are all new; the old module object becomes garbage once the old
     `BotModule` instance is dropped - UNLESS something still references it (for
     example a background task the old instance started and `on_unload` failed to
     cancel; keeping that from leaking is the module author's `on_unload` contract).
   - What DOES survive a reload is everything bot-owned that the module reads
     through `self.bot`: the `Store` files, `cfg`, rate-limiter windows, channel
     state.
   - Imports made BY the module (`from modules.base import ...`, third-party
     libraries) go through the normal import system and ARE cached in
     `sys.modules`. `.reload foo` therefore does NOT reload `modules/base.py` or
     any library - changes there need `.restart`.
   - A plain `import modules.name` executed anywhere else would create a second,
     distinct module object (registered in `sys.modules`); class identity across
     the two would differ. Nothing in the codebase does this; it is a constraint to
     preserve.
5. Contract check: the module must expose `setup(bot)`; the returned instance's
   `COMMANDS` (command word -> method-name string) must not conflict with a command
   owned by a DIFFERENT module.
6. `inst.on_load()` runs before registration - if it raises, the load fails and
   nothing is registered (but `setup()` side effects have already happened).
7. Registration: `_modules[name] = inst`; each command maps to
   `(module_name, method_name)`. Dispatch resolves `getattr(inst, method_name)` at
   call time, so the mapping stays valid across instance replacement.

Errors are logged with detail but reported to IRC generically ("see log for
details") - no exception text reaches the channel.

`unload_module(name)`: `on_unload()` first; if it raises, the module STAYS fully
loaded (commands intact) and the error is reported - deliberate, since half-removing
a module that failed to tear down would strand its commands. On success, its commands
and registry entry are removed.

`reload_module(name)`: strictly `unload` then `load`; an unload failure aborts the
reload. `autoload_modules()` iterates `AUTO_LOAD` from config at startup, logging
per-module success/failure.

### Shutdown

- `request_shutdown(reason)`: idempotent (first `_shutdown_initiated` wins, so a
  SIGINT during a clean shutdown cannot rewrite the QUIT message), thread-safe
  (`call_soon_threadsafe(self._stop.set)`).
- `graceful_shutdown()`: eight ordered, individually-guarded steps: (1) persist
  channels, (2) unload modules (their flush chance), (3) stop the store flush thread
  with a final write, (4) enqueue QUIT at priority 0, sleep 2 s for the sender to
  drain it, (5) stop the sender, (6) close the socket, (7) cancel and gather all
  background/command tasks, (7b) stop the metrics server, (8) flush log handlers -
  explicitly because `os.execv` skips atexit handlers. The ordering is load-bearing:
  channels are saved before anything else can fail; QUIT must be enqueued before the
  sender stops.

### Dispatch pipeline

- `_cmd_prefix()`: reads `command_prefix` from the live `cfg` at USE-time rather
  than the import-time `CMD_PREFIX` constant, so `.rehash`/SIGHUP prefix changes take
  effect without a restart (the docstring records the drift bug this fixes: core
  frozen on the old prefix while modules saw the new one).
- `_dispatch(nick, reply_to, cmd, arg, is_pm)`: the policy gate chain, in order:
  1. Shadow-ban: silent drop - no reply, no rate-limit consumption, no audit trace
     (the comment states the point: indistinguishable from the bot being offline).
  2. `auth`/`deauth` must be in PM (prevents a password being typed into a channel).
  3. Per-nick flood gate (admins bypass) - noisy notice.
  4. Per-channel burst gate - silent, log-only (a throttle notice would add to the
     flood).
  5. Arg length cap (400).
  6. Task cap: O(1) `_active_cmd_tasks >= _MAX_TASKS` check (the comment notes the
     constant name is asserted by the BUG-030 source-inspection test).
  7. Handler resolution: `_CORE` first (mixin method via `getattr(self, ...)`, not
     unloadable), else the module registry under `_mod_lock`.
  8. Task creation: increment counter and stats, bump the per-command Prometheus
     counter (module label = owning module or "core"), create the `cmd-<name>` task,
     append to `_tasks`, and attach a done callback that decrements the counter and
     removes the task from `_tasks`.
- `_run_cmd(handler, ...)`: `asyncio.wait_for(handler(nick, reply_to, arg), 60)`.
  Timeout -> counted, warned, user notified; `CancelledError` re-raised (shutdown
  path must propagate); any other exception -> counted, traceback logged, generic
  notice.

### Connection and protocol processing

- `_tls_or_refuse(cred_name)`: see Security.
- `_connect()`: builds the TLS context (1.3 floor / env downgrade / verify-off
  warning), `asyncio.open_connection(..., limit=_READ_LIMIT)`, then resets ALL
  per-connection state: nick back to configured `NICKNAME`, CAP/SASL/identify flags,
  `_caps`, `_last_pong` clock, `_chanops`, and the ISUPPORT tables back to defaults
  (the module-level comment explains why: reconnect can land on a different server,
  and a stale CHANMODES/PREFIX table silently misaligns MODE parameters). Finally
  replaces the `Sender` (stopping any previous one) and stamps `_stats_connect_ts`.
- `_keepalive()`: every 90 s, check `monotonic() - _last_pong > 240` -> close writer
  and return (the read loop turns that into a reconnect); otherwise send
  `PING :<server>` at priority 0.
- `_save_channels()`, `_on_invite()` (channel-shape check via `_CHAN_RE`, 5 s global
  invite cooldown), `_on_join()`, `_on_part()` (also drops the channel's chanops
  set): channel state bookkeeping, each persisting via `_save_channels`.
- `_deferred_rejoin()`: if `NS_PW` is set, poll `_ns_identified` up to 10 s (40 x
  0.25 s) so rejoin happens after services grant a hostmask/vhost; then JOIN every
  saved channel that matches `_CHAN_RE`.
- `_process(line)`: the per-line pipeline; see the trace below.
- `_handle_cap(line)`: CAP LS/ACK/NAK/NEW, the `AUTHENTICATE +` challenge, SASL
  numerics 903 (success) / 902/904/905 (failure; 904/905 set
  `_sasl_failed_permanently`), 421-for-CAP (no CAP support), and 451 (registration
  nudge). Falls back to NickServ IDENTIFY on SASL failure by simply completing CAP
  END and letting the MOTD hook send IDENTIFY. See Findings for the multiline CAP LS
  and ACK-replacement issues.
- `_handle_numeric(line)`: 433 nick collision (append `_` up to base+3 chars, then
  `base + secrets.randbelow(90)+10`), 005 ISUPPORT (malformed tokens keep the
  current table - tested), 473 invite-only (asks the configured services nick for an
  INVITE), join errors 403/405/471/474-476 (drop the channel from saved state), 381/
  491 OPER result, NickServ identification detection (900 numeric or a NickServ
  NOTICE containing "identified"/"recognized"), 353 NAMES (seed chanops via
  `parse_names_entry`), channel MODE (update chanops via `parse_mode_changes`
  against the live ISUPPORT tables).
- `_handle_membership(line)`: CHGHOST (refresh store + cached hostmask), ACCOUNT
  (audit log + refresh store record), INVITE, JOIN (self -> `_on_join`, other ->
  store), PART/KICK (self -> `_on_part`, other -> store + chanops removal), QUIT
  (store, chanops, drop cached hostmask, revoke admin session), NICK (track own nick
  - including the bare-nick-prefix server form; the comment block above `_RE_NICK`
  documents the incident where requiring `!` broke self-nick tracking - rename in
  store, move chanops entries, and DROP any admin session rather than migrate it).
  The regression tests "bare-nick NICK change updates self._nick" and "nick change
  revokes admin auth" in `tests/run_tests.py` pin both behaviors.
- `_handle_privmsg(line)`: see the trace below.

### Main loop and signals

- `run()`: see the startup trace below.
- `_on_signal(signum)`: SIGINT/SIGTERM -> `request_shutdown` once; repeats logged and
  ignored.
- `_on_sighup()`: `config.reload_config()` (template + local overlay together - the
  `config.py` docstring explains re-reading only the template would clobber overlay
  values), then defensively clears admin sessions. Import-time credential constants
  are NOT refreshed; the comment says so explicitly and the log message is worded to
  match.

### Entry point

- `_main(lock)`: constructs the bot, optionally starts the metrics exporter
  (`[metrics] enable`, loopback-by-default host), starts the console task only when
  the operator did not pass `--no-console` AND stdin is an interactive TTY, runs the
  bot task, and on whichever finishes first coordinates the other: a finished console
  triggers `request_shutdown("Console exited")` with a 10 s grace then hard cancel;
  a finished bot leads to stdin being closed to unblock the console's blocked
  `input()` (the long comment explains the daemon-thread design and why to_thread
  would hang interpreter shutdown). Finally: flush handlers, and if `_restart_flag`
  (set by `admin_cmds.py - cmd_restart`) close log handlers, release the process
  lock (the comment explains the PID-preserved-across-execv trap), and `os.execv` on
  POSIX or `subprocess.Popen` + `sys.exit(0)` on Windows.
- `_entry()`: drop-root guard (POSIX; `INTERNETS_ALLOW_ROOT=1` overrides with a
  warning), `ProcessLock` on `./internets.pid` (cwd-relative on purpose - the lock
  scope matches the cwd-relative state files, so separate deployments in separate
  directories can coexist), `asyncio.run(_main(lock))`, KeyboardInterrupt -> exit
  130, `LockHeld` -> exit 1.

## Implementation walk

File layout, top to bottom: docstring and imports; `_RNG`; inbound redaction
(`_RE_TRAILING_MSG` + `_redact_inbound`); per-subsystem loggers; `ChannelSet`;
backoff helpers; mode-table defaults; `IRCBot` (constants and regexes, `__init__`,
messaging, accessors, shadow-bans, module management, shutdown, dispatch, connection,
background tasks, line processing, main loop, signal handlers); `_main`; `_entry`;
`__main__` guard. Every block is covered by the sections above; the two traces below
account for the control flow end to end.

### Trace 1: process startup, `_entry()` to the running loop

```text
_entry()
  euid==0 and no INTERNETS_ALLOW_ROOT=1?  -> log CRITICAL, exit 1
  ProcessLock("./internets.pid").__enter__      # LockHeld -> exit 1
  asyncio.run(_main(lock))
    IRCBot.__init__                             # Store (flush thread starts),
                                                # RateLimiter, shadow-bans loaded
    [metrics] enable=true?  -> registry.enable() + expose(host, port)
    console: --no-console OR stdin not a TTY -> skipped; else run_console task
    bot task = bot.run()
      capture loop, create _stop Event
      POSIX: add_signal_handler(SIGTERM/SIGINT -> _on_signal,
                                SIGHUP -> _on_sighup)
      autoload_modules()                        # AUTO_LOAD from config.ini
      initial connect loop:
        _connect()                              # TLS ctx, open_connection,
                                                # per-connection state reset,
                                                # new Sender started
        on failure: wait _backoff_jittered(attempt) (or _stop), attempt += 1
      registration (once per connection, priority 0 throughout):
        PASS <pw>            # only if SERVER_PW and TLS
        CAP LS 302           # _cap_busy = True
        NICK <nick>
        USER <NICKNAME> 0 * :<REALNAME>
      read loop, each iteration:
        readline (timeout 300s) raced against _stop.wait()
        -> CAP negotiation via _handle_cap:
             LS: REQ (DESIRED_CAPS ∩ offered) or CAP END
             ACK: if sasl granted + NS_PW + TLS -> AUTHENTICATE PLAIN
                  server: "AUTHENTICATE +" -> AUTHENTICATE <base64 nick\0nick\0pw>
                  903 -> _ns_identified, CAP END
                  902/904/905 -> CAP END (904/905: _sasl_failed_permanently)
             else: CAP END
        -> 433: bump nick, resend NICK
        -> 005: ISUPPORT CHANMODES/PREFIX tables (malformed token = keep current)
        -> 376/422 (end of MOTD), once:
             CAP END if still busy (no-CAP-support fallback)
             MODE <nick> <USER_MODES>
             PRIVMSG NickServ :IDENTIFY <pw>    # if NS_PW, not identified, TLS
             OPER <name> <pw>                   # if configured, TLS
             spawn keepalive + rejoin tasks
        -> steady state: every line -> _process(line)   (Trace 2)
```

Reconnect (any `ConnectionResetError` / SSL / OS error in the loop): cancel tasks,
stop sender, clear `_authed` + `_nick_hosts`, reset `identified`/`registered`, then
wait-and-`_connect` with fresh backoff - unless the SASL permanent-failure condition
holds, in which case the loop breaks. Shutdown from any source funnels through
`request_shutdown` -> `_stop` -> loop break -> `graceful_shutdown()` -> back in
`_main`: console unblocking, handler flush, and the optional execv restart.

### Trace 2: inbound PRIVMSG to handler task

```text
run() read loop
  raw = await readline()          # >8192 bytes -> oversized arm: count, drain, skip
  line = raw.decode(utf-8, replace).rstrip("\r\n")   # empty -> skip
  log.debug("<< " + _redact_inbound(line))           # credential-masked
  _process(line)
    line = strip_tags(line)                          # IRCv3 @tags removed FIRST
    PING?  -> PONG :<payload[:400]> (priority 0), return
    PONG?  -> _last_pong = monotonic(), return
    prefix nick shadow-banned? -> skip_module_fanout = True
    module fanout: for each loaded module (snapshot under _mod_lock):
        inst.on_raw(line)                            # exceptions -> debug log only
    _handle_cap(line)        -> False for a PRIVMSG
    _handle_numeric(line)    -> False
    _handle_membership(line) -> False
    _handle_privmsg(line)
      _RE_PRIVMSG match -> (nick, hostmask, target, text)
      _stats_msg_in += 1;  _nick_hosts[nick] = hostmask   (under _auth_lock)
      text starts with \x01 (CTCP)? -> return
      is_pm = target == self._nick (case-insensitive); reply_to = nick or channel
      channel + tracked? -> store.user_join(target, nick, hostmask)
      command word:
        channel: text must start with the LIVE prefix (_cmd_prefix())
        PM:      prefix optional if the bare first word is a known command
        known = _CORE ∪ _commands (snapshot under _mod_lock)
      log "cmd=... from nick!host"    # auth/deauth arg -> [REDACTED];
                                      # others through redact_secrets
      _dispatch(nick, reply_to, cmd, arg, is_pm)
        shadow-banned?          -> silent drop
        auth/deauth in channel? -> "must be used in PM"
        flood_limited(nick)?    -> "slow down" notice     (admins bypass)
        channel_limited?        -> silent drop, warn log
        len(arg) > 400?         -> "input too long"
        _active_cmd_tasks >= 50 -> "bot is busy"
        handler: _CORE[cmd] -> getattr(self, method)      # mixin coroutine
                 else _commands[cmd] -> getattr(module_instance, method)
        create_task(_run_cmd(...), name="cmd-<cmd>")
          await wait_for(handler(nick, reply_to, arg), timeout=60)
          TimeoutError -> metric + "'<cmd>' timed out."
          Exception    -> metric + traceback log + generic "internal error"
        done callback: counter -= 1, remove from _tasks
```

## Findings

- **defect | internets.py - IRCBot._handle_cap() / _RE_CAP** - multiline `CAP LS 302`
  replies are mishandled even though the bot explicitly requests them
  (`CAP LS 302` in `run()`). For a continuation line
  `:srv CAP * LS * :cap1 cap2 ...`, the regex captures params as
  `"* :cap1 cap2 ..."` (verified by direct regex probe): the `*` continuation marker
  is treated as a capability token, the first real capability keeps its leading
  colon and can never match `DESIRED_CAPS`, and the LS branch answers EACH line
  independently - so the bot can send `CAP REQ` (or worse, a premature `CAP END`
  when a continuation line happens to contain no desired caps) before the server has
  finished listing. Only servers whose cap list fits one line behave correctly.
- **questionable | internets.py - IRCBot._handle_cap() ACK branch** -
  `self._caps = set(params.split())` REPLACES the cap set on every ACK instead of
  unioning. With multiple `CAP REQ`s in flight (the multiline case above) or a
  post-registration `CAP NEW` -> `CAP REQ` -> `ACK`, previously granted caps vanish
  from `_caps`, and any ACK that does not trigger SASL sends a `CAP END` even
  mid-session. Currently low-impact because `_caps` is only consulted for `sasl`
  during registration, but it makes `_caps` untrustworthy as session state.
- **questionable | internets.py - IRCBot._handle_numeric() 005 branch** - unlike
  every other consumed numeric, the 005 branch does not `return True`; a 005 line
  falls through to `_handle_membership` and `_handle_privmsg`. Harmless today (no
  later regex can match a 005), but it is the one inconsistency in an otherwise
  strict matched-means-consumed convention.
- **questionable | internets.py - IRCBot.run() read race** - each loop iteration
  creates and cancels a fresh `stop_task` (`self._stop.wait()`) per inbound line.
  Correct, but it is per-line task churn on the hottest path; a single long-lived
  stop task (or `asyncio.wait` with a persistent waiter) would do the same job once.
- **questionable | internets.py - IRCBot.request_shutdown() pre-run window** - if
  called before `run()` has created `_stop`/`_loop` (nothing does today, but the
  console starts concurrently), it sets `_shutdown_initiated` without setting any
  event; `_on_signal` would then ignore all subsequent signals as
  "shutdown_already_in_flight" while no shutdown is actually in flight.
- **test-gap | internets.py - IRCBot._process()/_dispatch()/_run_cmd()** - no
  behavioral test drives a line end-to-end through `_process` into a dispatched
  handler task, and there are no tests at all for `_handle_cap` (CAP/SASL flow),
  shadow-ban filtering, the keepalive/pong-timeout path, or the reconnect loop.
  Existing coverage in `tests/run_tests.py` is real but partial: helpers
  (`ChannelSet`, `_backoff`, `_redact_inbound`, `_split_msg`), `is_admin` semantics,
  `_handle_membership`/`_handle_numeric` units, and source-inspection checks
  (BUG-030/042/050, SEC-008). The dispatch gate chain ordering (shadow-ban before
  flood before caps) is asserted nowhere.
- **doc-drift | tests/test_dispatcher.py** - despite the name, this file tests
  `weather_providers/_dispatch.py` (the weather-provider fallback dispatcher), not
  the bot's command dispatch. The name collision invites exactly the wrong
  assumption about where dispatch coverage lives (this assignment's brief made it).

## Module task registry and lifecycle fanout, added 2026-08-17

`on_unload()` is synchronous, so a module can cancel a background task but never
await the cancellation. The bot therefore owns the tasks:

| Symbol | Role |
| --- | --- |
| `IRCBot.create_module_task()` | Run a coroutine as a task owned by a named module; it deregisters itself when done |
| `IRCBot.drain_module_tasks()` | Cancel every task owned by a module and AWAIT them, with a timeout |
| `IRCBot._forget_module_task()` | Registry cleanup callback |
| `IRCBot._notify_modules()` | Best-effort fanout of a lifecycle hook to every loaded module |

`_dispatch()` registers a module's command tasks in the same registry, because a
dispatched handler is a scheduled task holding a bound method and would
otherwise keep running after `unload_module()` dropped the registry entries -
free to mutate state after the module's final flush. Core command tasks are not
registered; they belong to the bot.

`cmd_unload`, `cmd_reload`, and `cmd_reloadall` await `drain_module_tasks()`
before unloading. `unload_module()` itself is synchronous and can only cancel,
so it is the fallback for non-async callers such as autoload and shutdown.

The drain has a timeout. A task that ignores cancellation past it logs
`event=module_task_drain_timeout` and the unload proceeds, because blocking
`.unload` forever on a wedged module is the worse failure.

`on_connect` is fanned out after `event=connect_ok`; `on_disconnect` from the
connection-error branch before the reconnect backoff.
