# Changelog

All notable changes to the Internets IRC bot are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.0] — 2026-03-04

Full async conversion and quality pass.  The entire bot now runs on a single
asyncio event loop — no more spawning threads for every command.  All module
handlers are coroutines.  Blocking I/O (HTTP, disk, password hashing) runs
via `asyncio.to_thread()` inside the handler, keeping the event loop free.

### Architecture

- **asyncio event loop** replaces all daemon threads for the connection
  lifecycle, command dispatch, keepalive, send queue, console, and deferred
  channel rejoin.

- **Sender** is now an async drain loop over `asyncio.PriorityQueue` +
  `StreamWriter.drain()`.  Token-bucket rate limiting uses `asyncio.sleep`.
  Thread-safe `enqueue()` uses `loop.call_soon_threadsafe()` so module
  handlers can call `bot.send()` / `bot.privmsg()` from any context.

- **Command dispatch** creates `asyncio.Task` per command.  Handlers are
  awaited directly — no `asyncio.to_thread()` wrapper.  Only the actual
  blocking operations (HTTP, password hashing) use the thread pool.

- **Console** uses `asyncio.to_thread(input)` for non-blocking stdin reads,
  with the console task running alongside the main bot task via
  `asyncio.wait(return_when=FIRST_COMPLETED)`.

- **Signal handling** uses `loop.add_signal_handler()` instead of the
  `signal` module, properly integrating with the event loop.

### Added

- **SASL PLAIN authentication** — When the server advertises SASL support and a
  NickServ password is configured, the bot authenticates during capability
  negotiation (before registration completes). This eliminates the timing race
  between NickServ IDENTIFY and `+R` channel joins. Falls back to traditional
  NickServ IDENTIFY if SASL fails. `AUTHENTICATE` payloads are redacted in logs.

- **Exponential reconnect backoff** — Reconnect delays now follow exponential
  backoff: 15s, 30s, 60s, 120s, 240s, capped at 5 minutes. Resets on successful
  connection.

- **Thread-safe `ChannelSet`** — `active_channels` is a proper thread-safe
  container (still uses `threading.Lock` because `enqueue()` may be called from
  thread pool executors).

- **User pruning** — User tracking entries older than 90 days (configurable via
  `user_max_age_days` in `config.ini`) are automatically pruned during store
  flushes.

- **Standalone test suite** — 79 tests in `tests/run_tests.py` covering protocol
  parsing, store, calculator, dice, weather merging/formatting, units, sender
  injection prevention, password hashing, ChannelSet, backoff, async sender
  (drain, priority bypass, thread-safe enqueue), and async handler verification
  (all module and core handlers confirmed as coroutines).

- **`protocol.py` extraction** — Pure protocol helpers (ISUPPORT parsing, MODE
  parsing, NAMES parsing, SASL payload encoding, tag stripping) in a separate
  module with no bot state or I/O.

### Changed

- **All command handlers are now coroutines** — Every module handler and every
  core command (auth, help, load, shutdown, etc.) is `async def`.  HTTP calls
  use `await asyncio.to_thread(requests.get, ...)` inside the handler.
  Password verification uses `await asyncio.to_thread(verify_password, ...)`.
  Pure computation (calc, dice, help text) runs directly in the event loop.

- **Channels module cleanup is an asyncio task** — The verification timeout
  garbage collector is now `asyncio.create_task(_cleanup_loop())` instead of a
  `threading.Thread`.  Created during `on_load()`, cancelled on `on_unload()`.

- **Type annotations everywhere** — All files use `from __future__ import
  annotations` with PEP 604 union syntax.  Every public function, method, and
  class attribute is annotated.

- **README updated** — Architecture section reflects async design, protocol.py,
  tests.  Module example uses async handlers.  SASL, backoff, pruning, testing
  documented.

### Fixed

- **Admin auth case-insensitive** — `_authed` now normalizes nicks to lowercase,
  matching IRC's case-insensitive nick semantics per RFC 2812. Previously, a
  case mismatch between auth and subsequent commands could silently drop admin
  status.

- **Hostmask capture now includes `user@` portion** — JOIN, NICK, and PRIVMSG
  regexes captured only the hostname after `@`, losing the ident/username. The
  `.users` display showed `nick!hostname` instead of `nick!user@hostname`, and
  `users.json` entries were inconsistent with the CHGHOST handler (which
  correctly stored `user@host`). All three regexes now capture the full
  `user@host` string.

- **Premature `active_channels.add` in `_on_invite` and `_deferred_rejoin`** —
  Both methods added channels to the active set and saved to disk before the
  server confirmed the JOIN. If the server rejected the JOIN, phantom entries
  persisted. Removed the premature adds; `_on_join` (triggered by the server's
  JOIN echo) now handles both add and save.

- **Missing JOIN error handlers for 403/405/476** — ERR_NOSUCHCHANNEL (403),
  ERR_TOOMANYCHANNELS (405), and ERR_BADCHANMASK (476) were unhandled, leaving
  phantom channels in `active_channels` and `channels.json`. Now handled
  alongside the existing 471/474/475 handlers.

- **Task done_callback safe after `_tasks.clear()`** — During reconnect, all
  tasks are cancelled and the list cleared. When cancelled tasks subsequently
  completed, their done callback called `list.remove()` on the empty list,
  raising `ValueError`. The callback now guards with an `in` check first.

- **`channels.py` uses `asyncio.get_running_loop()`** — Replaced deprecated
  `asyncio.get_event_loop()` call in `on_load()`.

- **Test suite expanded** — 6 new tests covering admin case-insensitivity,
  hostmask regex capture, JOIN error numerics, NICK regex, and done_callback
  safety. Total: 79 tests.

## [1.1.0] — 2026-03-03

Full codebase audit and hardening pass. 39 findings identified and resolved
across security, stability, architecture, and quality-of-life categories.
Includes hybrid weather data source merging, MODE/ISUPPORT parsing fixes,
and thread safety improvements found in the follow-up review.
See `AUDIT.md` for detailed forensic writeups of each finding.

### Added

- **Hybrid weather data merging** — Weather commands for US locations now query
  both NWS and Open-Meteo, merging results into a single output. NWS values
  take priority; Open-Meteo fills gaps (common for NWS stations that report
  null temperature, visibility, or humidity). Both sources return structured
  `WeatherDict` dicts instead of pre-formatted strings. A `_merge_current()`
  function combines them, and `_format_current()` produces the output. NWS heat
  index and wind chill labels are preserved through the merge.

- **Channel founder verification** — `.join` and `.part` now verify the
  requesting user is the registered channel founder via IRC services before
  acting. The bot WHOIS-es the user for their NickServ account, queries
  ChanServ/X3/etc. for the channel founder, and compares. Works across Anope,
  Atheme, Epona, X2, X3, and forks. Configurable via `services_nick` in
  `config.ini`. Bot admins bypass verification. `/INVITE` remains open.

- **`on_raw(line)` module hook** — Modules can now intercept raw IRC traffic
  (server numerics, NOTICEs, protocol messages) by overriding `on_raw()` in
  their `BotModule` subclass. The core dispatches every incoming line (after
  IRCv3 tag stripping) to all loaded modules. Used by the channels module for
  founder verification, available for any future module that needs protocol-level
  access.

- **Auth brute-force protection** — 5-minute lockout after 5 failed password
  attempts per nick. Counter resets after lockout expires or on successful auth.

- **Credential redaction in logs** — Outgoing `PASS`, `IDENTIFY`, and `OPER`
  commands are redacted in the sender's debug log. Incoming `AUTH` messages are
  redacted in the main loop. Command dispatch log redacts auth arguments.

- **Graceful shutdown** — `SIGTERM`, `SIGINT`, and the new `.shutdown` / `.die`
  admin command all trigger the same clean exit path: save channel list to disk,
  call `on_unload()` on every loaded module, send `QUIT` to the server, wait for
  the sender queue to flush, then exit. `.restart` also saves state and unloads
  modules before `execv`. Accepts an optional quit reason
  (e.g. `.shutdown maintenance window`).

- **`services_nick` config option** — New setting under `[bot]` for specifying
  the IRC services bot name. Defaults to `ChanServ`. Set to `X3`, `Q`, etc. for
  non-ChanServ networks.

- **Configurable user modes, oper modes, and snomask** — Three new `[irc]`
  config options: `user_modes` (applied after MOTD, e.g. `+ix`), `oper_modes`
  (applied after successful OPER, e.g. `+s`), and `oper_snomask` (server notice
  mask applied after OPER, e.g. `+cCkKoO`). All validated at startup. Also added
  `.mode` and `.snomask` admin commands for runtime changes without restart.

- **Runtime log control** — Two new admin commands: `.loglevel` and `.debug`.
  `.loglevel` with no args shows current state; `.loglevel WARNING` changes
  the base output level; `.loglevel internets.weather DEBUG` enables debug for
  a single subsystem.  `.debug on/off` toggles global debug.  `.debug weather`
  enables debug output for just the weather subsystem without flooding everything
  else — only that module's debug records appear in the main log and console.
  `.debug weather off` disables it.  Multiple subsystems can be debugged
  simultaneously.  `.rehash` resets all debug state to config defaults.

- **Log rotation** — Main log file and optional debug file are now rotated via
  `RotatingFileHandler`. New config options: `max_bytes` (default 5 MB),
  `backup_count` (default 3 rotated copies).

- **Dedicated debug file** — Optional `debug_file` setting in `[logging]`.
  When set, captures ALL log output at DEBUG level regardless of the main log
  level. Useful for post-mortem analysis of protocol issues without enabling
  verbose output in the main log.

- **Hierarchical logger names** — All modules use `internets.<name>` logger
  names (e.g. `internets.weather`, `internets.store`, `internets.sender`).
  Log format now includes the logger name, making it easy to grep for a
  specific subsystem's output.

- **CLI debug flags** — `--debug` enables global debug at startup.
  `--debug weather store` enables per-subsystem debug.  `--loglevel WARNING`
  overrides the config file level.  `--debug-file debug.log` enables a
  dedicated debug trace file.  `--no-console` disables the interactive
  stdin console.

- **Interactive console** — When running interactively (stdin is a TTY),
  the bot provides a `>` prompt accepting `debug`, `loglevel`, `status`,
  and `shutdown` commands without IRC auth.  `status` shows current nick,
  channels, modules, admin sessions, and log levels.  Auto-disabled when
  stdin is not a TTY (e.g. systemd, screen -dm).

- **Chanop tracking** — The core now parses `353` (NAMES) replies and `MODE`
  changes to track which users hold `~` (owner), `&` (admin), and `@` (op)
  status in each channel. Exposed via `bot.is_chanop(channel, nick)`. Maintained
  in real time across PART, QUIT, KICK, and NICK events.

- **Rate limiter cleanup** — Stale entries in the flood and API rate limiter
  dicts are now purged every 5 minutes, preventing unbounded memory growth on
  long-running instances.

### Changed

- **Calculator completely rewritten** — Replaced `eval()` with a recursive AST
  walker that only permits numeric literals, whitelisted math functions
  (`sin`, `cos`, `sqrt`, `factorial`, etc.), and basic arithmetic operators.
  No attribute access, no builtins, no comprehensions, no string operations.
  Exponents capped at 10,000, factorial capped at 170, nesting depth capped
  at 50.

- **Message splitting respects UTF-8 boundaries** — `_split_msg()` now backs up
  to the last valid UTF-8 character boundary instead of slicing mid-codepoint.
  CJK, emoji, and accented characters no longer garble at chunk boundaries.

- **Atomic JSON persistence** — All file writes in `store.py` now use
  write-to-temp + `os.replace()`. A crash during write cannot corrupt the data
  file.

- **Store rewritten: in-memory cache with periodic flush** — `store.py` no
  longer reads and writes JSON on every operation.  All data is loaded once at
  startup and mutated in memory.  A background thread flushes dirty datasets to
  disk every 30 seconds.  `graceful_shutdown` and `.restart` force an immediate
  flush.  Each dataset (locations, channels, users) now has its own lock, so a
  weather lookup never blocks behind a user-tracking write.  Public API is
  unchanged — zero module modifications required.

- **`_require_admin` and help header use live nick** — Auth hint messages and
  the help banner now reference `self._nick` instead of the stale `NICKNAME`
  constant, so they remain correct after a nick collision.

- **Dice output truncated for large rolls** — `.d 100d100` now shows only the
  first 10 individual rolls with a count note, instead of dumping all 100 values
  into the channel.

- **Restart flushes properly** — `cmd_restart` now sends `QUIT` *then* sleeps 2
  seconds, so the sender thread can actually flush the message before `os.execv`
  replaces the process.

- **Urban Dictionary module decoupled from weather config** — Falls back to a
  default User-Agent if the `[weather]` config section is missing.

- **Constant-time comparison uses stdlib** — `hashpw._ct_eq` now delegates to
  `hmac.compare_digest` instead of a hand-rolled Python loop.

### Fixed

- **Registration flood on connect** — `NICK`/`USER`/`CAP`/`PASS` were re-sent
  on every `recv()` iteration until MOTD arrived. Added a `registered` flag so
  they're sent exactly once per connection.

- **CAP LS parser destroyed capabilities** — The regex consumed capability names
  instead of stripping `=value` suffixes. Only the first capability survived
  negotiation. Replaced with `{cap.split("=",1)[0] for cap in params.split()}`.

- **Nick collision infinite loop** — `rstrip("_") + "_"` stripped all trailing
  underscores then added one, producing the same nick on consecutive collisions.
  Now simply appends `_`.

- **Self-detection broken after nick change** — All JOIN/PART/KICK/PM
  self-detection used the `NICKNAME` constant instead of `self._nick`. Channel
  tracking broke completely after any nick collision.

- **MOTD detection false-positives** — Substring match `"376" in line` triggered
  on PRIVMSGs, nicks, and server names containing those digits. Replaced with
  `re.match(r":\S+ (376|422) ", line)`.

- **PING handler crash** — Colon-less `PING` messages (valid per RFC 2812)
  caused `IndexError`. Handler now supports both formats.

- **Auth session hijack via nick change** — `_authed` set wasn't updated on
  NICK events. Users who changed nicks left their old nick as admin; anyone
  taking that nick inherited the session. Auth now migrates on NICK change.

- **`channels_load()` race condition** — Read without lock while `channels_save`
  wrote under lock. Concurrent access could yield partial JSON. Now locked.

- **`channel_users()` race condition** — Same class of bug as `channels_load`.
  Now locked.

- **Module dict thread safety** — `_modules` and `_commands` accessed from
  dispatch threads without synchronization during hot-reload. Added
  `_mod_lock` protecting all reads and writes.

- **Empty prefix crash** — Sending just the command prefix character (`.`)
  with nothing after it produced an empty list and `IndexError`. Guarded.

- **MODE arg desync corrupted chanop tracking** — The MODE parser hardcoded
  which modes consume parameters, ignoring the server's ISUPPORT CHANMODES
  and PREFIX values. Unknown modes (e.g. `L` type B, `H` type C) caused arg
  misalignment, shifting all subsequent parameters. A `+Loq` change would
  assign the wrong nicks to the wrong modes. Added 005 ISUPPORT parsing for
  both CHANMODES and PREFIX. MODE processing now handles all four CHANMODES
  types correctly. See AUDIT.md BUG-017.

- **Thread safety on `active_channels` iteration** — The `active_channels` set
  was modified from multiple threads without synchronization. `sorted()` on the
  set during concurrent mutation could crash. All iteration sites now use
  `set()` snapshots. See AUDIT.md BUG-018.

- **Gusts displayed when wind is zero** — `_format_current` showed gusts for
  any nonzero value when wind speed was 0 (`0 * 1.3 = 0` always passes).
  Added explicit `wind_kph > 0` guard. See AUDIT.md BUG-019.

- **Stale `fmt_dt` import in `nws.py`** — Unused import left over after the
  structured dict refactor. Removed.

### Security

- **RCE via `eval()` eliminated** — The calculator's `eval()` sandbox was
  trivially bypassable. Replaced with a safe AST walker. See AUDIT.md BUG-001.

- **Path traversal in module loader blocked** — `.load ../../evil` could execute
  arbitrary Python files outside the modules directory. Module names now validated
  against `^[a-z][a-z0-9_]*$`. See AUDIT.md SEC-002.

- **CRLF injection in IRC output blocked** — Embedded `\r\n` in outgoing
  messages could inject raw IRC protocol commands. Sender now strips all CR/LF.
  See AUDIT.md SEC-003.

- **Admin sessions cleared on reconnect** — After disconnect, authenticated
  nicks persisted but may belong to different people on the new connection.
  `_authed` is now cleared on every disconnect. See AUDIT.md SEC-005.

- **Admin password no longer logged** — Auth arguments were written to the log
  file at INFO level. Now redacted. See AUDIT.md SEC-001, SEC-004.

- **Auth brute-force lockout added** — No rate limiting existed on password
  attempts beyond the global 3-second flood gate. Now locks out after 5 failures
  for 5 minutes. See AUDIT.md SEC-006.

- **Non-atomic writes fixed** — A crash mid-write could corrupt JSON data files.
  Now uses atomic temp-file + rename. See AUDIT.md BUG-013.

- **Calculator DoS mitigated** — `factorial(99999)` could hang a thread; deeply
  nested expressions could blow the stack. Inputs and depth are now capped.
  See AUDIT.md BUG-015.

- **TLS 1.0/1.1 blocked** — SSL context now enforces `TLSv1_2` as the minimum
  version, preventing downgrade attacks to deprecated protocols.
  See AUDIT.md SEC-009.

- **Log injection prevented** — IRC content with embedded `\r\n` could forge log
  entries. A custom `_SafeFormatter` now strips all CR/LF/NUL from log messages
  before they reach any handler. See AUDIT.md SEC-007.

- **Error info disclosure fixed** — Raw Python exception details were sent back
  to IRC users in module load errors and unhandled command crashes. Now sends
  generic "see log for details" messages. See AUDIT.md SEC-008.

- **PRIVMSG/NOTICE target validation** — Empty or space-containing targets in
  `privmsg()` and `notice()` are now rejected, preventing protocol parameter
  injection within a single IRC line. See AUDIT.md BUG-027.

- **Symlink traversal in module loader blocked** — Symlinks in the modules
  directory pointing outside it could load arbitrary Python files. The loader
  now `resolve()`s paths and verifies they remain under `MODULES_DIR`.
  See AUDIT.md BUG-028.

- **IRC 512-byte line limit enforced** — Sender now truncates outgoing lines to
  510 bytes (plus `\r\n`) with UTF-8-safe boundary detection.
  See AUDIT.md BUG-026.

- **Concurrent task cap** — `_dispatch` now limits active command tasks to 50,
  preventing resource exhaustion from coordinated slow-command flooding.
  See AUDIT.md BUG-030.

- **Command argument length cap** — Arguments exceeding 400 characters are
  rejected before reaching any handler, preventing oversized input attacks.
  See AUDIT.md BUG-031.

- **Config file permission warning** — Startup now warns if `config.ini` is
  world-readable, since it contains credentials. See AUDIT.md BUG-029.

### Fixed (post-audit)

- **Channels not rejoined after reboot** — Invite-only (`+i`) channels silently
  failed to rejoin because the original invite expired on disconnect. The bot now
  handles 473 (ERR_INVITEONLYCHAN) by asking ChanServ to re-invite it. Also,
  NickServ identification now completes before rejoin attempts, so `+R` channels
  and ChanServ access lists work. Join errors 471 (full), 474 (banned), and 475
  (bad key) are logged and the channel is removed from the saved list.
