# Deployment and operations

Operations manual for Internets v4.0.0. Grounded in `console.py`, `process_lock.py`,
`botlog.py`, `internets.py`, `config.py`, `metrics.py`, `store.py`, `audit_log.py`,
`admin_cmds.py`. Read those alongside this.

## Running the bot

Two equivalent entry points:

```
python internets.py            # run from a checkout
internets                       # console_script (pyproject [project.scripts]: internets = "internets:_entry")
```

Both land in `_entry()` (`internets.py:1446`). `_entry()` does, in order:

1. **Drop-root guard** (POSIX). If `os.geteuid() == 0` and `INTERNETS_ALLOW_ROOT != "1"`,
   it logs `event=refused_root_start` and `sys.exit(1)`. Set `INTERNETS_ALLOW_ROOT=1`
   to override (e.g. binding a port < 1024 without `setcap`). Windows has no euid;
   the check is skipped there.
2. **Acquire the process lock** at `./internets.pid` (resolved to absolute), as a
   context manager around `asyncio.run(_main(lock))`. `LockHeld` -> log
   `Another bot instance is already running` and `sys.exit(1)`.
3. On `KeyboardInterrupt` after the loop: log `event=keyboard_interrupt`, `sys.exit(130)`
   (128 + SIGINT). No traceback. Other exceptions are deliberately NOT buried.

`config.py` reads `config.ini` (and the optional `config.local.ini` overlay) at
**import time**, before `_main` runs. A missing/unreadable `config.ini` raises
`SystemExit` with an actionable message (`config.py:72`) pointing at
`python -m secret_store init`. `botlog.py` (also import-time) then validates the admin
hash and config modes and can `sys.exit(1)` before the loop ever starts - see
[Startup validation](#startup-validation).

### CLI arguments

Parsed in `config.py:115` (`argparse`). All optional:

| Flag | Effect |
|------|--------|
| `--version` | Print `Internets 4.0.0` and exit. |
| `--debug [SUBSYSTEM ...]` | No args = global debug (all subsystems). With args = per-subsystem, e.g. `--debug weather store`. Applied in `botlog.py:150`. |
| `--loglevel LEVEL` | Base level: `DEBUG`/`INFO`/`WARNING`/`ERROR`. Overrides `[logging] level`. |
| `--debug-file PATH` | Write ALL output at DEBUG to a separate rotating file. Overrides `[logging] debug_file`. |
| `--no-console` | Disable the interactive stdin console (for daemons). |

`--debug` subsystem names are normalized to `internets.<name>` unless they already
start with `internets`.

### The interactive console

When stdin is an interactive TTY and `--no-console` is not set, `_main` starts a console
task (`internets.py:1362`). It runs the dispatch loop on a **daemon thread**
(`console.py:140`), not `asyncio.to_thread`, because `input()` parks on a blocking
`read(0)` that nothing short of process death interrupts; a non-daemon thread would hang
`asyncio.run()`'s `shutdown_default_executor()` forever on exit (`console.py:101` docstring).

`should_skip_console()` (`console.py:42`) returns True when `sys.stdin.isatty()` is False
(systemd, `docker run` without `-it`, piped/redirected stdin) or stdin is gone. In that
case `_main` logs `Console skipped: stdin is not a TTY`. On entry the console logs a loud
`event=console_active` WARNING: it grants **admin-equivalent capability without
authentication** to anyone with stdin access (debug, loglevel, status, shutdown). Run
daemonized deployments with `--no-console`, or under a dedicated unprivileged user with
no shared shell.

Console commands (`_console_dispatch_loop`, `console.py:58`; dispatch chain at
`console.py:83`): `help`, `debug`,
`loglevel`, `status`, `shutdown`/`quit`. They are safe to call off-loop:
`apply_debug`/`apply_loglevel` touch RLock-guarded logging state; `_print_status` reads
bot fields through their own `threading.Lock` accessors; `request_shutdown` uses
`loop.call_soon_threadsafe`. The loop exits on EOF (Ctrl-D), Ctrl-C, stdin close, or a
`shutdown`/`quit` command. `status` has no IRC equivalent - it prints version, nick,
channels, modules, admins, and log state.

## Process lock (single-instance enforcement)

`process_lock.py`. Prevents two instances racing on the JSON state files (locations /
channels / users / shadow_bans), whose tmp-and-rename writes would clobber each other.

- Lockfile `./internets.pid` stores `pid|start_time|hostname` (`process_lock.py:209`).
  The path is resolved at `acquire()` time against the then-current CWD, not at
  construction, so a relative path tracks the startup CWD (`process_lock.py:131`). It is
  NOT `.resolve()`d, so a not-yet-existing parent dir is tolerated.
- Creation is atomic via `os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o644)`. Losing the
  `O_EXCL` race raises `LockHeld` (`process_lock.py:204`).
- **Stale detection** on an existing lockfile (`process_lock.py:153`):
  - Same hostname -> probe liveness with `os.kill(pid, 0)`. Alive -> refuse (`LockHeld`).
    `ProcessLookupError`/`ESRCH` -> dead -> remove stale file and continue.
    `PermissionError` (process owned by another user) -> treated as **alive** (conservative
    refusal beats clobbering state).
  - Different hostname (shared NFS / Docker volume) -> cannot probe -> treated as alive ->
    refuse. The operator deletes the lockfile by hand if sure the other host is dead.
  - Unreadable/corrupt lockfile -> log and remove, continue.
  - Non-POSIX without `psutil` -> fail-open: log and take the lock.
- `release()` (`process_lock.py:220`) re-reads the file and only unlinks if it still
  contains our PID; a PID mismatch logs `not releasing ... pid mismatch` and skips. It is
  idempotent.

**Restart interaction:** `os.execv` preserves the PID. The restart path releases the lock
*before* `execv` (`internets.py:1426`); otherwise the new image would see its own
preserved PID as a live holder and refuse to start. See [Restart](#restart-execv).

**Recovering a stuck lock:** if the bot was `kill -9`'d on the same host, the next start
auto-clears the stale file (dead PID). If the file names a *different* host, or the PID was
reused by an unrelated live process, startup refuses - delete `internets.pid` manually
after confirming no instance is running.

## Logging

`botlog.py`. The `internets` logger is configured at import time (`_setup_logging`,
`botlog.py:112`), set to `DEBUG`, handlers cleared then rebuilt:

- **Main rotating file** -> `LOG_FILE` (`[logging] log_file`), `RotatingFileHandler` at
  `LOG_MAX` bytes, `LOG_BACKUPS` old copies. Handler level DEBUG; the `DebugFilter`
  decides what passes.
- **Console stream** -> stdout, same formatter and filter.
- **Optional debug file** -> only if `LOG_DEBUG` is set (`[logging] debug_file` or
  `--debug-file`). Same rotation params, captures everything at DEBUG regardless of base
  level (no `DebugFilter` attached). Useful for protocol diagnostics.

Rotation defaults (`config.py:144`): `LOG_MAX` = `[logging] max_bytes` (default
`5242880`, 5 MB), `LOG_BACKUPS` = `[logging] backup_count` (default `3`). So the main log
occupies at most ~4 files (`log` + `.1`..`.3`). The debug file rotates with the same
caps. Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.

**Log injection defense:** `_SafeFormatter` (`botlog.py:28`) strips C0 controls (except
TAB), DEL, and C1 from `record.msg` and `record.args` on a *copy* of the record, so
attacker-supplied strings can't forge log lines. Exception tracebacks survive (rendered
into `exc_text`, not `msg`).

### Runtime log control

The `DebugFilter` (`botlog.py:64`) passes a record if level >= `base_level`, OR
`global_debug` is on, OR the record's logger name matches an enabled subsystem prefix.
Subsystem set is `threading.Lock`-guarded. Control it three ways, all hitting the same
filter instance:

- IRC admin: `.loglevel`, `.loglevel <logger> LEVEL`, `.debug [on|off]`,
  `.debug <subsystem> [off]`.
- Console: `loglevel`, `debug` (same handlers via `apply_loglevel`/`apply_debug`).
- CLI at startup: `--loglevel`, `--debug`.

Setting a base level via `.loglevel LEVEL` also forces `global_debug` off
(`botlog.py:278`). `.rehash` resets the base level from `[logging] level` and clears all
debug subsystems (`admin_cmds.py:451`).

### Startup validation (can refuse to boot)

`botlog.py` runs these at import, before the loop:

- `_validate_hash()` (`botlog.py:180`): reads `[admin] password_hash` via
  `reload_config()`. Empty -> WARNING, auth disabled, bot still runs (first-run before
  `hashpw.py`). Non-empty but prefix not in `scrypt`/`bcrypt`/`argon2` -> `log.critical`
  + `sys.exit(1)` (an unrecognized prefix would make `verify_password` raise on every
  auth, silently disabling admin; fail-closed is louder). The invalid prefix is NOT echoed.
- World-readable `config.ini` (POSIX, `st_mode & 0o004`) -> WARNING suggesting `chmod 640`.
- `user_modes`/`oper_modes`/`oper_snomask` must match `^[a-zA-Z+\- ]*$` or `sys.exit(1)`.

## Configuration for a deploy

Single merged `config.ini`, **0600**, holds everything: server/nick/modules plus a
`[secrets]` section. `config.ini.example` is the committed credential-free template -
never edit it with real values. An optional gitignored `config.local.ini` overlays
non-secret personal values on top.

`config.py` loads `config.ini` then overlays `config.local.ini` if present
(`reload_config`, `config.py:43`). configparser `read()` only overrides keys present in
the re-read file, so **every reload path must go through `reload_config()`** - re-reading
`config.ini` alone would clobber a `password_hash` that lives only in the overlay with the
template's empty placeholder. Reads are pinned to UTF-8 (the example file uses non-ASCII
header glyphs; the platform locale would raise `UnicodeDecodeError`).

### Secrets

Lookup order, first hit wins (`config.py:24`, `_secret_or_cfg`):

1. `INTERNETS_<NAME>` environment variable.
2. `config.ini` `[secrets]` section - read **only** when perms are exactly `0600`; the
   store fails closed (returns empty) on looser perms.
3. Legacy plaintext field in the value's own non-secret section (`_secret_or_cfg`,
   `config.py:24`), e.g. `[irc] nickserv_password` (`config.py:86`) - consulted only when
   both the env var and `[secrets]` are empty.

Secrets covered: NickServ/SASL/server/oper passwords, every provider/API key, the
`weather_user_agent` contact identifier. Manage via `python -m secret_store`
(`init`/`status`/`list`/`get`/`set`/`delete`/`migrate`). `get` never prints the value -
extract for rotation with `python -c "import secret_store; print(secret_store.get('<name>'))"`.

**Never** create, restore, or hand-edit `config.ini`, `config.local.ini`, or any
secret/PII file without explicit per-file approval. These files are not in the repo and
must not be.

Migrating from an old separate `secrets.ini`:

```bash
{ echo; cat secrets.ini; } >> config.ini
shred -u secrets.ini
chmod 600 config.ini
```

Then restart (env vars still win over the file). OS-keyring support was removed in 3.0.0.

## Reload vs restart

Three distinct refresh mechanisms with different scopes. Picking the wrong one is the
classic gotcha.

### `.reload` / `.reloadall` - command modules only

`reload_module` = `unload_module` then `load_module` (`internets.py:504`). `load_module`
(`internets.py:452`) builds a *fresh* module object every time via
`importlib.util.spec_from_file_location` + `module_from_spec` + `exec_module`. It does NOT
populate or consult `sys.modules` for the `modules.<name>` entry, so editing a command
module file and running `.reload <name>` picks up the new source immediately. All module
operations hold `self._mod_lock`.

**The trap:** helper modules under `modules/` that command modules `import` (notably
`modules/geocode.py` and `modules/units.py`, and anything in `weather_providers/`) ARE
cached in `sys.modules` after their first import. `exec_module` re-runs the command
module's top-level `import geocode`, but Python returns the already-cached helper object -
your edits to `geocode.py` do **not** take effect. A full process restart is the only way
to refresh a helper or any non-command module. The same applies to `config.py`'s
import-time constants and to core files (`internets.py`, `sender.py`, `store.py`, etc.).

`load_module` guards: module name must match `^[a-z][a-z0-9_]*$`; the resolved path must
stay inside `MODULES_DIR` (symlink/traversal escape -> rejected); the module must expose
`setup(bot)`; a command-name collision with another loaded module is rejected (the second
loser, not the incumbent). Failures return a generic "see log for details" to IRC.

### `.rehash` / SIGHUP - config only, no link drop

`.rehash` (`admin_cmds.py:434`) and SIGHUP (`_on_sighup`, `internets.py:1309`) both call
`reload_config()` to re-read `config.ini` + `config.local.ini` into the live `cfg`, then
clear all admin sessions defensively. What this refreshes: values read at use-time, e.g.
`command_prefix` via `_cmd_prefix()` (`internets.py:589`), which is why the core reads the
prefix live instead of the frozen import-time `CMD_PREFIX`.

What it does **NOT** refresh: the import-time credential constants `NS_PW`/`OPER_PW`/
`SERVER_PW` and other module-level constants in `config.py`. A live on-wire credential
reload is intentionally out of scope; the SIGHUP log says so
(`note=defensive_no_cred_reload`). Changing a password or any import-time constant needs a
full restart. `.rehash` also re-validates the hash prefix and resets the log base level.

### Restart (execv)

`.restart` (`admin_cmds.py:424`) sets `bot._restart_flag = True` then `request_shutdown`.
After `graceful_shutdown` completes and tasks drain, `_main` (`internets.py:1412`) closes
logging file handlers (clean rotation across the restart), **releases the process lock**
(PID survives `execv`, see [Process lock](#process-lock-single-instance-enforcement)),
then re-execs:

- POSIX: `os.execv(sys.executable, [sys.executable] + sys.argv)` - replaces the image.
- Windows: `subprocess.Popen(...)` then `sys.exit(0)` (execv doesn't replace the process
  on NT).

`argv` is preserved, so CLI flags carry across the restart. A restart is required for any
change outside a command module's own source: helper modules, `config.py` constants, core
files, dependency upgrades.

### Graceful restart from the shell

Send SIGINT or SIGTERM (`_on_signal`, `internets.py:1294`) and relaunch. Both trigger
`request_shutdown`; the handler is idempotent (a second signal during shutdown is logged
and ignored). SIGHUP is rehash, not shutdown - do not use it to restart. On POSIX the
handlers are installed via `loop.add_signal_handler` (`internets.py:1112`/`1116`); Windows has no
such API and relies on `KeyboardInterrupt`.

```
kill -INT "$(cat internets.pid | cut -d'|' -f1)"   # graceful; sender drains QUIT, store flushes
# then relaunch
python internets.py
```

`graceful_shutdown` (`internets.py:527`) order: save channels -> unload all modules (each
gets `on_unload` to flush its own state) -> stop the store flush thread with a final write
-> enqueue `QUIT` at priority 0 -> sleep `_SHUTDOWN_DRAIN_S` (2.0s) for the sender to drain
-> stop sender -> close socket -> cancel background tasks -> stop the metrics server if
running -> flush logging handlers.

## Health / metrics endpoint

`metrics.py`. **Off by default** - zero network footprint until explicitly enabled. The
registry singleton accepts increments regardless, but starts no listener until
`enable()` + `expose()`.

Enable in `config.ini` / `config.local.ini` (`internets.py:1345`):

```ini
[metrics]
enable = true
host = 127.0.0.1
port = 9779
```

`_main` calls `registry.enable()` then `registry.expose(host, port)`. A failure here logs
`event=metrics_start_failed` and is non-fatal.

**Bind guard (rejects all-interfaces binds):** `expose()` (`metrics.py:256`) refuses to start unless
`enable()` was called, and **rejects any all-interfaces bind** - empty host, `0.0.0.0`,
`::`, `::0`, IPv4-mapped `::ffff:0.0.0.0`, whitespace variants - by parsing the host with
`ipaddress` and testing `is_unspecified`, raising `ValueError`. Only empty/unspecified hosts
are refused; any specific address binds, including a routable interface IP - loopback is the
intended default, not an enforced constraint; bind `127.0.0.1` and front with a reverse proxy
to expose off-host. This is an auth-less internal endpoint. The HTTP handler serves Prometheus text exposition at
`GET /metrics` only (everything else 404s) on a daemon thread; idempotent (a second
`expose` no-ops). `registry.shutdown()` stops it (joined with a 2s timeout) and is called
in `graceful_shutdown`.

Metric series are pre-registered (`_register_defaults`, `metrics.py:193`):
counters `internets_commands_total`, `internets_provider_calls_total`,
`internets_provider_quota_used`, `internets_reconnects_total`,
`internets_dropped_messages_total`, `internets_audit_records_total`; gauges
`internets_module_loaded`, `internets_provider_active`, `internets_sender_queue_depth`,
`internets_authed_admins_count`.

`.stats` (admin) also surfaces runtime counters, queue depth, memory, and audit-log record
count over IRC without any HTTP exporter.

## Persistence and backup files

All paths default to the CWD; override under `[bot]`. The store
(`store.py`) loads each JSON file into memory at startup and a background thread flushes
dirty datasets every `_FLUSH_INTERVAL` = 30s (`store.py:39`). Each dataset has its own
lock. Worst-case loss on hard crash is ~30s of user-tracking timestamps; channel and
location changes are also flushed on shutdown/restart/signal.

| File (default) | `[bot]` key | Contents | Notes |
|---|---|---|---|
| `locations.json` | `locations_file` | per-nick saved locations | written 0600 (POSIX); user-supplied data |
| `channels.json` | `channels_file` | joined channel list | restored on reconnect; saved first in shutdown |
| `users.json` | `users_file` | per-channel nick/hostmask/seen (PII) | written 0600; pruned > `user_max_age_days` (default 90) on flush |
| `shadow_bans.json` | `shadow_bans_file` | shadow-banned nicks (lowercased) | loaded at init (`internets.py:261`) |
| `steamids.json` | `[steam] steamids_file` | steam nick->ID map | module-managed |
| `internets.pid` | (fixed) | process lock | see [Process lock](#process-lock-single-instance-enforcement) |

**Atomic writes** (`store._write`, `store.py:192`): write to a `*.tmp` in the same dir,
`fdopen`+`json.dump` a v2 checksum envelope, `chmod 0600` the tmp *before* the rename (so
the final file is never momentarily world-readable), copy the current good file to
`<name>.bak` (one-deep backup), then `os.replace(tmp, target)`. `os.replace` is atomic on
POSIX, best-effort on NTFS.

**Corruption handling** (`store._read`/`_quarantine`, `store.py:150`): a file over 10 MB
(`_MAX_FILE_SIZE`), bad JSON, bad envelope checksum, or wrong top-level type is **not**
silently reset (that would let the next flush clobber the only copy). Instead it is moved
aside to `<name>.corrupt.<unixtime>` and the dataset starts from default. Recover by
inspecting the `.corrupt.*` or `.bak` files by hand.

### Audit log

`audit_log.py`. Append-only, HMAC-chained log of privileged admin actions, default
`./audit.log` (`audit_log.py:121`), each record `chmod 0600`. Every admin command records
via `admin_cmds._audit` -> `audit_log.default().record(...)`.

- **HMAC key sidecar** `audit.log.key` (`audit_log.py:123`), generated 0600 on first use
  (32 bytes, `O_WRONLY|O_CREAT|O_TRUNC, 0o600`). The chain lets you detect tampering with
  the log alone; an attacker who copies only `audit.log` (a backup) cannot forge a valid
  continuation without the key. An invalid/short existing key is backed up to
  `audit.log.key.bad` and regenerated.
- **Rotation:** at `_MAX_BYTES` = 5 MB (`audit_log.py:55`) the log renames to
  `audit.log.<timestamp>` and a fresh chain starts from genesis. Each rotated segment keeps
  its own independent chain.

**Back up together:** `audit.log`, `audit.log.key`, the rotated `audit.log.*` segments,
the JSON state files, and `config.ini` (securely - it holds secrets). The `.bak` and
`.corrupt.*` files are recovery artifacts; keep them until you've confirmed the live files
are good.

## Upgrade procedure

1. Stop the bot gracefully: `.shutdown` from IRC/console, or `kill -INT <pid>`. Confirm
   `internets.pid` is gone (or stale-cleared on next start).
2. `git pull`.
3. If `requirements.txt` / `pyproject.toml` changed, refresh the environment:
   `pip install -r requirements.txt` (or `pip install -e ".[dev]"` for the dev extras).
   Review the lockfile diff before trusting it.
4. If `config.ini.example` gained keys you need, merge them into your `config.ini` by hand
   (never copy real values into the example). Keep `config.ini` at 0600.
5. Run the standalone suite as a smoke test: `python tests/run_tests.py`.
6. Start: `python internets.py` (re-add any `--no-console` / `--debug` flags your service
   uses).

A `git pull` that only touched command modules under `modules/` can be picked up with
`.reloadall` **without** a restart - but only the command modules themselves, not helpers
(`geocode.py`, `units.py`), `config.py`, core files, or `weather_providers/`. When in
doubt, restart: `sys.modules` caching makes partial reloads silently stale (see
[Reload vs restart](#reload-vs-restart)).
