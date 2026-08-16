# botlog.py - logging setup, debug filtering, and startup validation

## Purpose

Builds the bot's entire logging stack at import time and exposes the shared
handles the rest of the code uses: `log` (the `internets` logger), `log_filter`
(the process-global `DebugFilter`), and the `apply_debug` / `apply_loglevel`
helpers shared by IRC admin commands and the interactive console. It also runs
three startup validations as import side effects: admin password-hash format,
config file permissions, and IRC mode-string syntax.

## Responsibilities / boundaries

Belongs here:

- Handler/formatter/filter construction (`_setup_logging()`).
- Log-injection sanitization (`_SafeFormatter`).
- Runtime debug controls (`DebugFilter`, `apply_debug`, `apply_loglevel`).
- Fail-closed startup validation that must happen before the bot connects.

Deliberately not here:

- Config parsing: all constants (`LOG_LEVEL`, `LOG_FILE`, `LOG_MAX`,
  `LOG_BACKUPS`, `LOG_DEBUG`, `LOG_FMT`, mode strings, `cli_args`) come from
  `config.py`.
- Password verification: `_validate_hash()` only checks the hash *format*;
  actual verification lives with `admin_cmds.py - cmd_auth()`.
- Boundary anomaly: `get_hash()`, the accessor for the admin password hash,
  lives here rather than in `config.py` (see Findings).

## Dependencies and dependents

- Imports from `config.py`: `cfg`, `CONFIG_PATH`, `__version__`, `cli_args`,
  the five `LOG_*` constants, `LOG_FMT`, and the three mode strings. Importing
  `botlog` therefore transitively triggers `config.py`'s import-time work,
  including `argparse.parse_args()` on the live `sys.argv` (tests must pin argv
  first; the module-level argv-pinning preamble in `tests/test_botlog.py` does
  exactly that).
- Dependents:
  - `internets.py` imports `log`, `log_filter`.
  - `admin_cmds.py` imports `log_filter`, `get_hash`, `apply_debug`,
    `apply_loglevel` (used by `cmd_rehash`, `cmd_auth`, `cmd_loglevel`,
    `cmd_debug`).
  - `console.py` imports `apply_debug`, `apply_loglevel`, `log_filter` for the
    stdin admin console.
  - Most modules do `logging.getLogger("internets.<name>")` and rely on the
    handler tree this module builds, without importing it.

## Lifecycle

Import order of side effects (top to bottom of the file):

1. `log_filter = _setup_logging()` - configures the `internets` logger.
2. `log.info(f"Internets v{__version__} starting")`.
3. CLI `--debug` flags applied: bare `--debug` sets `global_debug`; `--debug a
   b` namespaces each to `internets.<name>`, sets that logger to DEBUG, and adds
   it to the filter's subsystem set.
4. `_validate_hash()` - may `sys.exit(1)`.
5. World-readable check on `CONFIG_PATH` (POSIX only) - warning, never fatal
   (tagged BUG-029 in a comment).
6. Mode-string validation for `user_modes` / `oper_modes` / `oper_snomask` -
   may `sys.exit(1)`.

After import the module is passive; its functions are invoked by admin commands
and the console for the process lifetime. Nothing is torn down explicitly;
`internets.py` flushes the handlers during shutdown.

## Initialization detail: `_setup_logging()`

- Root of the tree is `logging.getLogger("internets")`, set to `DEBUG` with
  `handlers.clear()` first (safe re-run; the test suite re-invokes it).
- One `_SafeFormatter(LOG_FMT)` instance and one
  `DebugFilter(getattr(logging, LOG_LEVEL, logging.INFO))` instance are shared
  by the first two handlers. An unrecognized `LOG_LEVEL` string silently falls
  back to `INFO` via `getattr`'s default.
- Handlers:

| Handler | Sink | Level | Formatter | DebugFilter | Rotation |
|---|---|---|---|---|---|
| `RotatingFileHandler` | `LOG_FILE` | DEBUG | `_SafeFormatter` | yes | `LOG_MAX` bytes (default 5 MB), `LOG_BACKUPS` (default 3) |
| `StreamHandler` | stdout | DEBUG | `_SafeFormatter` | yes | n/a |
| `RotatingFileHandler` (only if `LOG_DEBUG` set) | `LOG_DEBUG` | DEBUG | `_SafeFormatter` | **no filter** | same `LOG_MAX` / `LOG_BACKUPS` |

- Handler levels are all DEBUG; the *effective* severity gate is the
  `DebugFilter`, not handler levels. The debug-file handler deliberately omits
  the filter so it captures everything at DEBUG regardless of the runtime
  base level, and is tagged with a `_debug_file = True` attribute for
  introspection (`tests/test_botlog.py - TestSetupLogging.test_debug_file_adds_third_handler`).

## Severity model and per-subsystem debug

`DebugFilter.filter()` passes a record if any of:

1. `record.levelno >= base_level` (normal severity gate; `base_level` seeded
   from `LOG_LEVEL`, so config `level` / CLI `--loglevel` set the floor),
2. `global_debug` is true (`.debug on`, `--debug`),
3. the record's logger name equals a registered subsystem or is a dotted child
   of one (`name == sub or name.startswith(sub + ".")` - the explicit dot
   boundary prevents `internets.weatherx` matching subsystem
   `internets.weather`;
   `tests/test_botlog.py - TestDebugFilter.test_subsystem_prefix_not_a_false_match`).

Because subsystem debug also requires the *logger* to emit DEBUG records at
all, the enable paths (`apply_debug`, `apply_loglevel`, the CLI block) both add
the subsystem to the filter and call `setLevel(logging.DEBUG)` on that logger.

`VALID_LEVELS` is the operator-facing allowlist: `DEBUG, INFO, WARNING, ERROR`
(no CRITICAL; CRITICAL records still pass the filter since they exceed any
base level).

## Formatting and log-injection protection

`_SafeFormatter` subclasses `logging.Formatter` and sanitizes user-controlled
data before interpolation:

- Strips ASCII C0 controls except TAB (so CR, LF, NUL, ESC), DEL (0x7f), and C1
  controls (0x80-0x9f, terminal CSI vectors) via `_CONTROL_RE`.
- Applies `_clean()` to `record.msg` (after `str()`) and to every element of
  `record.args` (tuple and dict forms). Non-string args pass through untouched.
- Works on a copy built with `logging.makeLogRecord(record.__dict__)` so other
  handlers see the original record
  (`tests/test_botlog.py - TestSafeFormatterFormat.test_format_does_not_mutate_original_record`).
- Exception tracebacks survive because they render via `record.exc_text`
  downstream, not through `msg`/`args`.

This defeats the classic log-forging vector (`log.info("cmd: %s",
attacker_input)` with embedded CRLF forging a fake log line, or ESC sequences
attacking a terminal viewing the log).

Scope limit, stated plainly: this is *injection* protection, not credential
scrubbing. There is no redaction hook that masks secrets in log output; the
codebase relies on call-site discipline instead (e.g.
`admin_cmds.py - cmd_auth()` never logs the password value, and
`_validate_hash()` deliberately does not echo an invalid hash because "the hash
is sensitive material"). A module that logs a secret would ship it to all three
sinks unmodified.

## Rotation

Both file handlers are `logging.handlers.RotatingFileHandler` with
`maxBytes=LOG_MAX` and `backupCount=LOG_BACKUPS` (config `[logging] max_bytes`
/ `backup_count`, defaults 5242880 / 3), UTF-8 encoded. No time-based rotation
exists.

## `get_hash()` and startup validation

`get_hash()` calls `config.reload_config()` (re-reading BOTH `config.ini` and
the `config.local.ini` overlay - re-reading the template alone would clobber an
overlay-only `password_hash` with the template's empty placeholder, per its
docstring and `tests/test_config.py - TestReloadConfig.test_second_reload_does_not_clobber_local_value`)
and returns `cfg["admin"]["password_hash"]` stripped, or `""`. Callers:
`_validate_hash()` at import, `admin_cmds.py - cmd_auth()` (fresh hash per auth
attempt, so a rehashed password takes effect without restart), and
`admin_cmds.py - cmd_rehash()`.

Placement is questionable: this is a config accessor with no logging content.
The implementation implies it lives here because `_validate_hash()` needs it at
botlog import time and `admin_cmds.py` then imported it from where it happened
to be; `config.py` is its natural home (see Findings).

`_validate_hash()` is fail-closed with one deliberate exception:

- Empty hash: warning only; the bot runs with admin auth disabled
  (documented first-run state before the operator runs `hashpw.py`).
- Non-empty hash whose prefix (the text before the first `$`, or `""` when no
  `$` exists) is not in `_VALID_HASH_PREFIXES` (`scrypt`, `bcrypt`, `argon2`):
  `log.critical` + `sys.exit(1)`. Rationale in the docstring: an unrecognized
  prefix would make `verify_password` raise on every attempt, silently
  disabling admin commands; refusing to start surfaces it immediately. The
  invalid value is never echoed. Behavior pinned by
  `tests/test_botlog.py - TestValidateHash` (including `scryptnodollar` and
  `$weird`).

The remaining import-time checks:

- BUG-029: if POSIX `os.stat(CONFIG_PATH)` shows the world-readable bit
  (`st_mode & 0o004`), warn with a `chmod 640` suggestion; `OSError` is
  swallowed (the missing-file case already failed loudly in `config.py`).
- Mode strings: `_MODE_VALID = ^[a-zA-Z+\- ]*$` applied to non-empty
  `USER_MODES` / `OPER_MODES` / `OPER_SNOMASK`; a violation is
  `log.critical` + `sys.exit(1)`. These strings are sent raw into IRC `MODE` /
  `OPER` lines by `internets.py`, so the allowlist blocks CRLF or separator
  injection through config into the IRC protocol stream.
  `tests/test_botlog.py - TestImportTimeModeGuard` drives the real exit in a
  subprocess.

## Runtime helpers

### `apply_debug(args, reply=print)`

Shared implementation of the `.debug` admin command and the console `debug`
command (`admin_cmds.py - cmd_debug()`, `console.py`). `reply` is `print` for
the console or a bound IRC reply callback.

- `[]` or `["on"]`: `global_debug = True`.
- `["off"]`: `global_debug = False` and clears all subsystems.
- `["<sub>"]`: namespaces to `internets.<sub>` unless the argument already
  starts with the literal `internets`, sets that logger to DEBUG, registers the
  subsystem.
- `["<sub>", "off"]`: sets the logger to NOTSET and deregisters.

No return value; feedback goes entirely through `reply`.

### `apply_loglevel(args, reply=print)`

Shared implementation of `.loglevel` / console `loglevel`. Returns an error
string on bad input, `None` on success (callers relay the error).

- `[]`: status report - base level, global debug flag, active subsystems,
  debug file path if configured.
- `["LEVEL"]`: validates against `VALID_LEVELS`, sets `base_level`, and clears
  `global_debug` (but not the subsystem set - see Findings).
- `["<logger>", "LEVEL"]`: target must start with `internets`; a dotless target
  is namespaced (`weather` is rejected, `internets` becomes
  `internets.internets` - characterized as odd by
  `tests/test_botlog.py - TestApplyLogLevel.test_two_args_bare_logger_namespaced`).
  `DEBUG` adds the subsystem; `NOTSET` and explicit levels remove it and set
  the logger level directly.
- Three or more args: usage string.

## State

- `log_filter`: the single process-global `DebugFilter`; its `base_level`,
  `global_debug`, and `_subsystems` set are the only mutable state this module
  owns. Mutated by `apply_debug`, `apply_loglevel`, the CLI block at import,
  and `admin_cmds.py - cmd_rehash()` (which resets base level from the reloaded
  config and clears both global debug and subsystems).
- Handler objects on the `internets` logger (owned by the stdlib logging
  registry once attached).
- Everything is in-memory; log files are the only persistent artifacts.

## Concurrency

- `DebugFilter._subsystems` is guarded by a `threading.Lock` for add / remove /
  clear / snapshot and the membership scan in `filter()`.
  `active_subsystems()` returns a copy, so callers cannot corrupt internal
  state (`tests/test_botlog.py - TestDebugFilter.test_active_subsystems_returns_copy`).
- `base_level` and `global_debug` are read in `filter()` and written without
  the lock; these are single-attribute reads/writes, benign under the GIL, and
  a momentarily stale value only mis-filters one record.
- Handler emission itself is serialized by the stdlib logging module's
  per-handler locks. Modules log from worker threads and the asyncio loop
  concurrently; that is the supported pattern.

## Failure behavior

- Fail-closed exits: invalid hash prefix, invalid mode string
  (`sys.exit(1)` each, at import).
- Fail-open: empty hash (auth disabled, warn), world-readable config (warn),
  unknown `LOG_LEVEL` name (falls back to INFO silently).
- Unhandled: `_setup_logging()` does not catch `OSError`; an unwritable
  `LOG_FILE`/`LOG_DEBUG` path aborts import with a raw traceback. This is
  fail-loud but without the actionable message the config guards provide.

## Security

- Log injection: `_SafeFormatter` (above) on all three handlers.
- IRC protocol injection via config: the mode-string allowlist.
- Secret hygiene: `_validate_hash()` and `cmd_rehash` never echo hash material;
  no general redaction layer exists (call-site responsibility).
- Filesystem: log paths come from config/CLI verbatim; the operator controls
  both, so no path validation is applied. Config permission check is advisory
  only.

## Classes

### `_SafeFormatter`

See "Formatting and log-injection protection". Stateless apart from the
compiled class-level regex; safe to share across handlers.

### `DebugFilter`

See "Severity model". Constructed once per `_setup_logging()` call; the module
instance is `log_filter`. Attached to the main file handler and the console
handler, deliberately not to the debug-file handler. Invariant: a subsystem in
`_subsystems` should also have its logger at DEBUG, maintained by the enable
paths, not enforced by the class.

## Implementation walk

- Lines 1-23: docstring and imports; the `config` import triggers config
  parsing and argv parsing as a prerequisite.
- Lines 28-61: `_SafeFormatter` (sanitization).
- Lines 64-107: `DebugFilter` (severity + subsystem gate).
- Lines 112-142: `_setup_logging()` (handler tree).
- Lines 145-159: module initialization: build the stack, announce the version,
  apply CLI `--debug`.
- Lines 164-210: `get_hash()`, `_VALID_HASH_PREFIXES`, `_validate_hash()`, and
  its invocation.
- Lines 212-229: config permission warning and mode-string validation loop
  (validation + security enforcement).
- Lines 234-303: `VALID_LEVELS`, `apply_debug()`, `apply_loglevel()`.

## Findings

- **questionable** | `botlog.py - get_hash()` | A config accessor (admin
  password hash) lives in the logging module; `admin_cmds.py` imports it from
  here. It exists here to serve `_validate_hash()` at import time, but its
  natural home is `config.py` next to `reload_config()`, which it wraps.
- **questionable** | `botlog.py - apply_debug()` / `apply_loglevel()` |
  Inconsistent namespacing rules: `apply_debug` prefixes unless the argument
  `startswith("internets")` (so a logger literally named `internetsfoo` cannot
  be addressed), while `apply_loglevel` requires the prefix and then keys on
  `"." in target`, producing `internets.internets` for the bare root name (test
  characterizes this rather than endorsing it).
- **questionable** | `botlog.py - apply_loglevel()` | Setting a base level
  clears `global_debug` but leaves per-subsystem debug sets active, whereas
  `admin_cmds.py - cmd_rehash()` clears both; an operator raising the level to
  WARNING still receives DEBUG from any previously enabled subsystem.
- **questionable** | `botlog.py - _setup_logging()` | An unrecognized
  `[logging] level` value degrades silently to INFO via
  `getattr(logging, LOG_LEVEL, logging.INFO)`; the config guards elsewhere fail
  loudly on bad values, this one does not even warn.
- **test-gap** | `botlog.py` import block (lines 150-159) | The CLI `--debug`
  application at import (global and per-subsystem branches) has no direct test;
  `tests/test_config.py - TestCliArgs` pins only the parsed `cli_args` values,
  not the filter/logger effects of this block.
