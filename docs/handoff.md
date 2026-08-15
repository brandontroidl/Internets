# Handoff Binder

Assume every original developer disappears tomorrow. This document answers the
questions a new maintainer with a CS degree and production engineering
experience - but zero prior knowledge of this codebase - needs answered before
they can safely maintain, extend, debug, and deploy this system.

Read `docs/executive.md` first for the strategic picture.

---

## How is the system organized

```
internets.py          the bot core: event loop, IRC state machine, dispatch
admin_cmds.py         AdminCommandsMixin: every privileged admin command
console.py            interactive stdin admin console (TTY only)
protocol.py           pure IRC protocol parsers (stateless, no I/O)
sender.py             async outbound queue with token-bucket flood control
store.py              in-memory state + periodic disk flush, plus RateLimiter
config.py             config.ini parsing, CLI argparse, module-level constants
botlog.py             logging setup, startup validation, debug/loglevel handlers
hashpw.py             admin password hashing (scrypt/bcrypt/argon2)
secret_store.py       two-tier reversible credential store
audit_log.py          HMAC-chained tamper-evident privileged-action log
metrics.py            optional Prometheus exporter (off by default)
process_lock.py       PID-based single-instance guard

modules/
  base.py             BotModule base class + fetch_json, strip_ctrl, cred, resolve_public
  _netsafe.py         SSRF-safe fetch with DNS-TOCTOU pinning
  geocode.py          location resolution (Nominatim / Zippopotam)
  units.py            dual-unit formatting for weather output
  example.py          copy-and-fill module skeleton
  <60+ command modules>

weather_providers/
  __init__.py          provider registry + 14 public get_* functions
  base.py              frozen normalized dataclasses + WeatherProvider protocol
  _dispatch.py         capability discovery + accuracy/health ranking + fallback
  _health.py           per-provider EMA health score + circuit breaker
  _http.py             capped async HTTP client (aiohttp / requests fallback)
  <32 provider packages>

tests/
  run_tests.py         standalone pre-pytest test runner (regression tests)
  conftest.py          sys.path setup
  test_*.py            41 pytest modules

scripts/
  build-docs.sh        Sphinx HTML + PDF build
  regen-lockfile.sh    lockfile regeneration
  remap-doc-citations.py  doc citation updater
  sbom.sh              software bill of materials
  verify_install.sh    supply-chain install smoke test
```

The import order matters: `config.py` (imports `secret_store`) -> `botlog.py`
(imports from `config`) -> everything else. `config.py` and `botlog.py` both
run validation at import time and can `sys.exit(1)` before the event loop
starts.

---

## What is dangerous

### Code execution surfaces

1. **`.load <module>`** (`admin_cmds.py:475`, `internets.py:448`).
   `exec_module` runs arbitrary Python from any `.py` file in `MODULES_DIR`
   with the bot's full process privileges. The name is regex-constrained and
   path-traversal-checked, but anything already sitting in `modules/` is
   trusted. A compromised file in that directory is full code execution.

2. **`.raw <line>`** (`admin_cmds.py:603`). Injects an unvalidated IRC
   protocol line onto the wire. Only CR/LF/NUL and the 510-byte cap are
   enforced; the command itself (KILL, OPER, SAMODE) is whatever the admin
   types. The audit log records it (with credential redaction), but the
   blast radius is the full privilege of the bot's IRC connection.

3. **The interactive console** (`console.py`). Admin-equivalent capability
   (debug, shutdown) with no authentication. The security boundary is physical
   access to stdin. Run daemonized deployments with `--no-console`.

### State corruption risks

4. **Two instances on the same state files.** The process lock
   (`process_lock.py`) prevents this, but a manual deletion of
   `internets.pid` while the bot is running opens the race. The JSON state
   files use tmp-and-rename, so two writers' renames interleave.

5. **Editing `config.ini` while the bot is running.** The `configparser`
   object (`cfg`) is live in memory. `.rehash` / SIGHUP re-reads it.
   Concurrent edits are not locked; a partial write visible to a rehash
   produces a partial config. Edit, save, then `.rehash`.

### Security invariants that look like simplifications

6. **`is_admin()` hostmask re-check.** (`internets.py:343`). Looks like it
   could be simplified to a set membership test. It cannot. The hostmask
   re-derivation on every call prevents nick-grab session inheritance. The
   `"unknown"` sentinel denial prevents a TOCTOU where the admin quits during
   `verify_password`. Both bugs were observed in production. See
   `docs/security-model.md` section 1.

7. **Auth session drop on NICK/QUIT.** Sessions are dropped, not migrated.
   Migrating would let a malicious server or nick-takeover launder an authed
   session onto an attacker-chosen nick. See `internets.py:1065-1084`.

8. **DNS pinning in `_netsafe.py`.** The global `getaddrinfo` wrapper looks
   invasive. It is the SSRF defense. See ADR-003 in `docs/design-decisions.md`.

9. **The derived-field exclusion in weather gap-fill.** `feels_like_c` and
   `dewpoint_c` look like they should be gap-filled. They must not be. See
   ADR-010.

---

## What should never be touched

See `docs/executive.md`, "What should never change" for the full list. The
short version:

- The `is_admin()` fail-closed hostmask binding
- The `_tls_or_refuse` credential gate
- The netsafe DNS pinning mechanism
- The store's quarantine-on-bad-read behavior
- The audit log HMAC chain (do not downgrade to plain SHA-256)
- The outbound HTTP size-cap discipline (stream + cap before parse)
- The process lock `O_CREAT | O_EXCL` mechanism
- The derived-field invariant in weather gap-fill

---

## Where are the sharp edges

### Hot-reload only refreshes command modules

`.reload weather` re-executes `modules/weather.py` from disk. It does NOT
refresh helper modules (`geocode.py`, `units.py`, `_netsafe.py`, `base.py`)
or `weather_providers/*` because those are cached in `sys.modules`. An edit
to `geocode.py` is invisible until `.restart` (full `os.execv`). This is the
single most common gotcha for a new maintainer.

### Config constants are frozen at import

`config.py` parses `SERVER`, `PORT`, `NICKNAME`, `NS_PW`, `OPER_PW`,
`SERVER_PW`, and other constants at import time. `.rehash` re-reads `cfg` but
does not re-bind these constants. Changing a server address or NickServ
password requires a full restart. The `_cmd_prefix()` function reads the
prefix live (not from the frozen `CMD_PREFIX`) specifically to support rehash;
this is the exception, not the rule.

### The `config.ini` / `config.local.ini` overlay

`config.py`'s `reload_config()` reads both files in order. Re-reading
`config.ini` alone would clobber values that exist only in
`config.local.ini` (e.g., `password_hash`) with the template's empty
placeholders. Every reload path must go through `reload_config()`.

### Lockfile and restart interaction

`os.execv` preserves the PID. The restart path releases the process lock
before `execv`; otherwise the new process image would see its own old PID as
a live holder and refuse to start. See `internets.py:1444-1454`.

### Password length is bytes, not characters

`hashpw.py`'s `check_password` enforces a 128-byte UTF-8 limit, not
128 characters. A non-ASCII passphrase of 128 characters can be 384 bytes
and will be rejected. The bcrypt 72-byte limit is a hard algorithm
constraint, not a tunable. Both limits are enforced at hash time and at
verify time via the same function so the two ends cannot drift.

### scrypt cost is host-dependent

`_best_scrypt_params()` probes descending `(N, r, p)` sets and uses the
first one OpenSSL accepts. A host with a tighter memory cap silently gets a
weaker hash. The CLI prints which parameters were used but does not warn
about degradation.

### `_VALID_HASH_PREFIXES` in `botlog.py` is a separate enumeration

Adding a fourth hash algorithm to `hashpw.py` without updating
`_VALID_HASH_PREFIXES` in `botlog.py:177` makes the bot refuse to start on
a hash that `verify_password` can verify perfectly well.

### Shadow-ban persistence

`.shadow-ban` flushes to `shadow_bans.json` on disk. Bans survive a restart.
The file is not in the config template (`config.ini.example`), and one admin
can shadow-ban another admin (silently locking them out of every command
including `.deauth` and `.shutdown`).

### Module `forget()` coverage

`.forgetme` calls `forget()` on every loaded module. Five modules override
it: `seen`, `tell`, `notes`, `remind`, and `steam`. Saved locations are
erased through `privacy.py` -> `bot.loc_del()`, not through a module
`forget()` hook. Any new module that persists per-nick data must override
`forget()` or `.forgetme` will miss it.

### `sasl_password` secret is defined but never consumed

`sasl_password` is in `KNOWN_SECRETS` and in the config template, but the
SASL PLAIN auth path hardcodes `NS_PW` (the `nickserv_password` value). A
distinct `sasl_password` is silently ignored. See `docs/configuration.md`,
audit finding 2.

### `[weather] units` is a dead key

The config template ships `units = us` but no code reads it. Unit selection
is driven by per-provider API params, not this key. See
`docs/configuration.md`, audit finding 1.

### Metrics endpoint wiring

`metrics.py` defines a full Prometheus exporter. It IS wired into
`internets.py:1363-1370`, gated by `[metrics] enable = true`. The TODO
comment in `metrics.py:9` is stale. The counters/gauges are incremented from
various callsites regardless of whether the HTTP exporter is running.

---

## What technical debt exists

1. **`fetch_json` vs inline streaming.** Many modules use raw `requests` with
   hand-rolled streaming/size-cap logic instead of the shared
   `modules/base.fetch_json` helper. Functionally equivalent (all cap body
   size) but inconsistent with the single-helper convention.

2. **`dice.py` and `bofh.py` have no rate-limit gate**, unlike every other
   command module. Low severity (both are local, no network), but
   inconsistent.

3. **`KEY_ROTATION.md` reference in `hashpw.py:285`.** No such file exists.
   Stale documentation debt.

4. **bcrypt 4.3.0 installed vs 5.0.0 pinned.** The lockfile may lag behind
   `pyproject.toml`'s declared floor. Run `pip install -r requirements.txt`
   to reconcile.

5. **`idlerpg.py` default endpoint is plain HTTP.** The configured default
   `http://idlerpg.rizon.net/xml.php` leaks query params in cleartext.

6. **Console `.debug WEATHER` vs IRC `.debug WEATHER` case handling.**
   The console preserves case but subsystem names are lowercase, so
   uppercase at the console matches no real logger. IRC lowercases the
   entire arg. Use lowercase at the console.

---

## What should be refactored first

Priority order, highest value first:

1. **Consolidate HTTP helpers.** Migrate modules still using raw `requests`
   to `fetch_json`, or factor out the streaming/cap pattern so there is
   genuinely one path.

2. **Wire `sasl_password` or remove it.** Either implement the documented
   "differs from nickserv" behavior or remove the secret from
   `KNOWN_SECRETS` and the template.

3. **Remove the dead `[weather] units` key** from the template and its
   comment.

---

## What should be monitored

1. **`*.corrupt.*` files in the working directory.** Any quarantine event
   means a state file was corrupt on load. Investigate the cause (power loss,
   disk full, racing instance).

2. **`audit.log` chain integrity.** Run `.audit verify` periodically or
   after any suspect event. A break at a specific index indicates that
   record was tampered with or the log was not cleanly closed.

3. **Provider health via `.providers`.** An admin-only command that dumps
   the per-provider health score, success rate, latency, circuit breaker
   state, and capability matrix. A provider in `open` breaker state for an
   extended period may indicate a revoked API key.

4. **Log for `event=isupport_malformed`.** Indicates the IRC server sent
   a malformed CHANMODES or PREFIX token. MODE-based chanop tracking may
   be degraded until the next clean 005.

5. **Log for `dispatch_budget_exhausted`.** The 45s chain budget was
   consumed before all providers were tried. Either upstream providers are
   slow or the chain is too deep for the budget.

6. **`dropped_messages` in `.stats` or Prometheus.** Non-zero means the
   outbound queue hit its 200-message cap and started discarding. Either
   the bot is sending too much or the network is slow.

---

## Operational procedures

### First-time setup

```bash
git clone <repo>
cd Internets
pip install -r requirements.txt
python -m secret_store init          # creates config.ini from template, 0600
python hashpw.py --algo argon2       # generates admin password hash
# paste the output into config.ini [admin] password_hash
# set [irc] server, port, nickname, realname
# set [secrets] nickserv_password (if needed)
# set API keys in [secrets] as desired
python internets.py
```

### Upgrading

See `docs/deployment.md`, "Upgrade procedure". Read the CHANGELOG first.
Stop gracefully, pull, reinstall deps, merge new template keys, run
`python tests/run_tests.py`, start.

### Backup

Back up together: `config.ini`, `audit.log`, `audit.log.key`, all `*.json`
state files, the `*.bak` and `*.corrupt.*` recovery files, and the rotated
`audit.log.*` segments. `config.ini` contains secrets; handle accordingly.

### Disaster recovery

If a state file is corrupt on startup, the bot quarantines it and starts
with empty state. Check for `<name>.corrupt.<timestamp>` and
`<name>.bak` files. The `.bak` is a one-deep backup from the last
successful write. Manual inspection and restoration is the recovery path.

If the audit log key is corrupt, it is backed up to `audit.log.key.bad`
and a new key is generated. The old log segments are still verifiable with
the old key (if recovered from `.key.bad`); new records start a fresh
chain.

If the process lock file is stale (the bot was `kill -9`'d), the next
start auto-clears it if the PID is dead. If the PID was reused by an
unrelated process, or the lockfile names a different host, delete
`internets.pid` manually after confirming no instance is running.

---

## File reference: core

### `internets.py`

Main bot core. Asyncio IRC client: connect, register, negotiate IRCv3
capabilities (SASL, multi-prefix, account-notify, chghost, extended-join,
server-time, message-tags), process inbound lines, dispatch commands to
module handlers, manage reconnect/shutdown lifecycle. Process entry point.

**Key classes/functions:**

- `IRCBot` (line 176) - central class inheriting `AdminCommandsMixin`. Owns
  connection state, module registry, auth sessions, chanop tracking,
  shadow-ban list, and the main event loop.
- `ChannelSet` (line 96) - thread-safe `set[str]` wrapper for active
  channels (lowercased).
- `_backoff` / `_backoff_jittered` (lines 125, 140) - exponential backoff
  with bounded equal-jitter for reconnect delays.
  `random.SystemRandom` (not `secrets`) to satisfy Bandit B311.
- `_redact_inbound` (line 67) - masks credentials in inbound PRIVMSG/NOTICE
  trailing text before debug logging, scoped to the trailing portion to avoid
  false-matching hostmasks.
- `_process` (line 863) - hot path: strips IRCv3 tags, handles PING/PONG,
  applies shadow-ban filtering for module `on_raw` fanout, then chains to
  `_handle_cap`, `_handle_numeric`, `_handle_membership`, `_handle_privmsg`.
- `_handle_cap` (line 909) - full CAP LS/ACK/NAK/NEW negotiation plus SASL
  PLAIN flow. Sets `_sasl_failed_permanently` on 904/905 to abort reconnect
  when auth is broken.
- `_handle_membership` (line 1037) - tracks JOINs, PARTs, KICKs, QUITs,
  NICKs, CHGHOST, ACCOUNT. Revokes admin auth on QUIT and NICK change
  (identity change drops session, never migrates).
- `_dispatch` (line 632) - command dispatch: shadow-ban drop, PM-only gate
  for auth/deauth, flood/channel rate limiting, arg length cap, `_MAX_TASKS`
  concurrency cap (O(1) counter check), 60s per-command timeout.
- `run` (line 1171) - main loop: signal setup (SIGINT/SIGTERM -> shutdown,
  SIGHUP -> rehash), initial connect with bounded backoff, IRC registration
  (PASS/CAP/NICK/USER), readline loop racing `_stop` event, reconnect logic
  distinguishing transient vs permanent (SASL) failures.
- `_main` (line 1403) - async entry: instantiates bot, optionally enables
  Prometheus metrics, spawns console task if TTY, runs bot, handles
  console-first-exit gracefully, manages `os.execv` restart path (releases
  process lock before exec).
- `_entry` (line 1519) - process entry: drop-root guard (refuses euid 0
  unless `INTERNETS_ALLOW_ROOT=1`), acquires `ProcessLock`, runs
  `asyncio.run(_main)`.

**Invariants:**

- `_RE_NICK` (line 231) must accept bare-nick prefixes (no `!user@host`),
  otherwise self-NICK changes from services are silently dropped, causing the
  bot to stop recognizing its own nick.
- ISUPPORT tables (`_chanmode_types`, `_prefix_modes`) are reset on every
  reconnect (line 789) because DNS round-robin can land on a different
  server. Malformed CHANMODES/PREFIX tokens are rejected without replacing
  the current table (prevents silent parameter misalignment in MODE parsing).
- `is_admin` (line 378) fails closed: unknown/missing/changed hostmask
  revokes the session. A sentinel `"unknown"` value or a `None` current
  hostmask both deny.
- Auth sessions are cleared on disconnect (line 1316) and on SIGHUP rehash
  (line 1393).
- `_cmd_prefix()` (line 620) reads `cfg` live at use-time so a rehash
  changes the prefix without restart; the import-time constant `CMD_PREFIX`
  is only a fallback.
- `_split_msg` (line 362) splits on UTF-8 byte boundaries to avoid cutting
  multi-byte characters.
- Shadow-ban filtering skips module `on_raw` but still runs internal handlers
  (CAP, numerics, membership), so the bot tracks banned users for ops while
  they remain invisible to `.seen`/`.tell` etc.
- On the restart path (line 1485), the process lock must be explicitly
  released before `os.execv` because execv preserves the PID, and
  stale-detection would see it as live.

**IRC events handled:** PING, PONG, CAP (LS/ACK/NAK/NEW), AUTHENTICATE,
903/902/904/905 (SASL), 421 (unknown command), 451 (not registered), 433
(nick in use), 005 (ISUPPORT), 473 (invite-only), 403/405/471/474/475/476
(join errors), 381/491 (OPER), 900 (logged in), NickServ NOTICE, 353
(NAMES), MODE, CHGHOST, ACCOUNT, INVITE, JOIN, PART, KICK, QUIT, NICK,
PRIVMSG, 376/422 (end of MOTD).

### `admin_cmds.py`

Mixin class (`AdminCommandsMixin`) containing all admin/core IRC command
handler coroutines. Separated from `IRCBot` so the main class stays focused
on connection and state.

**Key functions:**

- `_require_admin` (line 87) - guard that checks `is_admin` and sends an
  "auth first" notice on failure.
- `_audit` (line 93) - writes an audit record via `audit_log.default().record()`.
  Strips C0/C1 control bytes from actor/hostmask via `_clean_actor` (line 33)
  to prevent format-byte injection in durable records. Failures are caught and
  warned, never propagated.
- `cmd_auth` (line 116) - password verification with brute-force lockout (5
  fails, 300s sliding window). Pre-snapshots hostmask before
  `verify_password` (runs in thread via `to_thread`), rejects if hostmask
  changed during async verify (TOCTOU defense). Audit-logs failures (capped
  at lockout threshold to prevent log rotation churn).
- `cmd_help` (line 270) - progressive disclosure: default shows modules
  grouped by `_MODULE_GROUPS`, `.help <module>` shows module commands,
  `.help <cmd>` shows specific line, `.help all` shows full grid, `.help
  admin` shows admin commands.
- `cmd_rehash` (line 487) - re-reads both `config.ini` and
  `config.local.ini`, resets log level and debug subsystems, validates
  password hash prefix, clears all admin sessions.
- `cmd_raw` (line 557) - sends arbitrary IRC line. Rejects CR/LF/NUL and
  lines > 510 bytes. Wire gets full line; echo/log/audit get
  credential-redacted copy via `redact_secrets`.
- `cmd_fingerprint` (line 790) - cross-references a nick across hostmask
  cache, channel tracking, shadow-ban list, seen.json, tells.json,
  notes.json, and audit log mentions.
- `cmd_stats` (line 674) - displays uptime, module counts, traffic counters,
  sender queue depth, audit record count, RSS memory (reads
  `/proc/self/status` on Linux).

**Invariants:**

- Auth lockout uses a sliding window: any attempt while locked refreshes
  the timer (line 165), preventing one-attempt-per-window bypass.
- `cmd_auth` re-reads `_auth_fails` inside the lock after the async verify
  (lines 194, 223) because a concurrent attempt could have bumped the counter
  during the thread-offloaded hash.
- Auth refuses if hostmask is unknown or changed between pre-snapshot and
  post-verify (lines 200-209) - defense against TOCTOU where a nick is
  reused during the hash computation.
- Failed-auth audit records are capped: once locked out, no more records are
  written, preventing audit log rotation churn under sustained brute-force.
- `cmd_rehash` clears admin sessions but does NOT reload import-time
  credential constants (NS_PW, OPER_PW, SERVER_PW) - that requires a
  restart.
- `_MODULE_GROUPS` (line 44) categorizes modules for `.help` display;
  unlisted modules fall into "More".

**Commands handled:** auth, deauth, help, version, modules, load, unload,
reload, reloadall, restart, rehash, mode, snomask, raw, say, act, nick,
uptime, stats, audit, fingerprint, shadow-ban, shadow-unban, shadow-list,
loglevel, debug, shutdown/die.

### `console.py`

Interactive stdin console running as an async task alongside the bot.
Admin-equivalent local access for debug/loglevel/status/shutdown without IRC
authentication.

**Key functions:**

- `should_skip_console` (line 42) - returns True when stdin is not a TTY.
  Prevents granting admin equivalence to piped input.
- `_console_dispatch_loop` (line 62) - synchronous blocking `input()` loop
  on a daemon thread. Dispatches `debug`, `loglevel`, `status`,
  `shutdown`/`quit`, `help`.
- `run_console` (line 105) - async wrapper that spawns the dispatch loop
  on a `daemon=True` thread and awaits an `asyncio.Event`.
- `_print_status` (line 154) - prints version, nick, channels, loaded
  modules, authed admins, and log level to stdout.

**Invariants:**

- The dispatch loop runs on a `daemon=True` `threading.Thread`, NOT
  `asyncio.to_thread`. `input()` blocks on a `read(0)` syscall that nothing
  can interrupt. A non-daemon thread on the default executor would make
  `asyncio.run()`'s `shutdown_default_executor()` hang forever on shutdown.
- `internets.py` closes `sys.stdin` during shutdown (line 1470) to unblock
  the `input()` call (causes `EOFError`).
- Emits a loud `WARNING` log on entry documenting that the console grants
  admin equivalence without authentication.

**Console commands:** `help`, `debug [on|off|<subsystem>]`,
`loglevel [LEVEL|<logger> LEVEL]`, `status`, `shutdown [reason]`, `quit`.

### `protocol.py`

Pure IRC protocol parsing helpers. No bot state, no I/O. Extracted for
testability.

**Key functions:**

- `strip_tags` (line 15) - removes IRCv3 `@tag=value` prefix from a line.
- `parse_isupport_chanmodes` (line 21) - parses `CHANMODES=A,B,C,D` into
  `{mode_char: type_letter}`. Returns `None` (not empty dict) on structural
  invalidity (fewer than 4 comma-separated groups), so the caller keeps its
  current table rather than replacing with a partial one.
- `parse_isupport_prefix` (line 52) - parses `PREFIX=(modes)symbols` into
  `(mode_set, {symbol: mode})`. Returns `None` on malformed input. Distinct
  from `(set(), {})` which is the well-formed empty case.
- `parse_mode_changes` (line 71) - parses a channel MODE string + args into
  `[(adding, mode_char, param|None)]`. Correctly consumes parameters based on
  ISUPPORT type classification: A/B always take param, C takes param only when
  setting, D never takes param, prefix modes always take param.
- `parse_names_entry` (line 109) - parses a NAMES entry like `~@nick` into
  `(nick, is_op)`. `~`(owner), `&`(admin), `@`(op) count as chanop;
  `%`(halfop), `+`(voice) do not.
- `sasl_plain_payload` (line 122) - builds base64-encoded SASL PLAIN
  payload: `\0nick\0password`.

**Invariants:**

- `parse_isupport_chanmodes` requires `token.count(",") >= 3` - a truncated
  token like `beI` (no commas) yields a non-empty dict that would silently
  drop type B/C modes, causing parameter misalignment in MODE parsing (e.g.,
  channel key `k` consumes no parameter, shifting all subsequent params).
- `parse_isupport_prefix` returns `None` for malformed vs `(set(), {})` for
  well-formed-empty. The caller must distinguish these: one means "keep
  current table", the other means "server has no membership prefixes."

### `sender.py`

Async priority send queue with token-bucket rate limiting for outbound IRC
traffic. Provides credential redaction for log output.

**Key classes/functions:**

- `Sender` (line 72) - priority queue (`asyncio.PriorityQueue`) with two
  tiers: priority 0 (protocol: PONG/CAP/NICK/QUIT) bypasses rate limit,
  priority 1 (normal: PRIVMSG/NOTICE) is rate-limited. Token bucket: 5
  burst, 1 token per 1.5s (~40 msg/min sustained).
- `enqueue` (line 172) - thread-safe. Uses `call_soon_threadsafe` to schedule
  `_safe_put` on the event loop. Monotonic sequence number ensures FIFO
  within same priority.
- `_safe_put` (line 128) - on a full queue (200 cap), priority-0 traffic
  evicts the worst (highest priority/seq) entry from the heap to guarantee
  protocol traffic always enqueues. Non-priority-0 messages are silently
  dropped.
- `_drain` (line 205) - consumer coroutine: dequeues, applies token-bucket
  wait for priority > 0, calls `_write_line`, then `drain()`.
- `_write_line` (line 183) - sanitizes (strips CR/LF/NUL), enforces 512-byte
  IRC line limit (minus 2 for CRLF), avoids splitting multi-byte UTF-8,
  redacts credentials in log output only (wire gets full message).
- `redact_secrets` (line 45) - masks the argument after the first credential
  verb (`AUTHENTICATE`, `IDENTIFY`, `REGISTER`, `IDENT`, `OPER`, `PASS`,
  `AUTH`) with `[REDACTED]`. Word-boundaried regex, longest-match-first
  ordering.
- `_SECRET_VERBS` (line 33) - canonical list of credential-bearing IRC verbs.
  Single source of truth for both inbound and outbound redaction.

**Invariants:**

- `MAX_QUEUE = 200` (line 81) bounds queue to prevent OOM during disconnects.
- Priority-0 eviction (lines 142-163) directly manipulates
  `PriorityQueue._queue` (the internal heap) - acceptable because losing a
  PONG causes server-side ping timeout and a reconnect storm worse than the
  queue eviction.
- `_seq_lk` (threading.Lock) protects the monotonic sequence counter from
  concurrent `enqueue` calls from different threads.
- The `on_drop` callback (passed by IRCBot) increments the bot's in-process
  dropped-message counter, which feeds the shutdown summary.

### `store.py`

In-memory state store (locations, channels, users) with periodic background
flush to JSON files. Provides per-nick opt-out support and rate limiting.

**Key classes/functions:**

- `Store` (line 106) - three datasets (locations, channels, users) each with
  their own `threading.Lock`. Background daemon thread flushes dirty datasets
  to disk every 30s. Data loaded once at startup.
- `_read` (line 150) - reads JSON, unwraps v2 envelope (validates SHA-256
  checksum), rejects type mismatches. On any failure, quarantines the corrupt
  file (renames to `<name>.corrupt.<timestamp>`) instead of silently loading
  empty and letting the next flush overwrite the only copy.
- `_write` (line 192) - atomic write via `mkstemp` + `os.replace`. Wraps data
  in v2 envelope with checksum. Sets 0o600 perms before rename. Creates a
  `.bak` backup of the previous file.
- `_prune_users` (line 272) - removes entries older than `_user_max_age`
  (default 90 days). Never prunes opted-out records (privacy preference
  outlives inactivity window).
- `set_opt_out` / `is_opted_out` (lines 427, 453) - privacy opt-out flag.
  Creates a sentinel entry in synthetic `"*"` channel if user isn't tracked
  anywhere yet.
- `RateLimiter` (line 464) - three independent rate windows: per-nick flood
  (default 3s, admin bypass), per-nick API (default 10s), per-channel sliding
  window (20 commands per 10s). All cooldowns floored at 1s.

**Invariants:**

- `user_max_age_days` is floored at 1 (line 125): a 0/negative value would
  make the cutoff `== now`, wiping all tracked users (including their opt-out
  flags) on the first flush.
- `RateLimiter` cooldowns are floored at 1s (lines 489-490) both here and
  in `config.py` (defense in depth): a zero cooldown makes
  `now - ts < cd` never true, silently disabling the limiter.
- Channel rate limiter does NOT record the attempt when over budget
  (line 557-558), so attackers can't keep the window permanently full.
- `_MAX_FILE_SIZE = 10 MB` (line 147) caps data file reads.
- The flush thread catches and logs exceptions without dying (line 243).
- `_quarantine` (line 177) renames corrupt files instead of deleting them.

### `config.py`

Loads `config.ini` (and `config.local.ini` overlay) at import time. Parses
all bot constants, handles CLI argument parsing, provides `reload_config()`
for runtime rehash.

**Key functions/constants:**

- `reload_config` (line 43) - re-reads both config files into the live `cfg`
  dict. Pinned to UTF-8 encoding. Returns list of files actually read.
- `_secret_or_cfg` (line 24) - two-tier credential lookup:
  `secret_store.get(name)` first, then `cfg[section][key]` fallback.
- `cfg` (line 36) - `configparser.ConfigParser` instance, the live config
  dict read at use-time for hot-reloadable values.
- `cli_args` (line 138) - parsed `argparse.Namespace` with `--debug`,
  `--loglevel`, `--debug-file`, `--no-console`.
- Exported constants (lines 81-111): `SERVER`, `PORT`, `NICKNAME`, `REALNAME`,
  `NS_PW`, `SERVER_PW`, `OPER_N`, `OPER_PW`, `USER_MODES`, `OPER_MODES`,
  `OPER_SNOMASK`, `CMD_PREFIX`, `API_CD`, `FLOOD_CD`, `MODULES_DIR`,
  `AUTO_LOAD`, `DESIRED_CAPS`, `LOG_LEVEL`, `LOG_FILE`, `LOG_MAX`,
  `LOG_BACKUPS`, `LOG_DEBUG`, `LOG_FMT`, `__version__` ("5.0.0").

**Invariants:**

- `CMD_PREFIX` must be non-empty (line 97) - an empty prefix makes every
  message a command. Enforced with `SystemExit`.
- `API_CD` and `FLOOD_CD` are floored at 1 (lines 102-103) - defense in
  depth against disabling rate limiting.
- Credential constants (`NS_PW`, `SERVER_PW`, `OPER_PW`) are import-time -
  NOT refreshed by `reload_config()` or SIGHUP.
- `DESIRED_CAPS` includes `sasl`, `multi-prefix`, `away-notify`,
  `account-notify`, `chghost`, `extended-join`, `server-time`,
  `message-tags`.
- Fails loud on missing `config.ini` (line 72) with an actionable
  `SystemExit` message.

### `botlog.py`

Configures the `internets` logger hierarchy at import time. Rotating file
handler, console handler, optional debug file, safe formatter (strips
control characters), and `DebugFilter` for per-subsystem debug control.

**Key classes/functions:**

- `_SafeFormatter` (line 28) - strips C0 controls (except TAB), DEL, and C1
  controls from `record.msg` and `record.args` to prevent log injection.
- `DebugFilter` (line 64) - logging filter. Passes records if: level >=
  base_level, OR global_debug is True, OR the record's logger name matches
  an active subsystem. Thread-safe subsystem set management.
- `_setup_logging` (line 112) - creates `RotatingFileHandler` (5MB, 3
  backups) + `StreamHandler(stdout)` + optional debug file handler.
- `get_hash` (line 164) - re-reads layered config and returns current
  `password_hash` from `[admin]` section.
- `_validate_hash` (line 180) - startup validation: `sys.exit(1)` if the
  hash has an unrecognized algorithm prefix. Empty hash is not fatal
  (intentional for first-run).
- `apply_debug` (line 237) - shared helper for IRC `.debug` and console.
- `apply_loglevel` (line 259) - shared helper for `.loglevel` and console.

**Invariants:**

- `_validate_hash` runs at import time and calls `sys.exit(1)` on invalid
  hash prefix. Intentional: an unrecognized prefix would make
  `verify_password` raise `ValueError` on every auth attempt.
- Config file world-readability warning checks `st_mode & 0o004` on POSIX.
- Mode string validation rejects anything outside `[a-zA-Z+\- ]` with
  `sys.exit(1)`.

### `hashpw.py`

Password hashing and verification supporting three algorithms (scrypt,
bcrypt, argon2id). Used both as CLI tool to generate hashes and as library
for runtime auth verification.

**Key functions:**

- `hash_scrypt` (line 207) - scrypt with probed parameters (starts at
  N=2^17 per OWASP 2024, walks down if OpenSSL refuses). 32-byte salt,
  64-byte derived key.
- `hash_bcrypt` (line 232) - bcrypt with configurable rounds (default 13,
  env `INTERNETS_BCRYPT_ROUNDS`). Refuses input > 72 bytes.
- `hash_argon2` (line 258) - argon2id with configurable memory (default
  128 MiB, env `INTERNETS_ARGON2_MEM_MIB`) and time cost (default 3,
  env `INTERNETS_ARGON2_TIME`). OWASP 2024 recommended.
- `verify_password` (line 279) - dispatches on stored hash prefix
  (`scrypt$`, `bcrypt$`, `argon2$`). Each stored hash carries its own
  parameters.
- `_verify_bcrypt` (line 314) - refuses candidates > 72 bytes (fails
  closed) to prevent bcrypt truncation bypass.
- `check_password` (line 142) - shared validation: min 8 chars, max 128
  UTF-8 bytes, bcrypt 72-byte limit, rejects leading/trailing whitespace.
- `_best_scrypt_params` (line 181) - probes downward from OWASP-strong to
  weakest acceptable.
- `_ct_eq` (line 349) - constant-time comparison via `hmac.compare_digest`.

**Invariants:**

- `MAX_PASSWORD_BYTES = 128` must stay <= `IRCBot._MAX_ARG_LEN` (400).
- `BCRYPT_MAX_PASSWORD_BYTES = 72` is the algorithm's hard input limit.
  bcrypt < 5.0 silently truncates (auth bypass); bcrypt >= 5.0 raises
  ValueError.
- Stored hash format is self-describing: `algo$params$salt$dk`.
- CLI warns if hash takes < 50ms (too weak) or > 1s (latency issue).

### `secret_store.py`

Two-tier secret store for outbound credentials. Lookup: env var
(`INTERNETS_<NAME>`) first, then `config.ini[secrets]` section (requires
0o600 perms). Provides CLI for managing secrets and migrating plaintext.

**Key functions:**

- `get` (line 180) - tiered lookup. Refuses to read `config.ini` if perms
  are looser than 0o600. Filters out placeholder values.
- `set_value` (line 214) - writes to `[secrets]` via targeted text-based
  edit (preserves comments). Rejects newlines in values.
- `delete` (line 233) - removes key from `[secrets]`. Raises
  `PermissionError` on bad perms so a failed delete isn't reported as
  "not found".
- `perms_ok` (line 161) - checks file is exactly 0o600. Fails closed.
  Windows skips.
- `migrate` (line 462) - moves all known plaintext secrets from their
  source config sections into `[secrets]`, then scrubs the source keys.
- `_atomic_write_text` (line 296) - creates file with 0o600 from `os.open`
  (no readable window), then `os.replace` for atomicity.
- `_cmd_init` (line 591) - bootstraps `config.ini` from
  `config.ini.example` with 0o600 perms.
- `KNOWN_SECRETS` (line 57) - ~40 canonical secret names across IRC auth,
  weather providers, and module API keys.

**Invariants:**

- `SECRETS_FILE` is `config.ini` itself - runtime config and secrets share
  one file.
- `_PLACEHOLDERS` matched case-insensitively. Placeholder env vars are also
  filtered.
- `_safe_exc` returns only `type(e).__name__` - exception messages can echo
  secret fragments.
- CLI `get` confirms presence without printing the value.

**CLI subcommands:** `status`, `list`, `get <name>`, `set <name>`,
`delete <name>`, `migrate`, `init`.

### `audit_log.py`

Append-only, HMAC-SHA-256-chained audit log for privileged bot actions. Each
record carries an HMAC over the previous record's hash plus current fields,
forming a tamper-evident chain.

**Key classes/functions:**

- `AuditLog` (line 107) - thread-safe audit log with lazy initialization.
- `record` (line 236) - appends a new entry. Computes HMAC-SHA-256 over
  `(prev_hash, ts, actor, host, action, args_str)` using NUL-separated
  canonical form. Opens in append-binary, sets 0o600. Rotates to
  `audit.log.<timestamp>` when > 5MB.
- `verify` (line 296) - re-walks the chain. v2 records verified with HMAC;
  legacy records fall back to plain SHA-256. Returns `(True, -1)` if intact,
  `(False, idx)` on first broken record.
- `_load_key` (line 131) - returns HMAC key from `audit.log.key` sidecar
  (0o600). Generates fresh 32-byte key on first use. Refuses to overwrite
  existing but unreadable key (would void all prior HMACs).
- `_load_tip` (line 177) - walks the file to find the last record's
  `this_hash`. Returns genesis hash if file doesn't exist.
- `_stable_args_str` (line 63) - deterministic args serialization.
- `_canonical` (line 81) - NUL-separated byte concatenation (NUL prevents
  field-boundary collisions).
- `default` (line 382) - module-level singleton factory, double-checked
  locking.

**Invariants:**

- HMAC key sidecar is separate from the log so an attacker with only
  `audit.log` cannot recompute the chain.
- `_load_key` refuses to regenerate over existing-but-unreadable key
  (line 143) - regenerating silently voids every prior record's HMAC.
- Rotation starts a fresh chain (new genesis), so each rotated segment is
  independently verifiable.
- `_RECORD_VERSION = 2` distinguishes HMAC-chained from legacy SHA-256.
- Pure tail truncation is undetectable from the file alone - needs a remote
  append-only sink.

### `metrics.py`

Pure-stdlib Prometheus text-format metrics registry with optional HTTP
exporter. Disabled by default - zero network footprint until `enable()` +
`expose()` are called.

**Key classes/functions:**

- `MetricsRegistry` (line 132) - holds counters/gauges, renders Prometheus
  text format.
- `Counter` (line 100) - monotonically increasing. Rejects negative
  increments.
- `Gauge` (line 113) - arbitrary up/down value.
- `expose` (line 256) - starts HTTP server on a daemon thread. Refuses to
  bind to `0.0.0.0`/`::`/any unspecified address (loopback-only). Only
  serves `/metrics`.

**Pre-registered metrics:** `internets_commands_total` (by module/command),
`internets_provider_calls_total`, `internets_provider_quota_used`,
`internets_reconnects_total`, `internets_dropped_messages_total`,
`internets_audit_records_total`, `internets_module_loaded`,
`internets_provider_active`, `internets_sender_queue_depth`,
`internets_authed_admins_count`.

**Invariants:**

- Binding to `0.0.0.0` rejected (line 281) - uses
  `ipaddress.ip_address().is_unspecified` to catch all zero-address forms.
- `_format_value` handles `bool` defensively (bool is an int subclass).
- Empty metrics emit a zero sample so Prometheus scrapers see the series.

### `process_lock.py`

PID-based process lockfile preventing concurrent instances from corrupting
shared state files.

**Key classes/functions:**

- `ProcessLock` (line 99) - context manager. Stores
  `pid|start_time|hostname` in the lockfile.
- `acquire` (line 142) - checks existing lockfile. Same host: probes PID
  liveness via `os.kill(pid, 0)`. Different host: refuses conservatively.
  Creates atomically via `O_CREAT | O_EXCL`.
- `release` (line 220) - verifies lockfile still contains our PID before
  unlinking (prevents deleting another instance's lock). Idempotent.
- `_pid_is_alive` (line 61) - POSIX: `os.kill(pid, 0)`. `PermissionError`
  treated as alive (conservative).

**Invariants:**

- PID reuse on the same host: if reused by an unrelated process,
  `os.kill(pid, 0)` returns True and the lock is conservatively refused.
- Different-host lockfiles (e.g., shared NFS) are always treated as live.
- `release` re-reads the file to confirm PID match before unlinking.
- On the `os.execv` restart path, the lock must be explicitly released
  before exec (PID is preserved).

---

## File reference: modules

### Infrastructure modules

#### `modules/base.py`

Foundation module defining `BotModule` base class, shared HTTP helpers
(`fetch_json`, `resolve_public`), credential loader (`cred`), output
sanitizer (`strip_ctrl`), and help formatting (`help_row`).

- `BotModule` - base class; `COMMANDS` dict maps command words to async
  method names. `__init_subclass__` validates the mapping at
  class-definition time (typo or non-async handler raises `TypeError` at
  import, not at runtime). Hooks: `on_load`, `on_unload`, `on_raw`,
  `forget(nick)`.
- `fetch_json(url, ...)` - streams response body, hard-caps at
  `max_bytes + 1` (default 256 KB), raises `ResponseTooLarge` before
  decoding. `allow_404=True` returns `None` on 404.
- `resolve_public(host, port)` - DNS resolution + SSRF guard: refuses
  private/loopback/link-local/multicast/reserved/unspecified addresses.
- `cred(cfg, secret_name, section, key, default)` - two-tier credential
  loading: `secret_store` first, `config.ini` fallback. Placeholders
  filtered.
- `strip_ctrl(s, max_len=400)` - drops full C0 range `\x00-\x1f` plus
  `\x7f` from untrusted text; caps length.
- `_DEFAULT_MAX_JSON_BYTES = 256 KB`. Modules with larger payloads pass
  explicit `max_bytes=`.

#### `modules/_netsafe.py`

SSRF-safe HTTP fetch with DNS-TOCTOU pinning. Resolves the host, rejects
non-public IPs, then pins `socket.getaddrinfo` for the calling thread so
urllib3 cannot re-resolve to a different (internal) address. Re-validates on
every redirect hop.

- `ip_is_blocked(ip)` - rejects private/loopback/link-local/multicast/
  reserved/unspecified/ULA, unwraps IPv4-mapped IPv6.
- `resolve_safe_ip(host)` - resolves host, returns one safe IP literal or
  `None` if ANY answer is unsafe.
- `url_is_safe(url)` - pre-flight check: scheme (http/https) + host
  validation.
- `safe_open(method, url, ua, ...)` - context manager yielding a streaming
  `Response`. Per-hop SSRF validation + DNS pinning.
- `_pinning_getaddrinfo` - monkey-patches `socket.getaddrinfo` globally
  (no-op unless thread-local pin is set).
- `METADATA_HOSTS` frozenset blocks cloud metadata endpoints
  (`169.254.169.254`, `fd00:ec2::254`, `metadata.google.internal`).
- `DEFAULT_MAX_REDIRECTS = 5`.
- `%`-scoped IPv6 zone IDs stripped before address parsing.

#### `modules/geocode.py`

Location string resolver supporting coordinates, postal codes,
city+state/country, and free-text place names. Uses Nominatim (structured
and free-text), Zippopotam.us (Canadian/numeric postal), and reverse
geocoding. TTL + LRU cache per Nominatim ToS.

- `geocode(query, user_agent, default_country)` - main entry point
  returning `(lat, lon, display_name, country_code)` or `None`.
- `_parse_coords(query)` - parses decimal, hemisphere, and DMS coordinates.
- `_postal_kind(s)` - classifies input as us/ca/uk/ie/jp/br/num postal
  code or None.
- `us_state_code(query)` - returns USPS code if query is a bare state name.
- Cache: `OrderedDict` with `threading.Lock`, 24h TTL, 1000-entry LRU cap,
  negative caching.
- Refuses to call Nominatim without a contact identifier in the UA.
- Word-drop fallback loop: drops trailing tokens up to `_MAX_DROPS = 4`.
- Postal codes do NOT fall back to fuzzy free-text.
- Settlement vs free-text two-pass with `importance` score comparison.
- Canadian postal codes via Zippopotam (Nominatim lacks Canada Post data).
- APIs: `nominatim.openstreetmap.org/search`, `.../reverse`,
  `api.zippopotam.us/{cc}/{code}`.

#### `modules/units.py`

Pure unit conversion and formatting helpers: `cf` (C -> "C / F" string),
`kph`, `km_mi`, `mb`, `deg_to_card`, `fmt_dt`, `fmt_short`, `aqi_fmt`,
`wave_fmt`, `swell_fmt`. All return "N/A" on `None` input. Mile conversion
factor: 1 mile = 1.609344 km (exact).

#### `modules/example.py`

Copy-and-fill skeleton for new modules. The default `.example` echoes its
argument uppercased. Demonstrates the contract: `COMMANDS` dict maps to
async method names; `setup(bot)` returns a `BotModule` instance.

#### `modules/location.py`

User location registration. `.regloc` saves a default location for weather
commands. `.myloc` shows it. `.delloc` removes it. All commands operate on
the invoker's own nick only. Location stored via `bot.loc_set` /
`bot.loc_get` / `bot.loc_del`. Opted-out users can still manage their own
location. Uses `geocode()` for validation.

#### `modules/privacy.py`

GDPR-style privacy commands: `.forgetme` (right-to-erasure), `.privacy`
(data disclosure), `.optout` / `.optin`. All PM-only (rejected in channels
with a NOTICE directing to PM).

- `.forgetme` calls `forget(nick)` on every loaded module. Also calls
  `store.user_purge` for channel-tracking entries (NOT `user_quit`, which
  records a quit). One module raising must not abort the rest.
- `.privacy` discloses: saved location, own hostmask, per-channel tracking
  entries, opt-out status.
- Legacy `__optout__:` key migration.

### Command modules

Each module below follows the same contract: a `setup(bot)` function
returning a `BotModule` subclass. Rate limiting via
`self.bot.rate_limited(nick)` unless noted otherwise.

#### `modules/weather.py`

Multi-provider weather hub. 15+ user-facing commands covering current
conditions, forecasts, alerts, AQI, astronomy, marine, tides, UV, pollen,
wildfire, space weather, and historical data.

**Commands:** `.weather`/`.w`, `.forecast`/`.f`, `.hourly`/`.h`,
`.alerts`/`.al`, `.aqi`/`.air`, `.astro`/`.sun`, `.history`/`.hist`,
`.marine`/`.sea`, `.nowcast`/`.nc`, `.uv`/`.uvi`, `.pollen`/`.allergy`,
`.wildfire`/`.fire`, `.space`/`.aurora`, `.tides`/`.tide`,
`.providers` (admin-only).

- `_parse_weather_flags(arg)` - extracts `-l`, `-p <name>`, `-<alias>`,
  `-n <nick>` from anywhere in the argument.
- `_PROVIDER_FLAGS` - ~60-entry alias table mapping short flags to canonical
  provider IDs.
- Formatters: `_format_current`, `_format_forecast`, `_format_hourly`,
  `_format_alerts`, `_format_aqi`, `_format_astronomy`,
  `_format_historical`, `_format_marine`, `_format_uv`, `_format_pollen`,
  `_format_wildfire`, `_format_space`, `_format_tides`.
- `-n <nick>` for cross-user location lookup respects opt-out flag.
- Alerts deduplicated by (event, headline), sorted by severity (extreme
  first), capped at 5.
- State-wide alert queries widened via `us_state_code()`.
- `.providers` is admin-only.

#### `modules/advice.py`

Random advice slip. `.advice`. API: `api.adviceslip.com/advice`. No key.

#### `modules/apod.py`

NASA Astronomy Picture of the Day. `.apod`. Falls back to `DEMO_KEY` if no
key configured. API: `api.nasa.gov/planetary/apod`. Key: `nasa_api_key`.

#### `modules/astro2.py`

Five space/astronomy commands, all keyless. `.solar` (NOAA SWPC solar flare
class), `.neo` (NASA NeoWs near-earth objects), `.launches` (Launch Library
2, capped at 1-3 results, 512 KB body cap), `.moon` (pure compute
Fliegel-Van Flandern phase), `.sky` (Messier catalog M1-M110 embedded data,
accepts M-number or common name).

#### `modules/bofh.py`

Random BOFH excuse from ~100 hardcoded excuses. `.bofh`/`.excuse`. Pure
local. No rate limiting (no I/O cost).

#### `modules/bored.py`

Random activity suggestion. `.bored`. API:
`bored-api.appbrewery.com/random`. No key.

#### `modules/calc.py`

Safe expression evaluator using `ast`. `.cc`. Whitelisted operators and math
functions only. Implicit multiplication (`2pi` -> `2*pi`). Max AST depth 50,
exponent cap 10000, result bit-length 100000, factorial cap 170.
Digit-containing function names (e.g., `log2`) protected via sentinel
substitution before the implicit-mul regex.

#### `modules/catfact.py`

Random cat fact. `.catfact`/`.cat`. API: `catfact.ninja/fact`. No key.

#### `modules/channels.py`

Join/part management with async channel-founder verification. `.join` (non-
admins must be the NickServ-identified founder), `.part`, `.users`
(admin-only, capped at 20 entries). Verification is async: WHOIS for
account, ChanServ INFO for founder, then compare. 15-second timeout.
`threading.Lock` protects `_pending` dict.

#### `modules/chuck.py`

Random Chuck Norris joke. `.chuck`. API: `api.chucknorris.io/jokes/random`.
No key.

#### `modules/cocktail.py`

Cocktail recipe lookup. `.cocktail`/`.drink`. Iterates `strIngredient1`
through `strIngredient15` (flat schema). API:
`thecocktaildb.com/api/json/v1/1/search.php`. No key.

#### `modules/cowsay.py`

Pure-Python ASCII cow speech bubble. `.cowsay`. Input capped at 200 chars,
word-wrapped at 40 columns. Each line sent as separate `privmsg`. No
external binary.

#### `modules/crypto.py`

Crypto spot price from CoinGecko (keyless). `.gecko`/`.coingecko`/`.cg`.
Two-call flow: search to resolve symbol/name to coin ID, then price
endpoint. In-memory query-to-ID cache (512 entries, FIFO). Symbol-exact
match preferred. IRC color codes for green/red 24h change. Separate from
`stocks.py`'s `.crypto`.

#### `modules/dadjoke.py`

Random dad joke. `.dadjoke`/`.joke`. Sends `Accept: application/json`
header. API: `icanhazdadjoke.com`. No key.

#### `modules/devtools.py`

Seven developer utility commands, all pure stdlib. `.jwt` (decoder, no
signature check, warns on `alg=none`), `.semver` (comparator), `.uuid5`
(generator/inspector), `.tz` (timezone converter with common abbreviation
map - genuinely ambiguous IST/BST deliberately omitted), `.unix` (signal and
errno lookup), `.color` (CSS color converter with nearest-name via
Euclidean RGB), `.cron` (expression validator + next-fire-time via minute
scan up to 366 days on `to_thread`, field ranges bounded against memory
amplification).

#### `modules/devutils.py`

Six offline text codec/utility commands. `.b64`/`.unb64` (base64),
`.hex` (auto-detect direction: all-hex even-length decodes, otherwise
encodes), `.morse` (auto-detect: `.-/ ` only decodes, otherwise encodes),
`.uuid` (random UUIDv4), `.epoch` (epoch-to-ISO / ISO-to-epoch).

#### `modules/dice.py`

Dice roller. `.d`. `XdN+M` notation. 1-100 dice, 2-10000 sides. Shows
individual rolls for up to 20 dice. `random.SystemRandom()`. No rate
limiting (no I/O).

#### `modules/dictionary.py`

English dictionary. `.dict`/`.dictionary`. Supports `/N` pagination across
all definitions. API: `api.dictionaryapi.dev/api/v2/entries/en/{word}`.
No key.

#### `modules/dnd.py`

D&D 5e SRD lookup. `.dnd`. Tries spells endpoint first, falls back to
monsters. Slugs are kebab-case lower, capped at 64 chars. Monster AC field
can be list or value. API: `dnd5eapi.co/api/2014/spells/{slug}`,
`.../monsters/{slug}`. No key.

#### `modules/dnsutils.py`

DNS and RDAP utilities over HTTPS. `.dns` (Cloudflare DoH), `.rdns`
(reverse PTR), `.caa` (CAA + SPF/DMARC), `.whois` (RDAP domain
registration), `.asn` (RDAP IP/ASN, accepts bare IPs and `AS<number>`).
Hostname validated to `[A-Za-z0-9._-]{1,253}`. RDAP body cap 512 KB.
APIs: `cloudflare-dns.com/dns-query`, `rdap.org/domain|ip|autnum/...`.
All keyless.

#### `modules/encode.py`

Twelve offline encoding/text/generator utilities. `.unicode` (codepoint
inspector with block table), `.hash` (md5/sha1/sha256/sha512/blake2b),
`.crc` (CRC32+Adler-32), `.b32` (base32), `.slug` (slugify), `.ulid`
(Crockford base32, millisecond timestamp + 80 bits `secrets.randbits`),
`.ascii` (table lookup), `.ds` (data-size converter), `.defang`/`.refang`
(auto-detect direction), `.entropy` (password entropy estimator with crack
time at 10B/s), `.pw` (random password/passphrase via `secrets`), `.lorem`
(ipsum generator).

#### `modules/fact.py`

Random useless fact. `.fact`. API: `uselessfacts.jsph.pl/api/v2/facts/random`.
No key.

#### `modules/fml.py`

Random FMyLife quote. `.fml`. HTML scrape of `fmylife.com/random`. Regex
extraction anchored on `block text-blue-500`, filtered to "Today..." posts.
512 KB body cap.

#### `modules/fx.py`

Foreign exchange conversion. `.fx`. Optional amount, currency codes validated
to exactly 3 letters, amount capped at 1e12. Sub-unit results to 4
significant digits. API: `api.frankfurter.dev/v1/latest`. No key (ECB
rates).

#### `modules/games.py`

Four pure-local randomness commands. `.coin` (flip), `.8ball`, `.rps`
(rock-paper-scissors), `.choose` (comma-separated list, 2-20 options).
`random.SystemRandom()`.

#### `modules/ghinfo.py`

Public GitHub repo info. `.gh`. Stars, forks, issues, language, license,
last push. Unauthenticated: hard 60 req/hr per source IP. Input must
contain exactly one `/`. API: `api.github.com/repos/{owner}/{repo}`.

#### `modules/health.py`

Admin-only bot health inspector. `.health` shows loaded modules, provider
state, sender queue, store dirty flags, geocode cache, admin count, audit
integrity, counters. `.uptime` (public). Lazily imports optional subsystems.

#### `modules/hn.py`

Hacker News top story by rank (1-30). `.hn`. Two-step via Firebase: top
story IDs then item detail. API: `hacker-news.firebaseio.com/v0/`. No key.

#### `modules/httpcode.py`

HTTP status code lookup from hardcoded dictionary. `.http`. Standard +
WebDAV + RFC 7540 + 418. No network.

#### `modules/idlerpg.py`

IdleRPG player lookup from XML API. `.irpg`/`.idlerpg`. Parsed with
`defusedxml`. API URL configurable via `[idlerpg] api_url`. Default:
`http://idlerpg.rizon.net/xml.php` (plain HTTP - known debt).

#### `modules/imdb.py`

Movie/TV lookup via OMDb. `.imdb`. Hidden when unconfigured. API:
`omdbapi.com`. Key: `omdb_key`.

#### `modules/ipinfo.py`

IP/hostname geolocation. `.ipinfo`. Location, timezone, ISP, Google Maps
link. Uses plain HTTP (free tier does not support HTTPS - MITM risk
acknowledged). Input regex-validated twice. All upstream fields individually
`strip_ctrl`'d. API: `ip-api.com/json/{target}`. No key.

#### `modules/ipintel.py`

Multi-source IP reputation aggregator. `.ip`/`.rep`. DNSBLs (6 zones via
Cloudflare DoH), SANS ISC/DShield, GreyNoise Community, Tor exit list
(cached 1h, 4 MB body cap), and optionally AbuseIPDB.

- SSRF-guarded: private/loopback/link-local refused.
- All sources concurrent via `asyncio.gather(..., return_exceptions=True)`.
- Verdict: malicious if 2+ DNSBL hits OR Tor exit OR GreyNoise "malicious"
  OR AbuseIPDB >= 50; suspicious if 1 DNSBL hit OR AbuseIPDB 25-49 OR
  DShield count >= 10.
- Spamhaus ZEN excluded (refuses public-resolver queries).
- APIs: Cloudflare DoH, SANS ISC, GreyNoise, AbuseIPDB (keyed, optional),
  Tor Project.

#### `modules/iss.py`

ISS current position and crew list. `.iss`. Crew filtered to `craft ==
"ISS"`. API: `api.open-notify.org`. No key.

#### `modules/lastfm.py`

Last.fm user profile. `.lastfm`. Play count, registration, now-playing.
Hidden when unconfigured. Handles `tracks` being dict or list. API:
`ws.audioscrobbler.com/2.0/`. Key: `lastfm_key`.

#### `modules/linktitle.py`

Passive URL title announcer. Watches channel messages via `on_raw()` for
HTTP/HTTPS links. Auto-announces `<title>` or `og:title`. YouTube links
enriched via Data API (if keyed) or oembed fallback. No COMMANDS registered.

- SSRF protection via `_netsafe.safe_open()`.
- Per-channel cooldown: 3s. Per-URL dedup: 5 min TTL. Max 3 URLs per message.
- Skips: localhost, binary/media extensions, CTCP, bot's own messages,
  command-prefixed messages.
- HTML body capped at 768 KB. Dedup cache evicts at 500 entries.

#### `modules/mathx.py`

Offline math toolbox. `.isprime` (deterministic Miller-Rabin, max 100
digits), `.factor` (max 19 digits, trial division + Pollard rho), `.gcd`
(GCD/LCM), `.base` (base conversion), `.stats` (descriptive, max 1000
numbers), `.roman` (Roman numerals), `.pct` (percentages), `.bignum`
(factorial up to 100K, fibonacci up to 500K, power - temporarily raises
`sys.get_int_max_str_digits` to 2M on `to_thread`), `.const` (physical
constant lookup).

#### `modules/mtg.py`

Magic: The Gathering card lookup. `.mtg`. API:
`api.scryfall.com/cards/named?fuzzy=`. No key.

#### `modules/netcalc.py`

Offline network calculators. `.cidr` (CIDR info), `.subnet` (splitting),
`.port` (port/service name lookup - hardcoded dict first, `getservbyport`
fallback). IPv6 supported.

#### `modules/notes.py`

Per-nick persistent sticky notes in JSON. `.notes` (subcommands: list, add,
del, show, clear). 20 notes per nick, 200 chars per note. `.clear` requires
two-step confirmation within 60s. Atomic save (mkstemp + os.replace,
0o600). **Persists per-nick data** (`notes.json`). **Supports `forget()`.**

#### `modules/numberfact.py`

Number trivia: hybrid local math facts + Wikipedia REST API. `.numberfact`/
`.nf`. `_MAX_ABS_N = 1e12` (prevents DoS via O(sqrt(n)) factorization).
Response body cap 4 MB. `math_fact()` picks most distinctive property.
Wikipedia boilerplate triggers fallback. API: Wikipedia REST.

#### `modules/physcalc.py`

Offline physics/engineering calculator. `.ly` (light travel time), `.sr`
(special relativity), `.escape` (escape velocity), `.ohm` (Ohm's law),
`.rc` (resistor color codes), `.baud` (serial transfer time).

#### `modules/pkginfo.py`

Keyless package registry lookups. `.pypi`, `.npm`, `.crates` (version,
description, license, release date). Name validated with regex + `..`
traversal block. npm body cap 1 MB; crates 2 MB. APIs: `pypi.org`,
`registry.npmjs.org`, `crates.io`. All keyless.

#### `modules/poke.py`

Pokemon lookup. `.poke`/`.pokemon`. Types, base stats, BST, height/weight,
primary ability. Body cap 1 MB. API: `pokeapi.co/api/v2/pokemon/{name}`.
No key.

#### `modules/probe.py`

Network diagnostic probers, all SSRF-guarded. `.headers` (HTTP + security
headers HSTS/CSP/XFO/XCTO, no redirect follow), `.ssl` (TLS cert details,
TLSv1.2 minimum), `.tcp` (connect with latency), `.down` (reachability
check, no redirect follow).

#### `modules/qdb.py`

Quote database. `.qdb`. Scrapes `bash-org-archive.com`. Random or by ID.
Long quotes (>5 lines) emit a "view at" link. Base URL configurable.

#### `modules/qr.py`

QR code URL constructor. `.qr`. Constructs a goqr.me/qrserver URL locally.
No HTTP fetch - user clicks the link. Input capped at 1000 chars.

#### `modules/recipe.py`

Recipe lookup. `.recipe`/`.meal`. First 12 ingredients with "+ N more". API:
`themealdb.com/api/json/v1/1/search.php`. No key.

#### `modules/reddit.py`

Top subreddit post. `.reddit`/`.r`. Subreddit validated to
`[A-Za-z0-9_]{1,21}`. Uses `old.reddit.com`. `allow_redirects=False` (3xx =
private/banned). 403 = quarantined. Uses `weather_user_agent` (Reddit
aggressively 403s default UAs). API: `old.reddit.com/r/{sub}/top.json`.

#### `modules/reflookup.py`

Multi-source reference lookup. `.wiki` (Wikipedia summary, two-pass: exact
title then opensearch), `.doi` (Crossref), `.isbn` (Open Library), `.so`
(Stack Overflow), `.rfc` (RFC metadata via IETF datatracker), `.rtfm`
(tldr-pages across platforms: common/linux/osx/freebsd/openbsd/netbsd),
`.arxiv` (arXiv papers via `defusedxml`), `.element` (offline periodic table,
all 118). All APIs keyless.

#### `modules/remind.py`

Per-user reminder scheduler. `.remind` (set), `.remind-list`, `.remind-cancel`.
Min 30s, max 30 days, max 10 per nick, 200 chars. Supports relative durations
(`30s`, `1h30m`, `2d4h`), keywords (`tomorrow`, `tonight`), HH:MM (next
occurrence), ISO 8601 absolute. All times UTC. "tonight" = 20:00 UTC.
Late delivery detected and reported. Atomic save. **Persists per-nick data**
(`reminders.json`). **Supports `forget()`.**

#### `modules/satpass.py`

Satellite visible-pass predictor. `.passes`. Accepts NORAD IDs or common
names (iss, hubble, tiangong, etc.). Hidden when unconfigured. API:
`api.n2yo.com`. Key: `n2yo_api_key`.

#### `modules/scinews.py`

STEM news aggregator with ~140 curated RSS/Atom feeds across 13 topics.
`.sci [topic]`, `.sci read <N>`, `.sci sources`.

- Feed concurrency capped at 8 via `asyncio.Semaphore`. Aggregate cache
  TTL 120s.
- Diversity: at most 3 items per source in top 10.
- Article reader uses `_netsafe.safe_open()` (SSRF-guarded) because article
  links are attacker-influenceable.
- Feed fetches do NOT use SSRF protection (operator-curated constant URLs).
- Feed body cap 12 MB (podcast/arXiv feeds reach 8-10 MB).
- Parsed with `defusedxml`.

#### `modules/search.py`

Web and image search. `.sw`/`.g` (web), `.si`/`.gi` (image). DuckDuckGo
HTML Lite (keyless, scrape) with optional Brave Search API upgrade. DDG
result extraction via regex on HTML (fragile to markup changes).
`html.unescape` can turn `&#1;` back into raw C0 bytes, so `strip_ctrl`
runs last. Image search requires Brave key. APIs: `html.duckduckgo.com`
(POST), `api.search.brave.com` (key: `brave_key`, optional).

#### `modules/secinfo.py`

Security utilities. `.cve` (NVD lookup, CVE ID regex-validated), `.pwn`
(HIBP password breach check, PM-ONLY, k-anonymity - only first 5 SHA-1
hex chars leave the host), `.hashid` (offline hash type identification),
`.cvss` (CVSS v3.1 base score with spec-exact roundup), `.cipher`
(reference). APIs: NVD, HIBP. All keyless.

#### `modules/seen.py`

Passive nick tracker. Hooks PRIVMSG/JOIN/PART/QUIT/NICK via `on_raw()`.
`.seen`. Only records channel PRIVMSGs (not PMs). Respects opt-out at both
record and query time. Dirty flag + 60s periodic flush. NICK changes
recorded for both old and new nicks. Configurable max age (default 180
days; 0 disables pruning). `on_raw` wrapped in blanket try/except (must
never throw in IRC read path). **Persists per-nick data** (`seen.json`).
**Supports `forget()`.**

#### `modules/spacex.py`

Next upcoming SpaceX launch. `.spacex`. 180s in-process TTL cache. API:
`ll.thespacedevs.com/2.2.0/launch/upcoming/`. Body cap 512 KB. No key.

#### `modules/steam.py`

Steam user profile. `.steam` (status, `-g` for games list), `.regsteam`
(register nick-to-SteamID). `-n <nick>` for cross-nick lookup.
Owned-games cap 1 MB. API: `api.steampowered.com` (`ISteamUser`,
`IPlayerService`). Key: `steam_key`. **Persists per-nick data**
(`steamids.json`). **Supports `forget()`.**

#### `modules/stocks.py`

Multi-provider stock and crypto price. `.stock`/`.s`, `.crypto`. Ordered
failover: Finnhub -> Alpha Vantage -> Twelve Data. First success wins.
Requires at least one key. Finnhub crypto uses `BINANCE:<SYM>USDT`. Keys:
`finnhub_key`, `alphavantage_key`, `twelvedata_key`.

#### `modules/tell.py`

Offline message system. `.tell`, `.tell-cancel`, `.tell-list`. 10 tells per
recipient, 5 per sender globally. Message max 350 chars. TTL 30 days.
`on_raw` hook with cheap pre-check. Self-tells and tells to bot rejected.
Messages `strip_ctrl`'d at capture. **Persists per-nick data** (`tells.json`).
**Supports `forget()` (removes tells both TO and FROM).**

#### `modules/translate.py`

Language translation via Google's unofficial `gtx` endpoint. `.t`/
`.translate`. Language codes validated to strict 2-letter lowercase in both
handler AND worker. Detected source language re-validated before output.
Query cap 500 chars. Body cap 256 KB. API:
`translate.googleapis.com/translate_a/single`. No key.

#### `modules/twitch.py`

Twitch stream/channel/game search. `.tw`/`.twitch`. Subcommands: `-s`
(stream search, default), `-c` (channel info), `-g` (game search). No
argument = top 5 live streams. Auto OAuth client-credentials token
management with `threading.Lock` serialization and 60s pre-expiry renewal.
Keys: `twitch_client_id`, `twitch_client_secret`. APIs:
`id.twitch.tv/oauth2/token`, `api.twitch.tv/helix/*`.

#### `modules/urbandictionary.py`

Urban Dictionary. `.u`/`.urbandictionary`. `/N` pagination. Index clamped.
Definition flattened and truncated to 400 chars. API:
`api.urbandictionary.com/v0/define`. No key.

#### `modules/urls.py`

URL shortening and expansion. `.shorten` (is.gd, validates URL via
`url_is_safe()` first), `.expand`/`.unshorten` (uses
`safe_open("HEAD", ...)` with per-hop DNS re-validation and pinning). API:
`is.gd/create.php`. No key.

#### `modules/xkcd.py`

xkcd comic lookup. `.xkcd`. By number or random. Comic #404 special-cased
(xkcd skipped it; random bumps 404 to 405). Number max 100000. API:
`xkcd.com/{n}/info.0.json`. No key.

#### `modules/youtube.py`

YouTube video search. `.yt`/`.youtube`. Two API calls per search: search
then video details. Search costs 100 quota units per call. Hidden when
unconfigured. Key: `youtube_key`. API: `googleapis.com/youtube/v3/*`.

### Cross-cutting module facts

**Modules that persist per-nick data** (relevant for `.forgetme`/privacy):
`notes`, `remind`, `seen`, `steam`, `tell`. All use atomic JSON writes
(mkstemp + os.replace, 0o600) and implement `forget(nick)`. Additionally,
`location` stores location via the bot's own `loc_set`/`loc_del`.

**Modules requiring API keys** (hidden from `.help` when unconfigured):
`imdb` (omdb_key), `lastfm` (lastfm_key), `satpass` (n2yo_api_key),
`stocks` (finnhub/alphavantage/twelvedata), `twitch` (client_id +
client_secret), `youtube` (youtube_key). Optional keys that enhance but
aren't required: `apod`/`astro2` (nasa_api_key, defaults to DEMO_KEY),
`ipintel` (abuseipdb_key), `search` (brave_key), `steam` (steam_key).

**Rate limiting:** Nearly universal `self.bot.rate_limited(nick)`. Exceptions:
`bofh` and `dice` (no I/O cost), `channels` (admin/founder-gated), `health`
(admin-gated), `privacy` (always succeeds), `linktitle` (own cooldown/dedup).
Secondary caches: `geocode` (24h LRU), `spacex` (180s TTL), `crypto`
(512-entry FIFO), `scinews` (120s aggregate), `seen` (60s dirty-flush),
`ipintel` Tor list (1h TTL).

---

## File reference: weather providers

### Infrastructure

#### `weather_providers/__init__.py`

Provider registry and public dispatch surface. ~712 lines. Registers all
32 provider factories with their capabilities. Contains `configure()` which
reads `config.ini` and builds the provider chain, and 14 public `get_*`
functions (`get_weather`, `get_forecast`, `get_hourly`, `get_alerts`,
`get_air_quality`, `get_astronomy`, `get_historical`, `get_marine`,
`get_uv`, `get_pollen`, `get_wildfire`, `get_space_weather`, `get_tides`,
`get_nowcast`) that delegate to the `Dispatcher`. Per-provider quota tracking
and credential resolution via `_cred()` (secret_store first, config.ini
fallback).

#### `weather_providers/base.py`

Frozen normalized dataclasses and shared helpers. Defines the
`WeatherProvider` protocol and all result types: `WeatherResult`,
`ForecastDay`, `HourlyResult`, `HourlyEntry`, `AlertsResult`, `AlertEntry`,
`AirQualityResult`, `AstronomyResult`, `HistoricalResult`, `MarineResult`,
`UVResult`, `PollenResult`, `WildfireResult`, `SpaceWeatherResult`,
`TideResult`. Shared helpers: `deg_to_card`, `ms_to_kph`, `km_to_m`,
`haversine_km`, derived-field formulas for dewpoint and heat index.
`_CURRENT_GAP_FIELDS` lists which current-condition fields can be
gap-filled across providers - `feels_like_c` and `dewpoint_c` are
deliberately excluded (derived from the same observation's temperature).

#### `weather_providers/_dispatch.py`

Capability-based dispatcher with health-scored fallback. Defines
`CAPABILITY_METHODS` (mapping capability names to method names),
`DEFAULT_RELIABILITY` rankings (NWS is rank #1 for US current/forecast/
hourly/alerts), and the `Dispatcher` class that routes requests to the best
healthy provider and falls back through the chain on failure. 45s chain
budget.

#### `weather_providers/_health.py`

Per-provider health tracking with exponential moving average scoring. Tracks
success rate, latency, rate limiting, and a composite health score.
Circuit-breaker pattern: providers that consistently fail are temporarily
removed from the dispatch chain.

#### `weather_providers/_http.py`

Shared async HTTP client (~369 lines). Prefers aiohttp, falls back to
`requests` + `asyncio.to_thread`. Response size capping (1 MB default).
`HTTPError` and `ResponseTooLargeError` types. Cached aiohttp session
management.

### Provider packages

Each provider package has an `__init__.py` that defines the provider class
and lists its capabilities. Endpoint files (current.py, forecast.py,
hourly.py, alerts.py, etc.) each contain a `fetch()` coroutine.

| Provider | Capabilities | Coverage | Key required | API base |
|---|---|---|---|---|
| **nws** | current, forecast, hourly, alerts, marine | US only | No | `api.weather.gov` |
| **openmeteo** | current, forecast, hourly, air_quality, astronomy, historical, marine, nowcast, pollen, uv | Global | No | `api.open-meteo.com` |
| **weatherapi** | current, forecast, hourly, alerts, air_quality, astronomy, historical | Global | Yes (`weatherapi_key`) | `api.weatherapi.com` |
| **openweathermap** | current, forecast, hourly, alerts, air_quality | Global | Yes (`openweathermap_key`) | `api.openweathermap.org` |
| **visualcrossing** | current, forecast, hourly, alerts, historical | Global | Yes (`visualcrossing_key`) | `weather.visualcrossing.com` |
| **weatherbit** | current, forecast, hourly, alerts, air_quality, historical | Global | Yes (`weatherbit_key`) | `api.weatherbit.io` |
| **tomorrowio** | current, forecast, hourly, alerts, air_quality | Global | Yes (`tomorrowio_key`) | `api.tomorrow.io` |
| **pirateweather** | current, forecast, hourly, alerts, nowcast | Global | Yes (`pirateweather_key`) | `api.pirateweather.net` |
| **weatherkit** | current, forecast, hourly, alerts | Global | Yes (`weatherkit_*` - Apple JWT) | `weatherkit.apple.com` |
| **accuweather** | current, forecast, hourly, alerts | Global | Yes (`accuweather_key`) | `dataservice.accuweather.com` |
| **meteomatics** | current, forecast, hourly | Global | Yes (`meteomatics_user`, `_pass`) | `api.meteomatics.com` |
| **stormglass** | current, hourly, marine | Global | Yes (`stormglass_key`) | `api.stormglass.io` |
| **worldweatheronline** | current, forecast, hourly, astronomy, historical, marine | Global | Yes (`worldweatheronline_key`) | `api.worldweatheronline.com` |
| **weatherstack** | current, forecast, historical | Global | Yes (`weatherstack_key`) | `api.weatherstack.com` |
| **metno** | current, forecast, hourly, alerts, nowcast | Nordic/Europe | No | `api.met.no` |
| **eccc** | alerts | Canada only | No | `dd.weather.gc.ca` |
| **gdacs** | alerts | Global | No | `www.gdacs.org` |
| **airnow** | air_quality | US only | Yes (`airnow_key`) | `aqs.epa.gov` |
| **iqair** | air_quality | Global | Yes (`iqair_key`) | `api.airvisual.com` |
| **openaq** | air_quality | Global | Yes (`openaq_key`) | `api.openaq.org` |
| **purpleair** | air_quality | Global (sensor network) | Yes (`purpleair_key`) | `api.purpleair.com` |
| **waqi** | air_quality | Global | Yes (`waqi_key`) | `api.waqi.info` |
| **currentuvindex** | uv | Global | No | `currentuvindex.com` |
| **google_pollen** | pollen | Global | Yes (`google_pollen_key`) | `pollen.googleapis.com` |
| **pollendotcom** | pollen | US only | No | `www.pollen.com` |
| **firms** | wildfire | Global | Yes (`firms_key`) | `firms.modaps.eosdis.nasa.gov` |
| **nifc** | wildfire | US only | No | `services3.arcgis.com` |
| **swpc** | space_weather | Global | No | `services.swpc.noaa.gov` |
| **sunrisesunset** | astronomy | Global | No | `api.sunrise-sunset.org` |
| **noaa_coops** | tides | US coastal | No | `api.tidesandcurrents.noaa.gov` |
| **tidecheck** | tides | Global | Yes (`tidecheck_key`) | `www.worldtides.info` |
| **nasapower** | historical | Global | No | `power.larc.nasa.gov` |

#### NWS provider detail (`weather_providers/nws/`)

- `__init__.py` - `NWSProvider` class. Five async methods (`get_weather`,
  `get_forecast`, `get_hourly`, `get_alerts`, `get_marine`), each wrapped by
  `none_if_uncovered()` so non-US points return `None`.
  `requires_key = False`.
- `_scope.py` - US-coverage handling. `OutOfCoverage` exception. `nws_json()`
  wrapper maps NWS 400/404 to `OutOfCoverage` (coverage gap, not failure).
  `none_if_uncovered()` wraps coroutines.
- `_codes.py` - Re-exports `deg_to_card`, `ms_to_kph`. Defines
  `map_severity()` (extreme/severe/moderate/minor -> canonical).
- `current.py` - Nearest observation station via points API. Extracts
  temperature, feels-like (heat index or wind chill from same observation),
  humidity, wind, pressure, visibility, dewpoint. NWS reports in SI (Celsius,
  m/s, Pa).
- `forecast.py` - Daily forecast. Pairs daytime/nighttime periods for
  high/low. `_f_to_c()` converts without premature rounding (the display
  layer handles rounding).
- `hourly.py` - Hourly forecast. Up to 12 entries. `_parse_wind()` converts
  NWS "15 mph" strings to kph.
- `alerts.py` - Active alerts via `api.weather.gov/alerts/active`. Supports
  point query or state-wide (USPS state code). CAP/IPAWS severity mapping.
- `marine.py` - Marine forecast. Checks for marine zone coverage. Returns
  minimal `MarineResult`.

#### Open-Meteo provider detail (`weather_providers/openmeteo/`)

Broadest capability set (10 capabilities), all keyless. Global coverage.

- `_codes.py` - WMO weather code to description mapping.
- `current.py` - Current conditions from Open-Meteo's analysis model.
- `forecast.py` - 7-day daily forecast.
- `hourly.py` - Hourly forecast up to 24 hours.
- `air_quality.py` - AQI, PM2.5, PM10, ozone from CAMS model.
- `astronomy.py` - Sunrise, sunset, day length from ephemeris model.
- `historical.py` - Historical weather for a past date.
- `marine.py` - Wave height, swell, water temperature from marine model.
- `nowcast.py` - 15-minute precipitation nowcast.
- `pollen.py` - Grass/tree/weed pollen from CAMS. Europe-only in practice.
- `uv.py` - UV index forecast.

#### WeatherKit provider detail (`weather_providers/weatherkit/`)

Apple Weather. Requires Apple Developer Program membership. Authentication
via ES256 JWT signed with a `.p8` private key.

- `__init__.py` - JWT generation with configurable `team_id`, `service_id`,
  `key_id`, `key_path`. Token cached with 50-minute refresh (Apple tokens
  valid 60 min).
- `_codes.py` - Apple weather code to description mapping.
- `current.py`, `forecast.py`, `hourly.py`, `alerts.py` - Standard
  endpoints.
- Keys: `weatherkit_team_id`, `weatherkit_service_id`, `weatherkit_key_id`,
  `weatherkit_key_path`.
