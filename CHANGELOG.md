# Changelog

All notable changes to the Internets IRC bot are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.0] — 2026-03-04

Quality and standards pass. Type annotations across the entire codebase, shipping
test suite, SASL support, exponential reconnect backoff, user pruning, and
thread-safe channel tracking.

### Added

- **SASL PLAIN authentication** — When the server advertises SASL support and a
  NickServ password is configured, the bot authenticates during capability
  negotiation (before registration completes). This eliminates the timing race
  between NickServ IDENTIFY and `+R` channel joins. Falls back to traditional
  NickServ IDENTIFY if SASL fails. `AUTHENTICATE` payloads are redacted in logs.

- **Exponential reconnect backoff** — Reconnect delays now follow exponential
  backoff: 15s, 30s, 60s, 120s, 240s, capped at 5 minutes. Resets on successful
  connection. Replaces the fixed 15s/30s delays that hammered the server during
  extended outages.

- **Thread-safe `ChannelSet`** — `active_channels` is now a proper thread-safe
  container with a lock, replacing the bare `set` with `set()` snapshot hacks.
  Supports `add`, `discard`, `__contains__`, `snapshot`, iteration, `__len__`,
  and `__bool__` — all thread-safe.

- **User pruning** — User tracking entries older than 90 days (configurable via
  `user_max_age_days` in `config.ini`) are automatically pruned during store
  flushes. Prevents unbounded `users.json` growth on busy networks.

- **Standalone test suite** — 64 tests in `tests/run_tests.py` covering protocol
  parsing (ISUPPORT, MODE, NAMES, SASL), store (CRUD, flush, atomic writes,
  pruning), calculator (arithmetic, sandboxing, DoS guards), dice, weather data
  merging and formatting, unit conversions, sender injection prevention, password
  hashing, ChannelSet, and exponential backoff. No external dependencies — runs
  with `python tests/run_tests.py`. Also compatible with pytest.

- **`protocol.py` extraction** — Pure protocol helpers (ISUPPORT parsing, MODE
  parsing, NAMES parsing, SASL payload encoding, tag stripping) extracted into a
  separate module. No bot state, no I/O — fully unit-testable.

### Changed

- **Type annotations everywhere** — All files now use `from __future__ import
  annotations` and PEP 604 union syntax (`str | None` instead of
  `Optional[str]`). Every public function, method, and class attribute has type
  annotations. Module `setup()` functions and `BotModule` subclasses are typed.

- **README updated** — Architecture section reflects `protocol.py` and `tests/`.
  SASL support, exponential backoff, user pruning, testing instructions, and
  credential redaction for AUTHENTICATE documented. Module example uses type hints.

## [1.1.0] — 2026-03-02

Full codebase audit and hardening pass. 32 findings identified and resolved
across security, stability, architecture, and quality-of-life categories.
See `AUDIT.md` for detailed forensic writeups of each finding.

### Added

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

### Fixed (post-audit)

- **Channels not rejoined after reboot** — Invite-only (`+i`) channels silently
  failed to rejoin because the original invite expired on disconnect. The bot now
  handles 473 (ERR_INVITEONLYCHAN) by asking ChanServ to re-invite it. Also,
  NickServ identification now completes before rejoin attempts, so `+R` channels
  and ChanServ access lists work. Join errors 471 (full), 474 (banned), and 475
  (bad key) are logged and the channel is removed from the saved list.
