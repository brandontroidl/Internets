# Operations

Routine procedures for whoever runs the bot day to day: starting and stopping it,
choosing between restart / rehash / reload, reading and rotating logs, reviewing the
audit trail, backing up state, checking health, running the metrics endpoint, and
upgrading.

This document is the operator's runbook. It states what to do and what to expect.
Line-level mechanism lives in the internals set and is linked at each point rather
than repeated here:

| Subsystem | Internals reference |
|---|---|
| Process entry, connection, dispatch | [internals/internets.md](internals/internets.md) |
| Single-instance lock | [internals/process_lock.md](internals/process_lock.md) |
| Admin command surface | [internals/admin_cmds.md](internals/admin_cmds.md) |
| Audit trail | [internals/audit_log.md](internals/audit_log.md) |
| State files, rate limiting | [internals/store.md](internals/store.md) |
| Logging stack | [internals/botlog.md](internals/botlog.md) |
| Metrics registry and exporter | [internals/metrics.md](internals/metrics.md) |
| Config layering and CLI | [internals/config.md](internals/config.md) |
| Secret resolution | [internals/secret_store.md](internals/secret_store.md) |

Related guides: [configuration.md](configuration.md) for what every key means,
[administration.md](administration.md) for the admin command surface and its blast
radius, [troubleshooting.md](troubleshooting.md) for symptom-driven diagnosis.

(ops-starting)=
## Starting the bot

Two entry points, both landing in `internets.py - _entry()`:

```bash
python internets.py     # from a checkout
internets               # console_script, pyproject: internets = "internets:_entry"
```

Run from the deployment directory. Every path the bot uses - `config.ini`,
`config.local.ini`, `internets.pid`, the JSON state files, `audit.log`, the log
file - resolves against the process working directory at import or at acquire time
(`config.py` `CONFIG_PATH`, `secret_store.SECRETS_FILE`,
`process_lock.ProcessLock.acquire()`). Starting from a different directory silently
addresses a different config and a different state set.

`_entry()` does three things in order:

1. **Drop-root guard** (POSIX only). If `os.geteuid() == 0` and
   `INTERNETS_ALLOW_ROOT` is not `1`, it logs `event=refused_root_start` and exits 1.
   Set `INTERNETS_ALLOW_ROOT=1` only when you have a concrete reason (binding a
   privileged port without `setcap`); the override logs `event=root_start_allowed`.
   Windows has no euid and skips the check.
2. **Acquire the process lock** at `./internets.pid`, as a context manager around
   `asyncio.run(_main(lock))`. A held lock logs
   `Another bot instance is already running: <reason>` and exits 1. See
   [Process lock](#ops-process-lock).
3. **Run the loop.** `KeyboardInterrupt` after the loop logs
   `event=keyboard_interrupt` and exits 130.

Two import-time stages run before any of that and can refuse to boot on their own:
`config.py` (reads `config.ini` plus the `config.local.ini` overlay, parses `argv`)
and `botlog.py` (validates the admin hash prefix and the IRC mode strings). See
[Startup validation](#ops-startup-validation).

### CLI flags

Parsed in `config.py` at import. All optional.

| Flag | Effect |
|---|---|
| `--version` | Print the version and exit (during import). |
| `--debug [SUBSYSTEM ...]` | No args: global debug. With args: per-subsystem. |
| `--loglevel LEVEL` | Base level, overriding `[logging] level`. |
| `--debug-file PATH` | Unfiltered DEBUG copy to a separate rotating file. |
| `--no-console` | Disable the interactive stdin console. |

Flags survive a `.restart`: the restart re-execs the same `sys.argv`.

### The interactive console

`_main()` starts the stdin console only when stdin is an interactive TTY and
`--no-console` was not given. On entry it logs `event=console_active` at WARNING,
because the console is **admin-equivalent without authentication** - anyone with
stdin access can `shutdown`. Run daemonized deployments with `--no-console`.
Mechanism: [internals/console.md](internals/console.md).

(ops-startup-validation)=
### Startup validation that can refuse to boot

| Check | Owner | Outcome |
|---|---|---|
| `config.ini` missing or unreadable | `config.py` | `SystemExit` with a remediation message |
| Empty `command_prefix` | `config.py` | `SystemExit` (every message would be a command) |
| `password_hash` prefix not scrypt / bcrypt / argon2 | `botlog.py - _validate_hash()` | CRITICAL, exit 1 |
| `user_modes` / `oper_modes` / `oper_snomask` outside `^[a-zA-Z+\- ]*$` | `botlog.py` | CRITICAL, exit 1 |
| World-readable `config.ini` (POSIX) | `botlog.py` | WARNING only, suggests `chmod 640` |
| Empty `password_hash` | `botlog.py` | WARNING only; admin auth disabled, bot still runs |

A present-but-incomplete `config.ini` (missing a section or key, a non-integer
`port`) fails with a raw `KeyError` or `ValueError` traceback rather than a curated
message. That inconsistency is recorded in
[internals/config.md](internals/config.md#findings).

## Stopping the bot

Preferred, in order:

| Method | Who can use it | Notes |
|---|---|---|
| `.shutdown` / `.die [reason]` | authenticated admin over IRC | Audited before shutdown begins |
| `shutdown` / `quit` | console | No authentication; TTY access is the control |
| `SIGINT` / `SIGTERM` | shell | `kill -INT <pid>`; idempotent, repeats logged and ignored |

```bash
kill -INT "$(cut -d'|' -f1 internets.pid)"
```

Do not use SIGHUP to stop the bot: SIGHUP is rehash. See
[Refresh semantics](#ops-refresh).

All paths funnel through `IRCBot.request_shutdown()` (first caller wins, so a SIGINT
during a clean shutdown cannot rewrite the QUIT message) into
`IRCBot.graceful_shutdown()`, which runs eight individually-guarded steps in this
order: save channels, unload modules, stop the store flush thread with a final write,
enqueue QUIT at priority 0, sleep 2 s for the sender to drain, stop the sender, close
the socket, cancel tasks, stop the metrics server, flush log handlers. The ordering is
load-bearing - channels are persisted before anything else can fail, and QUIT is
enqueued before the sender stops. Look for `event=shutdown_begin` and
`event=shutdown_complete` in the log.

A hard kill (`kill -9`) is safe for the on-disk data in the sense that writes are
atomic, but loses up to 30 seconds of unflushed store mutations and leaves a stale
lockfile that the next start clears automatically.

(ops-process-lock)=
## Process lock

`process_lock.py` prevents two instances from racing on the same JSON state files,
whose tmp-and-rename writes would otherwise clobber each other. The lockfile
`./internets.pid` holds one line, `pid|start_time|hostname`. Creation is atomic
(`O_CREAT | O_EXCL`).

Acquire-time decisions, from `ProcessLock.acquire()`:

| Existing lockfile says | Probe | Result |
|---|---|---|
| Live PID, same host | `os.kill(pid, 0)` succeeds | Refuse (`LockHeld`), file untouched |
| PID owned by another user | `PermissionError` | Treated as alive, refuse |
| Dead PID, same host | `ProcessLookupError` | Warn, unlink, proceed |
| Different hostname | not probed | Refuse (a foreign PID cannot be verified) |
| Corrupt or empty file | parse fails | Warn, unlink, proceed |
| Liveness unknowable (non-POSIX, no `psutil`) | - | Warn, unlink, proceed (fail-open) |

`release()` re-reads the file and unlinks only if it still contains our PID, so a
process can never destroy a lock someone else now owns. It is idempotent.

### Recovering a stuck lock

1. Confirm no bot is running: `ps -p "$(cut -d'|' -f1 internets.pid)"`.
2. If the PID is dead and the hostname field matches this host, just start the bot -
   stale detection clears the file itself.
3. If the file names a **different host** (shared NFS or a bind-mounted Docker
   volume), or the PID has been reused by an unrelated live process, startup refuses
   and cannot self-clear. Verify the other instance is really gone, then delete
   `internets.pid` by hand.

The recorded `start_time` is the lock acquisition wall clock, not the OS process
start time, and no decision consults it - it cannot distinguish PID reuse. That is
why case 3 is manual.

### Restart interaction

`os.execv` preserves the PID, so the restart path releases the lock **before**
exec'ing; otherwise the new image would probe its own live PID and refuse to start.
A release failure there logs `event=restart_lock_release_failed` and the exec
proceeds anyway, which produces exactly that self-deadlock on the next boot. If a
`.restart` never comes back and the log carries that event, delete the lockfile and
start manually.

:::{warning}
**Known defect (concurrency window).** The stale-reclaim path is examine, unlink,
then exclusive-create, which is not atomic. Two processes started simultaneously over
the same stale lockfile can interleave so both acquire. The window is microseconds and
only reachable after a crash left a stale file. Recorded in
[internals/process_lock.md](internals/process_lock.md#findings) and the reconstruction
findings ledger.
:::

(ops-refresh)=
## Restart, rehash, reload: which one

Three refresh mechanisms with different scopes. Picking the wrong one is the classic
operational mistake, because the wrong one succeeds quietly and leaves stale code or
stale values running.

| Mechanism | Refreshes | Does not refresh |
|---|---|---|
| `.reload <mod>` / `.reloadall` | one or all command modules under `modules/` | helper modules, providers, core files, config constants |
| `.rehash` / `SIGHUP` | the live `cfg` mapping (both config layers) | import-time constants, credentials, autoload list |
| `.restart` (execv) | everything, same argv | nothing; it is a full process replacement |

### `.reload` - command modules only

`IRCBot.reload_module()` is strictly `unload_module()` then `load_module()`, and
`load_module()` builds a fresh module object every time via
`spec_from_file_location` + `module_from_spec` + `exec_module`. **No `sys.modules`
entry is ever created for the loaded module**, so editing a command module file and
reloading it picks up the new source with no bytecode staleness.

The trap: anything the module *imports* goes through the normal import system and
**is** cached in `sys.modules`. `modules/base.py`, `modules/geocode.py`,
`modules/units.py`, and everything under `weather_providers/` are cached after their
first import. Reloading a command module re-runs its top-level imports, but Python
hands back the already-cached helper object. Your edit to `geocode.py` does not take
effect. The same applies to `config.py` constants and every core file.

If `on_unload()` raises, the module stays fully loaded with its commands intact
rather than being half-removed, and the reload aborts.

### `.rehash` / SIGHUP - config only, no link drop

Both call `config.reload_config()`, which re-reads `config.ini` **and**
`config.local.ini` together. Re-reading only the template would clobber an
overlay-only `password_hash` with the template's empty placeholder, because
`configparser.read()` merges rather than replaces. Both paths then clear all admin
sessions defensively (a rehash may have rotated the password), and `.rehash`
additionally re-applies the base log level, clears debug overrides, and re-validates
the hash prefix.

What becomes live: anything read from `cfg` at use time. `command_prefix` is the
canonical example - `IRCBot._cmd_prefix()` reads it per dispatch specifically so a
prefix change takes effect on rehash.

What does not: every module-level constant in `config.py`, including `NS_PW`,
`SERVER_PW`, `OPER_PW`, `SERVER`, `PORT`, `NICKNAME`, `AUTO_LOAD`, `MODULES_DIR`,
`API_CD`, `FLOOD_CD`. The SIGHUP log line says so explicitly
(`note=defensive_no_cred_reload`). Changing a credential requires a restart.

Look for `event=rehash_ok` or `event=rehash_failed`. A malformed config on rehash is
caught and logged; the previous in-memory values survive rather than killing the bot.

:::{warning}
**Known defect (config).** Import-time validation is not re-applied on reload. A
rehash can load an **empty `command_prefix`** into the live `cfg`, and
`IRCBot._cmd_prefix()` then returns `""` because the key exists so the constant
fallback never fires - recreating the every-message-is-a-command hazard the
import-time guard exists to prevent. Recorded in
[internals/config.md](internals/config.md#findings).
:::

### `.restart` - full process replacement

`.restart` sets `_restart_flag` and requests shutdown. After the loop tears down,
`_main()` closes the log handlers (clean rotation across the boundary), releases the
process lock, and re-execs: `os.execv(sys.executable, [sys.executable] + sys.argv)`
on POSIX, `subprocess.Popen` + exit 0 on Windows. Look for `event=restart_exec`.

Restart is required for: any core file, any helper module, `weather_providers/`,
`config.py` constants, credentials, `AUTO_LOAD` changes, and dependency upgrades.
When in doubt, restart. A partial reload is silently stale, which is worse than a
five-second outage.

## Logs

`botlog.py` builds the whole logging stack at import time on the `internets` logger,
which is set to DEBUG with handlers cleared then rebuilt.

| Handler | Sink | Filtered | Rotation |
|---|---|---|---|
| Main file | `[logging] log_file` | yes (`DebugFilter`) | `max_bytes` (default 5 MiB) x `backup_count` (default 3) |
| Stream | stdout | yes | n/a |
| Debug file (optional) | `[logging] debug_file` or `--debug-file` | **no** | same caps |

Handler levels are all DEBUG. The effective severity gate is the `DebugFilter`, not
the handler level. The debug-file handler deliberately carries no filter, so it
captures everything at DEBUG regardless of the running base level - that is the
handler to enable for protocol diagnostics. Rotation is size-based only; there is no
time-based rotation, and no external logrotate configuration is needed or expected.

Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`. Subsystem loggers are
`internets.<name>` (for example `internets.sender`, `internets.store`,
`internets.modules`, `internets.secrets`), which is what per-subsystem debug targets.

All user-controlled data passes through `_SafeFormatter`, which strips C0 controls
(except TAB), DEL, and C1 controls from `record.msg` and `record.args` on a copy of
the record. That defeats CRLF log forging and terminal escape injection. It is
**not** credential scrubbing: there is no redaction hook in the logging layer, and
the codebase relies on call-site discipline instead.

### Runtime log control

Three routes, all mutating the same process-global filter instance:

| Route | Base level | Global debug | Per-subsystem |
|---|---|---|---|
| IRC admin | `.loglevel LEVEL` | `.debug on` / `off` | `.loglevel <logger> LEVEL`, `.debug <sub> [off]` |
| Console | `loglevel LEVEL` | `debug on` / `off` | same shapes |
| CLI at start | `--loglevel` | `--debug` | `--debug a b` |

`.loglevel` with no arguments prints the current base level, the global debug flag,
active subsystems, and the debug file path. Valid levels are DEBUG, INFO, WARNING,
ERROR.

:::{warning}
**Known defect (log control).** Setting a base level via `.loglevel LEVEL` clears
`global_debug` but leaves per-subsystem debug sets active, so raising the level to
WARNING still emits DEBUG from any previously enabled subsystem. Only `.rehash`
clears both. Recorded in [internals/botlog.md](internals/botlog.md#findings).
:::

## Audit log

`audit_log.py` records every privileged action as one JSON line in `./audit.log`,
HMAC-chained so an edit, reorder, or deletion is detectable by re-walking the chain.
It is separate from the bot log because it has different requirements: durable,
bounded, 0600 (records carry hostmasks, which are PII), and tamper-evident.

| Artifact | Mode | Purpose |
|---|---|---|
| `audit.log` | 0600 | live chain, JSON lines |
| `audit.log.key` | 0600 | 32-byte HMAC key, generated on first use |
| `audit.log.<UTC stamp>` | 0600 | rotated segments, 5 MiB threshold |
| `audit.log.key.bad` | 0600 | a malformed key moved aside, never truncated |

### Routine review

```text
.audit                 last 10 records
.audit 50              last N, clamped to [1, 200]
.audit tail            last 5
.audit grep <pattern>  case-insensitive substring, last 50 matches
.audit verify          re-walk the HMAC chain
```

`.audit verify` answers either `audit chain intact (N records).` or
`audit chain BROKEN at record index N.` (zero-based). `.health` reports the same
verification as one line, and `.stats` includes the record count.

Review cadence in practice: check `.audit verify` whenever you check health, and read
`.audit grep auth_failed` after any report of a failed login. Because rotation resets
the chain, verify is only ever a statement about the live file.

### What review will not catch

:::{warning}
**Known defect (verify downgrade).** `AuditLog.verify()` picks the hash algorithm
from each record's own `v` field. A record lacking `v == 2` is verified with
**unkeyed** SHA-256, at any chain position. Anyone with write access to `audit.log`
alone - no key needed, the algorithm is in the source - can truncate at any record
and append forged legacy-format records that chain cleanly, and `verify()` will
report the chain intact. This contradicts the module docstring's claim that all
non-tail edits are caught, and reduces effective tamper-evidence to the pre-3.0.0
scheme. Verified by the orchestrator; see the reconstruction findings ledger and
[internals/audit_log.md](internals/audit_log.md#findings). Operator consequence:
treat an intact verdict as evidence of accidental corruption being absent, not as
proof against a motivated local writer. Ship `audit.log` off-host if that matters.
:::

Three further limits, all in
[internals/audit_log.md](internals/audit_log.md#findings):

- **Rotated segments are never verified or displayed.** `verify()`, `.audit`,
  `count()`, and `.health` read only the live file. Deleting a whole rotated segment
  is undetectable by the bot's own tooling; inspect segments on disk.
- **No `fsync`.** `record()` writes and closes, so durability stops at the kernel
  page cache. A power loss can drop the most recent records, including the
  restart/shutdown record written moments before exit. Two source comments claim
  otherwise; they are stale.
- **Same-second rotation collision.** The rotation stamp has one-second granularity
  and `Path.rename()` replaces silently, so two rotations inside one second would
  destroy the first segment. Implausible under the capped auth-flood path, but the
  failure is silent.

### Key handling

An existing but **unreadable** key makes `record()` fail closed rather than
regenerate, because regeneration would silently void every prior record's HMAC. The
caller (`AdminCommandsMixin._audit()`) swallows that and logs a warning, so the admin
action still completes - availability over audit coverage, deliberately. A malformed
or short key is moved to `audit.log.key.bad` and a fresh one generated; prior records
then fail verification under the new key, and recovery is manual from the `.bad`
backup. Back up `audit.log.key` with the log, and keep them together.

## State files and backup

All paths default to the working directory; override under `[bot]`. `store.py` loads
each file once at startup and a daemon thread flushes dirty datasets every 30 s.
Worst-case loss on a hard crash is about 30 seconds of mutations.

| File | Key | Contents | Sensitivity |
|---|---|---|---|
| `locations.json` | `locations_file` | per-nick saved locations | user-supplied text |
| `channels.json` | `channels_file` | joined channel list | low |
| `users.json` | `users_file` | per-channel nick, hostmask, seen times, opt-out | PII |
| `shadow_bans.json` | `shadow_bans_file` | shadow-banned nicks and reasons | low |
| `audit.log` (+ `.key`) | fixed | privileged action trail | PII, tamper-evident |
| `internets.pid` | fixed | process lock | none |

Module-owned state files (for example `seen.json`, `tells.json`, `notes.json`,
`steamids.json`) live beside these and are listed per module in
[internals/modules/](internals/modules/index.md).

Writes are atomic: a `mkstemp` temp file in the same directory, chmod 0600 **before**
the rename, a one-deep `<name>.bak` copy of the previous good file, then
`os.replace()`. Readers never observe a half-written file. There is no `fsync` before
the rename, so a power loss can still lose the newest version; the checksum envelope,
the quarantine path, and the `.bak` turn that into a detected and recoverable event
rather than silent corruption.

Every file carries a v2 envelope: `{"schema": 2, "checksum": <sha256>, "data": ...}`.
A file that is unreadable, over 10 MiB, not valid JSON, wrong schema, checksum
mismatched, or the wrong top-level type is **quarantined** to
`<name>.corrupt.<unixtime>` and the dataset starts empty
(`Store: <path> unusable (<reason>) - quarantined to <dest>`). It is never silently
reset, because the next flush would then overwrite the only copy. Recovery is a
manual rename of the `.corrupt.*` or the `.bak` back into place while the bot is
stopped.

:::{warning}
**Known defect (permissions).** The `.bak` copy is written with `Path.write_bytes`
and never chmodded, so on first creation it takes umask-default permissions
(typically 0644) while the live file is 0600. The PII in `users.json` is therefore
world-readable in `users.json.bak`. Verified; recorded in the findings ledger and
[internals/store.md](internals/store.md#findings). Until it is fixed, `chmod 600
*.bak` belongs in the maintenance checklist.
:::

### What to back up

Stop the bot first, or accept that the newest 30 seconds may be missing.

- `config.ini` - securely; it holds every secret. Never commit it.
- `audit.log`, `audit.log.key`, and all `audit.log.*` rotated segments, together.
- `locations.json`, `channels.json`, `users.json`, `shadow_bans.json`, plus module
  state files.
- Keep `.bak` and `.corrupt.*` files until you have confirmed the live files load.

Do not back up `internets.pid`. Restoring one would refuse a start.

## Health checking

| Command | Audience | Shows |
|---|---|---|
| `.uptime` | admin (core) | process age and connection age |
| `.health` | admin | per-subsystem snapshot, delivered privately |
| `.stats` | admin | counters, queue depth, RSS, audit record count |
| `.providers` | admin | weather provider health and capability chains |

`.health` (`modules/health.py - HealthModule.cmd_health()`) reports, one line each:
uptime, module count with per-module configured badges, weather provider states,
sender queue depth, store dirty flags, geocode cache stats, authenticated admin
count, audit chain integrity, and bot counters. Every probe is individually wrapped,
so a broken subsystem degrades one line instead of failing the command.

:::{warning}
**Known defect (shadowed public `.uptime`).** `modules/health.py - HealthModule`
registers `uptime` alongside `health`, intending a public, module-load-relative
uptime. It is unreachable: `IRCBot._dispatch()` resolves `_CORE` before the module
registry, and `_CORE` maps `uptime` to the admin-gated
`admin_cmds.py - cmd_uptime()`. The module loader's collision check only compares
against other *modules*, so nothing rejects or warns about the shadowing at load time.
Every `.uptime` invocation therefore runs the core admin handler and a non-admin gets
the auth prompt, never the intended public figure. A secondary effect: the
`internets_commands_total` metric labels the call `module="health"` because the label
is derived from the module registry, while the core handler is what actually ran.
Discovered during this documentation pass; not previously in the findings ledger.
:::

:::{warning}
**Known defect (health).** The store section reads `store._dirty_locations` and
`store._dirty_channels`, but `store.py - Store` defines `_dirty_locs` and
`_dirty_chans`. The `getattr` default means `.health` prints `?` for those two
datasets permanently while looking wired. Only `_dirty_users` is real. Verified;
recorded in the findings ledger and
[internals/modules/health.md](internals/modules/health.md#findings).
:::

`.stats` reaches into `sender._q.qsize()` for queue depth, documented as approximate
but adequate. A persistently non-zero queue depth means outbound throttling is
backed up; see the token bucket in [internals/sender.md](internals/sender.md).

## Metrics endpoint

`metrics.py` is inert by default: importing it builds the registry, and increments
are accepted and stored, but no listener exists until `enable()` and `expose()` are
both called from `_main()`. Enable it in config:

```ini
[metrics]
enable = true
host = 127.0.0.1
port = 9779
```

Then scrape `http://127.0.0.1:9779/metrics`. The handler serves Prometheus text
exposition (version 0.0.4) on `GET /metrics` only; every other path 404s. Startup
failure is non-fatal and logs `event=metrics_start_failed`; success logs
`event=metrics_enabled`. `graceful_shutdown()` stops the server, joining its thread
with a 2 s cap.

Operational constraints to plan around:

- **The endpoint is unauthenticated.** The only enforced guard rejects
  *unspecified* binds (`0.0.0.0`, `::`, `::0`, IPv4-mapped equivalents, empty or
  whitespace host) by parsing with `ipaddress` and testing `is_unspecified`. A
  specific routable IP, or a hostname resolving to one, **passes the guard and
  binds**, despite an error message that says loopback-only. Bind loopback and front
  it with an authenticating reverse proxy if you need off-host scraping.
- **Single-threaded server.** It is `HTTPServer`, not `ThreadingHTTPServer`, with no
  socket timeout: one stalled scraper connection blocks subsequent scrapes.
- **Nothing persists.** All counters reset on restart. Rate queries are unaffected;
  raw totals are not.

:::{warning}
**Known defect (unwired metrics).** Six of the ten registered metrics have no update
call site anywhere in the codebase: `internets_provider_calls_total`,
`internets_provider_quota_used`, `internets_module_loaded`,
`internets_provider_active`, `internets_sender_queue_depth`, and
`internets_authed_admins_count`. A metric with no samples renders as a constant
`name 0`, which reads on a dashboard as healthy-but-idle rather than
not-instrumented. Do not build alerts on those six. The four live ones are
`internets_commands_total`, `internets_reconnects_total`,
`internets_dropped_messages_total`, and `internets_audit_records_total`. Recorded in
[internals/metrics.md](internals/metrics.md#findings).
:::

## Upgrade procedure

1. **Read the CHANGELOG entry for the target version first.** Breaking changes and
   the operator action they demand are recorded there, not here. A major release can
   require action before the bot will authenticate you again: v5.0.0 stopped
   accepting a bcrypt password longer than 72 UTF-8 bytes, and the only symptom is
   `.auth` answering `wrong password.`
2. Stop the bot gracefully (`.shutdown`, or `kill -INT`). Confirm `internets.pid` is
   gone.
3. Back up per [What to back up](#what-to-back-up) before touching anything.
4. `git pull`.
5. If `requirements.txt` or `pyproject.toml` changed, refresh the environment
   (`pip install -r requirements.txt`, or `pip install -e ".[dev]"` for dev extras).
   Review the lockfile diff before trusting it.
6. If `config.ini.example` gained keys you need, merge them into `config.ini` by
   hand. Never copy real values into the example. Keep `config.ini` at 0600.
7. Smoke-test: `python tests/run_tests.py`, then the pytest suite.
8. Start with the same flags your service uses.

A pull that touched **only** command modules under `modules/` can be picked up with
`.reloadall` and no restart - but only those files, never helpers, providers,
`config.py`, or core files. See [Refresh semantics](#ops-refresh).

:::{warning}
**Known defect (dependencies).** `requirements.lock` was generated on Python 3.14,
violating the resolve-on-3.10 contract in `scripts/regen-lockfile.sh`, and omits
marker-gated transitives (`typing_extensions>=4.4`, pulled by `aiohttp`). Every CI
leg below Python 3.13 fails the `--require-hashes` install, and the Tests workflow has
been red on `main` since 2026-08-13. If you install from the lock on Python 3.10 to
3.12 you will hit the same failure. Regenerate the lock per the script until this is
fixed. Recorded in the findings ledger and
[internals/ci-and-packaging.md](internals/ci-and-packaging.md#findings).
:::

## Routine maintenance checklist

Weekly, or after any incident:

- [ ] `.health` - every line reads plausibly; note that two store dirty flags print
      `?` by defect, not by fault.
- [ ] `.audit verify` - intact, and the record count moved about as much as the
      admin activity you expect.
- [ ] `.stats` - sender queue depth near zero, dropped-message counter not climbing,
      RSS stable across the week.
- [ ] Log tail for `event=reconnect_failed`, `event=pong_timeout`,
      `event=sasl_failure`, `event=module_load_failed`, `event=store_flush_failed`.
- [ ] Disk: `audit.log` size against the 5 MiB rotation point, log file plus its
      three backups, no accumulating `*.corrupt.*` files.
- [ ] Permissions: `config.ini` 0600, `audit.log` and `audit.log.key` 0600,
      `*.json` 0600, and `chmod 600 *.bak` for the known `.bak` defect above.
- [ ] Backups ran and a restore was spot-checked at least once per quarter. A
      recovery path you have never executed is unverified.

Per release:

- [ ] CHANGELOG read before upgrading.
- [ ] Test suite green locally before starting the new version.
- [ ] Secrets still resolve after any config edit: `python -m secret_store status`
      and `python -m secret_store list` (neither prints a value).

Per quarter:

- [ ] Rotate the admin password (`python hashpw.py`, update `password_hash`,
      `.rehash`) and any provider API keys.
- [ ] Prune or archive rotated `audit.log.*` segments off-host, keeping them with
      the key.
- [ ] Confirm `user_max_age_days` retention still matches your privacy posture; see
      [internals/store.md](internals/store.md).
