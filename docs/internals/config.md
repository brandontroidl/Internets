# config.py - config.ini parsing, CLI arguments, and import-time constants

## Purpose

Loads the bot's layered configuration (`config.ini` template plus an optional
gitignored `config.local.ini` overlay), parses the process command line, and
exposes the result as module-level constants consumed by the core, the modules,
and `botlog.py`. It is the single owner of the live `configparser` object
(`cfg`) and of `reload_config()`, the only sanctioned reload path.

All of this happens at import time: importing `config` reads files from disk,
resolves secrets, parses `sys.argv`, and can terminate the process.

## Responsibilities / boundaries

Belongs here:

- File discovery, layered read order, encoding policy, and the reload function.
- The secret-vs-config resolution helper (`_secret_or_cfg`).
- CLI argument definition and parsing (`cli_args`).
- Import-time validation of a small set of values (file presence, non-empty
  command prefix, cooldown floors).

Deliberately not here:

- Secret storage itself: `secret_store.py` owns the
  `INTERNETS_*` env var tier and the `config.ini [secrets]` file tier
  (with permission checks); `config.py` only consults it.
- Logging setup and the deeper validation of hash and mode strings:
  `botlog.py`, which imports this module's constants.
- Live re-reading of values after rehash: consumers that want rehash-visible
  values must read `cfg` at use time (see "reload semantics").

## Dependencies and dependents

- Imports: stdlib `argparse`, `configparser`, `pathlib`; project
  `secret_store`.
- Dependents: `botlog.py` (constants + `cfg` + `cli_args` + `reload_config` via
  `get_hash`), `internets.py` (connection constants, `cfg`, `reload_config` on
  SIGHUP), `admin_cmds.py` (`cfg`, `reload_config` in `cmd_rehash`,
  `CMD_PREFIX`), `console.py` and modules (various constants, `cfg`),
  `tests/test_config.py`.

## Lifecycle

Import-time sequence (the whole file is the initializer):

1. Build `cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))`
   so `;` and `#` start inline comments.
2. Resolve `CONFIG_PATH = str(Path("config.ini").resolve())` and
   `_LOCAL_CONFIG = Path("config.local.ini").resolve()` - both relative to the
   process working directory at import (see Findings).
3. `read_files = reload_config()` - first read of both layers.
4. Fail-closed guard: empty `read_files` raises `SystemExit` with an
   actionable message (missing/unreadable `config.ini`), instead of the bare
   `KeyError: 'irc'` the next line would produce.
5. Parse `[irc]`, `[bot]`, `[logging]` values into constants, resolving
   credentials through `_secret_or_cfg`.
6. Define the argparse CLI and call `_cli.parse_args()` on live `sys.argv`.
7. Compute the `LOG_*` constants, letting CLI flags override config.

There is no teardown; the module lives for the process. `reload_config()` may
be called any number of times afterwards.

## The parse path: config.ini + config.local.ini overlay

`reload_config()` is the canonical reader:

```python
files = cfg.read(CONFIG_PATH, encoding="utf-8")
if _LOCAL_CONFIG.exists():
    files += cfg.read(str(_LOCAL_CONFIG), encoding="utf-8")
return files
```

- Order matters: the committed template is read first, then the overlay, so
  overlay keys win on conflict while template-only keys survive
  (`tests/test_config.py - TestReloadConfig.test_local_overlay_overrides_and_preserves`).
- `configparser.read()` merges into the existing parser rather than replacing
  it: only keys present in the file being read are overwritten. That is exactly
  why every reload must read BOTH files: re-reading `config.ini` alone would
  overwrite an overlay-only value (e.g. `password_hash`) with the template's
  empty placeholder. The docstring documents this hazard and
  `tests/test_config.py - TestReloadConfig.test_second_reload_does_not_clobber_local_value`
  pins it.
- Reads are pinned to UTF-8 because `config.ini.example` uses box-drawing
  characters in comment banners and `configparser.read()` otherwise falls back
  to the platform locale (cp1252 on Windows would raise `UnicodeDecodeError`).
- Returns the list of files actually read, for caller logging. A missing
  overlay is normal (template-only deployments).

All reload paths route through this function (verified): import-time (line 67),
`internets.py - _on_sighup()` (SIGHUP), `admin_cmds.py - cmd_rehash()`, and
`botlog.py - get_hash()`.

## reload_config() semantics: live vs frozen

`reload_config()` refreshes the *contents of the `cfg` mapping* only. Every
module-level constant computed at import stays frozen until process restart
(or `os.execv` restart via `.restart`). Consumers split accordingly:

- Live after rehash: anything read through `cfg[...]` at use time. Example:
  `internets.py - IRCBot._cmd_prefix()` reads
  `cfg["bot"].get("command_prefix", CMD_PREFIX)` per dispatch precisely so a
  prefix change takes effect on rehash; `admin_cmds.py - cmd_rehash()` re-reads
  `cfg["logging"]["level"]`; `botlog.py - get_hash()` re-reads
  `cfg["admin"]["password_hash"]`.
- Frozen at import (the full list of module-level constants):

| Constant | Source | Notes |
|---|---|---|
| `SERVER`, `PORT`, `NICKNAME`, `REALNAME` | `[irc]` | `PORT` via `int()` |
| `NS_PW`, `SERVER_PW`, `OPER_PW` | `_secret_or_cfg` | credentials; `internets.py - _on_sighup()` explicitly documents that rehash does NOT refresh these |
| `OPER_N`, `USER_MODES`, `OPER_MODES`, `OPER_SNOMASK` | `[irc]`, stripped | validated later by `botlog.py` |
| `CMD_PREFIX` | `[bot] command_prefix` | frozen; live reads go through `_cmd_prefix()` |
| `API_CD`, `FLOOD_CD` | `[bot]`, `max(1, int(...))` | floored at 1 s so config cannot disable rate limiting (defense in depth with `RateLimiter`) |
| `MODULES_DIR` | `[bot] modules_dir` | a `Path`, default `modules` |
| `AUTO_LOAD` | `[bot] autoload` | comma-split, stripped, empties dropped; a rehash does not change autoload (restart required) |
| `DESIRED_CAPS` | hardcoded set | IRCv3 caps to request; all optional |
| `LOG_LEVEL` | CLI `--loglevel` else `[logging] level`, uppercased | |
| `LOG_FILE`, `LOG_MAX`, `LOG_BACKUPS` | `[logging]` | defaults 5242880 / 3 |
| `LOG_DEBUG` | CLI `--debug-file` else `[logging] debug_file` | empty string means disabled |
| `LOG_FMT` | hardcoded format string | |
| `cli_args` | argparse | never re-parsed |
| `CONFIG_PATH`, `read_files`, `__version__` | - | `__version__ = "5.0.0"`, must match `pyproject.toml` (enforced by `tests/test_config.py - TestVersion.test_version_matches_pyproject`) |

Import-time validation is also frozen: the non-empty-prefix guard and the
cooldown floors run once and are not re-applied by `reload_config()` (see
Findings).

## Secret resolution: `_secret_or_cfg()`

```python
_secret_or_cfg(secret_name, section, key, default="")
```

Three tiers, first hit wins:

1. `secret_store.get(secret_name)` - itself tiered:
   `INTERNETS_<NAME>` env var, then `config.ini [secrets]` (refused if the
   file's permissions are unsafe), with blank/placeholder filtering. A tier-1
   value is returned verbatim, not stripped (`secret_store` owns its own
   normalization; `tests/test_config.py - TestSecretOrCfg.test_secret_value_not_stripped`).
2. `cfg[section][key]`, stripped - the legacy fallback for pre-migration
   configs (`python -m secret_store migrate` moves plaintext out).
3. `default` (empty string unless overridden).

Used for `nickserv_password`, `server_password`, `oper_password` only; all
other keys read `cfg` directly.

## CLI: argparse at import time

The parser is built and **`parse_args()` runs at import** (`cli_args =
_cli.parse_args()`, line 138). Flags:

| Flag | Effect |
|---|---|
| `--version` | print `Internets 5.0.0` and exit (argparse action, exits during import) |
| `--debug [SUBSYSTEM ...]` | `None` when absent, `[]` for global debug, names for per-subsystem; consumed by `botlog.py` at its import |
| `--loglevel LEVEL` | overrides `[logging] level` for `LOG_LEVEL` |
| `--debug-file PATH` | overrides `[logging] debug_file` for `LOG_DEBUG` |
| `--no-console` | disables the stdin admin console; `internets.py` also requires stdin to be a TTY |

Consequences of parsing at import:

- Any process importing `config` (directly, or transitively via `botlog`,
  `internets`, `admin_cmds`, most modules) has its `sys.argv` interpreted by
  this parser. Unknown arguments cause argparse to print usage and
  `SystemExit(2)` *during import*; `--version`/`--help` exit successfully
  during import.
- **The test trap (document-and-pin):** pytest's own argv (`-q`, test paths)
  makes the import abort. Every test module importing this chain must pin
  argv first and restore it after, e.g.:

  ```python
  _SAVED_ARGV = sys.argv
  sys.argv = ["internets"]
  import config
  sys.argv = _SAVED_ARGV
  ```

  This is exactly what the module-level argv-pinning preamble in
  `tests/test_config.py` and `tests/test_botlog.py` does, and what
  `tests/test_config.py`'s
  `reimport` fixture re-does around every `importlib.reload(config)`. Any new
  test file that imports the bot's module graph must copy the pattern.
- Non-test embedders (scripts importing bot modules) inherit the same
  behavior: their own CLI flags will be rejected by this parser unless argv is
  pinned.

## Validation and failure behavior for missing/malformed config

Fail-closed with actionable messages:

- Missing/unreadable `config.ini`: `SystemExit` naming `CONFIG_PATH` and
  pointing at `python -m secret_store init`
  (`tests/test_config.py - TestGuards.test_missing_config_raises_systemexit`).
- Empty `command_prefix`: `SystemExit` explaining that an empty prefix would
  make every message a command
  (`TestGuards.test_empty_command_prefix_raises_systemexit`).

Silently corrected:

- `api_cooldown` / `flood_cooldown` of 0 or negative: floored to 1
  (`TestReimportParsing.test_cooldowns_floored_at_one`).

Fail-loud but *not* actionable (raw traceback at import):

- A present `config.ini` missing a required section or key: bare
  `KeyError` (e.g. `KeyError: 'irc'`) - only the missing-file case gets the
  friendly message.
- Malformed numerics (`port`, `api_cooldown`, `max_bytes`, `backup_count`):
  `ValueError` from `int()`.
- Syntactically invalid INI: `configparser.Error`.

Downstream, `botlog.py` adds the hash-prefix and mode-string exits and the
permission warning; those are its import-time behavior, not this module's.

## State

- `cfg`: the single mutable object this module owns. Shared by reference
  across the process; `reload_config()` mutates it in place, which is what
  makes use-time `cfg` reads rehash-visible.
- All other module attributes are effectively immutable after import
  (rebinding them affects nothing that already imported the values
  by name).
- Persistent inputs: `config.ini`, `config.local.ini`, the secret store file,
  environment variables. Nothing is written by this module.

## Concurrency

No locks. `reload_config()` mutates `cfg` in place while other threads (module
workers) may be reading it; `configparser` is not documented thread-safe, so a
reader could in principle observe a partially applied reload. In practice
reload is rare (SIGHUP / `.rehash`), reads are key-level `get` calls, and the
window is one file parse; no incident-grade race is visible in the code, but
the absence of any synchronization is worth knowing when adding new use-time
readers.

## Security

- Credentials resolve secret-store-first so plaintext can be migrated out of
  `config.ini`; the `[secrets]` file tier is permission-checked by
  `secret_store.perms_ok()` before being read at all.
- `_secret_or_cfg` returns credentials into module constants (`NS_PW` etc.)
  that live in process memory for the lifetime; `internets.py` gates their
  transmission on TLS (`_tls_or_refuse`).
- The empty-prefix guard is itself a security control (prevents
  every-message-is-a-command); the cooldown floor prevents config from
  disabling rate limiting.
- No path validation on `modules_dir` / log paths: config is operator-trusted
  input by design.

## Functions

### `_secret_or_cfg(secret_name, section, key, default="")`

See "Secret resolution". Pure read; no side effects. Called only at import
time, which is why credentials are frozen thereafter.

### `reload_config() -> list[str]`

See "The parse path". Side effect: mutates `cfg` in place. Raises nothing on a
missing base file (returns a shorter list; only the import-time caller treats
empty as fatal). Propagates `configparser.Error` on malformed content - which
`internets.py - _on_sighup()` and `admin_cmds.py - cmd_rehash()` catch and log,
so a bad edit plus rehash degrades to an error message with the previous
in-memory values partially or wholly intact rather than killing the bot.

## Implementation walk

- Lines 1-21: docstring (secret-store policy), imports, `__version__`.
- Lines 24-31: `_secret_or_cfg` (validation / business logic).
- Lines 36-40: parser construction and path resolution (initialization).
- Lines 43-67: `reload_config` and the initial read.
- Lines 72-77: missing-file fail-closed guard (error handling).
- Lines 81-92: `[irc]` constants, credentials via secret store.
- Lines 96-105: `[bot]` constants with the prefix guard and cooldown floors
  (validation / security enforcement).
- Lines 108-111: `DESIRED_CAPS` (protocol capability wishlist).
- Lines 115-138: CLI definition and the import-time `parse_args()`.
- Lines 142-147: `LOG_*` constants with CLI-over-config precedence.

## Findings

- **questionable** | `config.py - reload_config()` | Import-time validation is
  not re-applied on reload: a rehash can load an empty `command_prefix` into
  the live `cfg`, and `internets.py - IRCBot._cmd_prefix()` will then return
  `""` (the key exists, so the `CMD_PREFIX` fallback never triggers),
  recreating exactly the every-message-is-a-command hazard the import-time
  guard exists to prevent. Cooldown floors are import-frozen so they are not
  bypassable the same way.
- **questionable** | `config.py` (`CONFIG_PATH`, `_LOCAL_CONFIG`) | Paths
  resolve against the process working directory at import
  (`Path("config.ini").resolve()`), not the installation directory; starting
  the bot from any other directory silently reads a different (or no) config.
  The tests exploit this via `chdir` in the `reimport` fixture, which confirms
  the coupling.
- **questionable** | `config.py` import-time parsing | Malformed values fail
  with raw tracebacks (`KeyError: 'irc'`, `ValueError` from `int()`) while the
  missing-file and empty-prefix cases get curated actionable `SystemExit`
  messages; the failure quality is inconsistent for equally likely operator
  errors.
- **questionable** | `config.py - cli_args` | `parse_args()` at import couples
  every importer to `sys.argv` and can exit the process during import; the
  repo handles it by convention (argv pinning in each test file) rather than
  by an importable-without-parsing structure (e.g. lazy or `main()`-scoped
  parsing).
- **test-gap** | `config.py - reload_config()` | No test covers reload with a
  *malformed* base file (parse error propagation) or the concurrent-read
  window; only merge semantics and the missing-file import guard are pinned.
