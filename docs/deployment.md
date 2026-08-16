# Deployment

How to get Internets onto a host and keep it running there: installation shapes,
platform support, the deployment directory and its permissions, running under a
service manager, and the upgrade and rollback path.

This is not the runbook. Day-to-day operation - starting and stopping, restart
versus rehash versus reload, log review, audit review, health checking, the
metrics endpoint, backup mechanics, the maintenance checklist - is
[operations.md](operations.md), and it is linked from here rather than repeated.
First-time bring-up from a clone is [getting-started.md](getting-started.md).
Key-by-key config meaning is [configuration.md](configuration.md).

| Subsystem this page touches | Internals reference |
|---|---|
| Entry point, module loading, restart | [internals/internets.md](internals/internets.md) |
| Config and CLI parsing | [internals/config.md](internals/config.md) |
| Secret resolution and file mode gate | [internals/secret_store.md](internals/secret_store.md) |
| Single-instance lock | [internals/process_lock.md](internals/process_lock.md) |
| Logging stack and startup validation | [internals/botlog.md](internals/botlog.md) |
| Packaging, wheel contents, CI | [internals/ci-and-packaging.md](internals/ci-and-packaging.md) |

## What a deployment is

One long-lived Python process, one IRC network, one host. There is no
supervisor tree, no worker pool, no database, and no network listener except the
optional metrics exporter. Everything the process owns lives as files in a single
directory: the config, the process lock, four JSON state files, the module state
files, the audit log with its key sidecar, and the rotating bot log.

That directory is the unit of deployment. Back it up, move it, or restore it and
you have moved the bot.

(deploy-install)=
## Installation shapes

Two shapes are supported, and they differ in more than convenience.

### From a checkout (the primary path)

```bash
git clone https://github.com/brandontroidl/Internets
cd Internets
pip install -r requirements.txt
python internets.py
```

The checkout is both the code and the deployment directory. `modules/`,
`weather_providers/`, and `config.ini.example` are all present at the paths the
code expects, so nothing needs pointing at anything.

`requirements.txt` installs the full runtime stack and carries the security
floors, each annotated with the advisory it closes. `pyproject.toml` splits the
same set: `requests` alone is unconditional under `[project] dependencies`,
while `aiohttp`, `bcrypt`, `argon2-cffi`, `PyJWT`, `cryptography`, and
`defusedxml` are extras. That split is deliberate - each of the six enables a
feature and degrades to unavailable rather than failing the import - so a
minimal install is possible, and the floors must be kept identical in both files
or an extras install can resolve a version the requirements file calls unsafe.

### From a built wheel or sdist (console script)

`pyproject.toml` declares `[project.scripts] internets = "internets:_entry"`, so
a package install puts an `internets` command on `PATH`. Build and verify with
`scripts/verify_install.sh`, which builds both artifacts, installs the wheel into
a throw-away venv, hash-checks every installed file against the wheel's `RECORD`,
and confirms all thirteen declared top-level modules import.

```bash
./scripts/verify_install.sh
pip install dist/internets_irc-5.0.0-py3-none-any.whl
```

Two gaps make this shape more work than it looks, both verified against the built
artifacts in `dist/`:

- **The template is not packaged.** Neither the wheel nor the sdist contains
  `config.ini.example` (nor `requirements.txt` or `CHANGELOG.md`); only the
  thirteen top-level modules listed in `pyproject.toml`
  `[tool.setuptools] py-modules` plus the `modules` and `weather_providers`
  packages ship. `secret_store.py - _cmd_init()` copies `config.ini.example`
  from the current directory and exits 2 with "re-clone the repo" when it is
  absent, so `python -m secret_store init` cannot bootstrap a package-only
  install. Copy the template out of the source tree by hand.
- **Modules are loaded by file path, not by import.**
  `internets.py - IRCBot.load_module()` builds each module from
  `MODULES_DIR / f"{name}.py"`, and `config.py - MODULES_DIR` defaults to the
  relative path `modules`, resolved against the working directory. A wheel
  install puts the `modules` package in `site-packages`, where that default does
  not reach it, and every autoload entry fails with `'modules/<name>.py' not
  found.` Point `[bot] modules_dir` at the installed package directory:

  ```ini
  [bot]
  modules_dir = /srv/internets/venv/lib/python3.12/site-packages/modules
  ```

  Verified working: loading from that directory succeeds because the loader
  names the module `modules.<name>`, and the `modules` package is importable
  from `site-packages`, so the relative imports inside each module resolve.

The implication is that a package install still needs a deployment directory
holding `config.ini` and the state files, and still needs one absolute path in
the config. It gains a versioned, hash-verifiable artifact and loses the
self-contained directory.

### Editable install for development

`pip install -e ".[dev]"` adds pytest, coverage, bandit, pip-audit, and build on
top of the checkout, and keeps the checkout as the source of truth for both code
and paths. Use it for development, not for a production host.

## Platform support

The claim carried in `README.md` and [index.md](index.md) is Linux, macOS,
FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, and MSYS2, on Python 3.10 through
3.14. Distinguish claim from coverage before you rely on it:

| Platform | Status |
|---|---|
| Linux, macOS, Windows | Tested in CI on Python 3.10-3.14 (`.github/workflows/tests.yml` matrix) |
| FreeBSD, WSL, Cygwin, MinGW, MSYS2 | Claimed, no CI leg; POSIX-shaped platforms exercise the same code paths as Linux |

Note that the Tests workflow is currently red on `main` - see
[Known defect: dependency lockfile](#deploy-defect-lockfile) - so "tested in CI"
describes the matrix, not a passing run today.

Platform-dependent behavior, all of it in three places:

- **POSIX file modes.** `secret_store.py - perms_ok()` returns
  `(True, "windows (acl-based)")` on `os.name == "nt"` and enforces an exact
  0600 elsewhere. `audit_log.py` and `store.py - Store._write()` likewise skip
  `chmod` on Windows. A Windows deployment gets no permission enforcement from
  the bot; ACLs are yours to set.
- **Signals.** `internets.py - IRCBot.run()` installs handlers with
  `loop.add_signal_handler` for SIGINT, SIGTERM, and SIGHUP, which exists only
  on POSIX. Windows has no SIGHUP, so `.rehash` over IRC is the only rehash
  route there, and shutdown arrives as `KeyboardInterrupt`.
- **Restart.** `internets.py - _main()` re-execs with `os.execv` on POSIX and
  falls back to `subprocess.Popen` plus `sys.exit(0)` on Windows, which
  detaches the new process from the old one's supervisor.
- **Stale lock liveness.** `process_lock.py - _pid_is_alive()` probes with
  `os.kill(pid, 0)` on POSIX and tries `psutil` on Windows, failing open with a
  warning when `psutil` is absent.

The drop-root guard is also POSIX-only; see below.

(deploy-workdir)=
## The deployment directory and why it matters

Every path the process uses is relative to the working directory at the moment
it is resolved. Nothing is derived from the location of the source file.

| Path | Resolved by | When |
|---|---|---|
| `config.ini` | `config.py - CONFIG_PATH` | import time |
| `config.local.ini` | `config.py - reload_config()` | import time and every rehash |
| `config.ini` as secret store | `secret_store.py - SECRETS_FILE` | import time |
| `modules/` | `config.py - MODULES_DIR` | import time, per module load |
| `internets.pid` | `internets.py - _entry()` (`Path("./internets.pid").resolve()`) | at startup |
| `locations.json`, `channels.json`, `users.json`, `shadow_bans.json` | `[bot]` keys, default bare filenames | at startup |
| `audit.log`, `audit.log.key` | `audit_log.py - AuditLog` defaults | first record |
| the bot log | `[logging] log_file` | import time |

Consequences that bite in practice:

- **A service unit must set `WorkingDirectory`.** Without it the manager's
  default directory (`/` for a system unit) becomes the deployment directory,
  `config.ini` is not found, and `config.py` raises `SystemExit` before the loop
  starts. The failure is loud, which is the good case; the bad case is a unit
  whose working directory points at a *different* checkout, where the bot starts
  cleanly against another config and another state set.
- **Interactive administration must `cd` first.** `python -m secret_store status`
  run from your home directory reports on a `config.ini` that is not the one the
  bot reads.
- **A relative `modules_dir` follows the working directory too.** Use an
  absolute path for any package install.

`internets.py - _entry()` resolves the lock path to an absolute path at startup,
while `process_lock.py - ProcessLock._resolved_path()` resolves a relative path
at acquire time rather than at construction. Both land on the startup working
directory.

## File layout and permissions

A running deployment directory, with the mode each file actually gets:

| File | Mode | Set by | Holds |
|---|---|---|---|
| `config.ini` | 0600 required | operator (`secret_store.py - _cmd_init()` creates it 0600) | settings and `[secrets]` |
| `config.local.ini` | 0600 by convention | operator | overlay, typically `password_hash` |
| `audit.log`, `audit.log.key` | 0600 | `audit_log.py` | admin action trail, HMAC key |
| `locations.json`, `channels.json`, `users.json`, `shadow_bans.json` | 0600 | `store.py - Store._write()` chmods the temp file before the rename | user data, PII in `users.json` |
| `*.json.bak` | umask default | `store.py - Store._write()` (no chmod) | previous good copy |
| `*.json.corrupt.*` | mode of the original | `store.py - Store._quarantine()` | quarantined file |
| the bot log and its rotations | umask default | `logging.handlers.RotatingFileHandler` | operational log |
| `internets.pid` | 0644 | `process_lock.py - ProcessLock.acquire()` (`O_CREAT \| O_EXCL`, 0o644) | `pid\|start_time\|hostname` |

Set the directory itself to 0700 owned by the bot user. That is the only control
covering the three rows whose modes the bot does not manage.

:::{warning}
**Known defect (self-contradicting permission advice).** `botlog.py` warns at
startup, when `config.ini` is world-readable, with the literal text
`config.ini is world-readable - consider: chmod 640 config.ini`. Following it
breaks the deployment: `secret_store.py - perms_ok()` tests `mode != 0o600` and
fails closed, so at 0640 the `[secrets]` section is never read, `secret_store.get()`
returns the default for every name, and the bot runs with no NickServ password
and no API keys behind a single `REFUSING to read` error line. The equality test
also rejects *stricter* modes: 0400 fails the same way. Use `chmod 600`. Verified;
recorded in [known-issues.md](known-issues.md).
:::

:::{warning}
**Known defect (backup permissions).** `store.py - Store._write()` copies the
previous good file to `<name>.bak` with `Path.write_bytes` and never chmods it,
so on first creation it takes umask-default permissions, typically 0644, while
the live file is 0600. The PII in `users.json` is world-readable in
`users.json.bak`. Until it is fixed, a 0700 deployment directory is the
containment, and `chmod 600 *.bak` belongs in the maintenance checklist. See
[operations.md](operations.md#state-files-and-backup).
:::

:::{warning}
**Known defect (log file permissions).** The bot log gets umask-default
permissions with no check and no warning, unlike `config.ini`. It is not an
empty file from a privacy standpoint: `location.py - LocationModule.cmd_regloc()`
logs nick-to-location pairs and `linktitle.py - LinkTitleModule` logs announced
URLs with their channel, and `.forgetme` cannot reach either. Treat the log as
user-data-bearing: keep it inside the 0700 directory, and set the bot user's
umask to 077 in the service unit.
:::

## Running as an unprivileged user

`internets.py - _entry()` refuses to start when `os.geteuid() == 0` unless
`INTERNETS_ALLOW_ROOT=1` is set in the environment. Refusal logs
`event=refused_root_start` and exits 1; the override logs
`event=root_start_allowed` at WARNING on every start, deliberately, so an
overridden deployment cannot become quiet.

Create a dedicated account. There is no reason to hold the override: the only
scenario the guard's own message names is binding a port below 1024, which the
bot never does - it makes an outbound IRC connection and, when metrics are
enabled, binds a port you choose (default 9779). If you truly need a privileged
port for something adjacent, `setcap CAP_NET_BIND_SERVICE` on the interpreter is
the narrower tool.

The guard is POSIX-only. A container running as root inside its namespace still
trips it, which is intended: set `INTERNETS_ALLOW_ROOT=1` explicitly in the
container environment, or better, add a `USER` line to the image.

## Running as a service

### systemd

```ini
[Unit]
Description=Internets IRC bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=internets
Group=internets
WorkingDirectory=/srv/internets
UMask=0077
ExecStart=/srv/internets/venv/bin/python internets.py --no-console
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/internets

[Install]
WantedBy=multi-user.target
```

Every line above that is not boilerplate is load-bearing:

- `WorkingDirectory` is mandatory, per
  [The deployment directory](#deploy-workdir).
- `--no-console` is the daemonization requirement. `console.py - should_skip_console()`
  already returns True whenever `sys.stdin.isatty()` is False, which covers a
  normal unit (systemd gives it `/dev/null`), so the flag is redundant in the
  common case and cheap insurance in the rest: a unit with
  `StandardInput=tty`, a `docker run -it`, or a hand-run `systemd-run --pty`
  hands the console to whoever holds that terminal. The console is
  **admin-equivalent with no authentication** - `shutdown`, `debug`, `loglevel`,
  `status` - which is why `internets.py - _main()` logs `event=console_active` at
  WARNING when it starts. Pass the flag and treat an unexplained
  `event=console_active` in a service log as an incident.
- `ExecReload` maps `systemctl reload` onto SIGHUP, which
  `internets.py - IRCBot._on_sighup()` handles as a config rehash without
  dropping the IRC link. It is not a restart; see
  [operations.md](operations.md#restart-rehash-reload-which-one) for which changes need which.
- `KillSignal=SIGTERM` (the default) reaches
  `internets.py - IRCBot._on_signal()`, which requests a graceful shutdown.
  `TimeoutStopSec` must exceed the shutdown sequence, whose fixed floor is the
  2 s sender drain in `internets.py - IRCBot.graceful_shutdown()`.
- `Restart=on-failure` with a non-trivial `RestartSec`. Exit 1 covers both a
  held process lock and a rejected config, and both are permanent until a human
  intervenes; a one-second restart interval turns either into a hot loop.
- `UMask=0077` is what protects the three files the bot writes without a chmod
  (the `.bak` copies and the log), per the defects above.

`Type=simple` is correct even though the bot re-execs itself on `.restart`:
`os.execv` preserves the PID, so systemd's `MainPID` stays valid and the restart
is invisible to the manager. That is also why the lock must be released before
the exec, and it is handled - see [Process lock](#deploy-lock).

### Other managers and containers

The requirements generalize to any supervisor: unprivileged user, working
directory set, stdin off a TTY, SIGTERM to stop, enough stop timeout for the
shutdown sequence. Two constraints are easy to get wrong:

- **Do not treat `internets.pid` as a supervisor PID file.** It is a
  mutual-exclusion lock; `process_lock.py - ProcessLock.release()` deletes it on
  a clean exit, and a supervisor that recreates or restores it blocks the next
  start.
- **A container's hostname must be stable.**
  `process_lock.py - ProcessLock.acquire()` refuses a lockfile written by a
  different hostname, because it cannot probe a foreign PID. A container whose
  hostname changes per start therefore refuses to start on any lockfile a crash
  left behind, and needs it removed by hand.

The repository ships no container image. If you build one, mount the deployment
directory as a volume, set `WORKDIR` to it, run as a non-root `USER`, and do not
allocate a TTY.

(deploy-lock)=
## Process lock and the restart path

`process_lock.py` exists to stop two instances from interleaving writes into the
same JSON state files, whose temp-and-rename writes would otherwise clobber each
other. Acquire-time behavior and stuck-lock recovery are in
[operations.md](operations.md#process-lock); what matters at deployment time
is the interaction with re-exec.

`os.execv` preserves the process ID. If the lockfile survived the exec, the new
image would find its own PID in it, probe it as alive, and refuse to start -
a self-deadlock on every restart. `internets.py - _main()` therefore closes the
log handlers, calls `ProcessLock.release()`, and only then re-execs. A failure to
release logs `event=restart_lock_release_failed` and the exec proceeds anyway,
which produces exactly that deadlock on the next boot; if a `.restart` never
comes back, look for that event and delete `internets.pid`.

Two deployment-shaped hazards follow:

- **Shared filesystems.** Two hosts mounting the same deployment directory over
  NFS or a shared Docker volume cannot probe each other's PIDs, so the lock
  degrades to "refuse anything from another hostname". That is the safe
  direction, but it means a failover needs manual lock removal. Do not run two
  hosts against one directory.
- **PID reuse after a hard kill.** Stale detection clears a lockfile whose PID
  is dead on this host. It cannot tell a reused PID from the original - the
  recorded `start_time` is the lock acquisition time, not the OS process start
  time, and nothing consults it - so a reused PID produces a refusal that only a
  human can clear.

:::{warning}
**Known defect (concurrency window).** The stale-reclaim path in
`process_lock.py - ProcessLock.acquire()` is examine, unlink, then exclusive
create, which is not atomic. Two processes starting simultaneously over the same
stale lockfile can interleave and both acquire. The window is microseconds and
only reachable after a crash left a stale file, but a service manager restarting
a crashed unit is exactly the situation that produces one. Recorded in
[internals/process_lock.md](internals/process_lock.md#findings).
:::

## Environment variables

The environment is a first-class configuration surface, and for secrets it is the
highest-priority one.

| Variable | Read by | Effect |
|---|---|---|
| `INTERNETS_<NAME>` | `secret_store.py - get()` | Supplies any secret; wins over `config.ini[secrets]` |
| `INTERNETS_ALLOW_ROOT` | `internets.py - _entry()` | `1` permits starting as root |
| `INTERNETS_ALLOW_TLS12` | `internets.py - IRCBot._connect()` | `1` lowers the TLS floor from 1.3 to 1.2 and logs a warning |
| `INTERNETS_ARGON2_MEM_MIB`, `INTERNETS_ARGON2_TIME`, `INTERNETS_BCRYPT_ROUNDS` | `hashpw.py` | Hash cost parameters at generation time only |

Environment secrets suit containers and any host whose config file is templated
by a deployment tool: nothing lands on disk, and `secret_store.py - get()`
applies the same blank-and-placeholder filtering to the environment tier as to
the file tier, so an exported `changeme` is not treated as a value. The cost is
that `secret_store.py - status()` can enforce a file mode but nothing equivalent
on a process environment.

:::{warning}
**Known defect (invisible secret).** `nasa_api_key`, read by `modules/apod.py`
and `modules/astro2.py`, is registered in neither `secret_store.py -
KNOWN_SECRETS` nor `CONFIG_LOCATIONS`. It works through `get()` and through
`INTERNETS_NASA_API_KEY`, but it does not appear in `secret_store list` or
`status`, and `migrate` will not relocate it. Recorded in
[known-issues.md](known-issues.md).
:::

## Upgrade

The procedure itself, step by step with its verification points, is
[operations.md](operations.md#upgrade-procedure). Deployment-level notes:

- Read the CHANGELOG entry for the target version before anything else. A major
  release can require operator action before the bot will authenticate you again:
  5.0.0 began rejecting a bcrypt password longer than 72 UTF-8 bytes, and the
  only symptom is `.auth` answering `wrong password.`
- Upgrade the deployment directory, not around it. A checkout deployment upgrades
  with `git pull` in place; a package deployment installs the new wheel into the
  venv and leaves the directory untouched, but then must re-point
  `[bot] modules_dir` if the Python version in the venv path changed.
- `config.ini.example` gaining keys is not automatic. Merge new keys into your
  `config.ini` by hand and never copy real values into the template. Note that
  the template is incomplete in both directions: it omits `[tell]`, `[notes]`,
  `[remind]`, `[seen]`, and `[bot] shadow_bans_file` though the code reads them,
  and omits whole sections (`[imdb]`, `[lastfm]`, `[youtube]`, `[stocks]`,
  `[twitch]`, `[search]`, `[ipintel]`, `[satpass]`, `[apod]`) whose per-module
  ini fallbacks are therefore unreachable on a fresh install. See
  [configuration.md](configuration.md).
- Only command modules under `modules/` can be picked up without a restart, via
  `.reloadall`. Helpers, providers, `config.py`, and core files need a full
  restart. When in doubt, restart.

:::{warning}
**Known defect (privacy template).** The `autoload` list in `config.ini.example`
enables 67 modules including `seen`, `tell`, `linktitle`, `notes`, `remind`, and
`steam` - all of which record user-derived data - but does **not** include
`privacy`. A deployment that uses the shipped template verbatim tracks users
while shipping no `.forgetme`, `.optout`, `.optin`, or `.privacy` command: the
right-to-erasure entry point is absent by default. Add `privacy` (and `health`)
to your `autoload` before going live. Verified; recorded in
[known-issues.md](known-issues.md).
:::

(deploy-defect-lockfile)=
### Known defect: the dependency lockfile

:::{warning}
`requirements.lock` was generated on
Python 3.14, violating the resolve-on-3.10 contract stated in
`scripts/regen-lockfile.sh`, and consequently omits marker-gated transitives
(`typing_extensions>=4.4`, pulled by `aiohttp`). Any `--require-hashes` install
from the lock on Python 3.10 through 3.12 fails, and the Tests workflow has been
red on `main` since 2026-08-13. Install from `requirements.txt`, or regenerate
the lock per the script, until this is fixed. Recorded in
[internals/ci-and-packaging.md](internals/ci-and-packaging.md#findings).
:::

## Rollback

Rolling back is stopping the bot, restoring the previous code, and starting it
again. The code is stateless between versions; the state files are not, and they
are what to think about.

1. Stop gracefully and confirm `internets.pid` is gone.
2. Copy the whole deployment directory aside before touching anything. This is
   the rollback's only recovery path, and it is cheap.
3. Restore the previous code: `git checkout <previous-tag>` for a checkout, or
   reinstall the previous wheel into the venv for a package install. Reinstall
   dependencies if `requirements.txt` moved between the two versions - a
   downgrade that leaves newer libraries in place is not the version you tested.
4. Start, and check the log for `Store: <path> unusable` lines before assuming
   the state survived.

The state-file format itself is not the hazard: `store.py - Store._read()` has
accepted both the v2 checksum envelope and a legacy bare payload since the
initial commit. The hazard is the *handling* of a rejected file. Quarantine
arrived in 5.0.0 (CHANGELOG, "Store quarantine instead of clobber"); before it,
`Store._read()` reset to empty on a checksum, size, shape, or parse failure and
the next flush overwrote the only copy. So a rollback below 5.0.0 turns one bad
read into permanent loss of locations, channel state, and opt-out flags -
restore the state files from the copy taken in step 2 rather than letting an
older binary rewrite them.

The audit log does not roll back. `audit_log.py - AuditLog.record()` appends to
one chain that `AuditLog.verify()` walks from genesis, with no version-scoped
segment boundary, so a rollback simply continues the existing chain.

## Backing up a deployment

Full mechanics, including what has to be captured together and the 30-second
flush window, are in [operations.md](operations.md#what-to-back-up). The
deployment-level summary: the whole directory is the backup unit, `config.ini`
inside it holds every secret so the backup inherits that sensitivity,
`audit.log` and `audit.log.key` must travel together or neither is verifiable,
and `internets.pid` must **not** be restored - a restored lockfile refuses the
next start.

A backup you have never restored is not a verified recovery path. Restore into a
scratch directory, start the bot with `--no-console` against a test nick, and
confirm it reaches the channel list you expect, at least once per quarter.

## Deployment checklist

- [ ] Dedicated unprivileged user; `INTERNETS_ALLOW_ROOT` unset.
- [ ] Deployment directory 0700, owned by that user, and named in
      `WorkingDirectory`.
- [ ] `config.ini` mode exactly 0600. Not 0640, whatever the startup warning says.
- [ ] `modules_dir` absolute and correct for the install shape.
- [ ] `autoload` includes `privacy` and `health`.
- [ ] Service unit passes `--no-console`, sets `UMask=0077`, and has a
      `RestartSec` that will not hot-loop.
- [ ] `python -m secret_store status` run *from the deployment directory* reports
      the file readable and the expected names resolving.
- [ ] Metrics either disabled or bound to loopback behind an authenticating proxy;
      the exporter has no authentication of its own
      ([operations.md](operations.md#metrics-endpoint)).
- [ ] Backup configured, and one restore rehearsed.
- [ ] `.health`, `.audit verify`, and `.stats` all answer after the first start
      ([operations.md](operations.md#health-checking)).
