# secret_store.py - two-tier secret resolution and storage

## Purpose

`secret_store.py` is the single resolution path for every outbound credential the bot
holds: NickServ / server / oper passwords, weather and API provider keys, and the one
PII field (`weather_user_agent`, a contact address sent in HTTP User-Agent headers).
These values must be *recoverable* - the bot sends them on the wire - so this module is
the reversible-secret counterpart to `hashpw.py`, which handles the one verify-only
secret (the admin password hash). Hashing an outbound credential would break the
authentication it exists to perform; see `docs/internals/hashpw.md` for the boundary.

Resolution is two-tier, first hit wins:

1. Environment variable `INTERNETS_<NAME_UPPER>`.
2. `config.ini` `[secrets]` section, readable only if the file mode is exactly 0o600.
3. Caller-supplied default (empty string).

OS-keyring support was removed in v3.0.0. Verified against the current source: the file
contains no `keyring` import or reference; the only backends in `get()` are the
environment and `SECRETS_FILE` (`secret_store.py - get()`).

## Responsibilities and boundaries

Belongs here:

- Tiered lookup (`get()`), storage (`set_value()`), removal (`delete()`), and
  introspection (`status()`, `list_stored()`) of named secrets.
- Permission enforcement on the secrets file (`perms_ok()`), fail-closed.
- The canonical registry of secret names (`KNOWN_SECRETS`) and the legacy
  config-location map (`CONFIG_LOCATIONS`) that drives migration.
- One-time migration of plaintext credentials out of non-`[secrets]` config sections
  (`migrate()`), and the operator CLI (`python -m secret_store ...`).

Deliberately not here:

- Password hashing or verification - `hashpw.py`.
- Config parsing for non-secret settings - `config.py` (which *calls* this module).
- Encryption. Despite the module docstring's claim (see Findings), the file backend is
  plaintext guarded by POSIX permissions, not encryption-at-rest.
- Any caching. Every `get()` re-reads the file, so a rotation performed by the CLI is
  visible to the next in-process lookup without a restart (subject to `config.py`
  having captured values in module constants at import - see Dependents).

## Dependencies and dependents

External dependencies: standard library only (`argparse`, `configparser`, `getpass`,
`logging`, `os`, `stat`, `sys`, `pathlib`).

Dependents (all resolve secrets *through* this module rather than reading
`config.ini[secrets]` themselves):

- `config.py - _secret_or_cfg()`: tier-0 for `NS_PW`, `SERVER_PW`, `OPER_PW` module
  constants; falls back to the legacy `[irc]` config fields only when the store returns
  empty. These constants are captured once at import, so rotating an IRC credential
  requires a bot restart even though `get()` itself is uncached.
- `modules/base.py - cred()`: the module-facing helper (weather, imdb, stocks, twitch,
  search, ipintel, etc.) with the same store-first / config-fallback contract plus its
  own placeholder filtering on the fallback path.
- Tests: `tests/test_secret_store.py` (direct), `tests/test_config.py` (mocks
  `secret_store.get` to isolate the tier logic), `tests/test_modules_base.py`.

## Lifecycle

- **Import**: `config.py` imports it at bot startup, before any IRC connection.
  `SECRETS_FILE = Path("config.ini").resolve()` is resolved *once at import time from
  the current working directory* - the bot and the CLI must run from the deployment
  root or they will silently address a different `config.ini`. `config.py` resolves its
  own `CONFIG_PATH` the same way, so the two stay consistent per-process.
- **Runtime**: `get()` is called on demand; no state is initialized and nothing is
  destroyed. There is no handle to close.
- **CLI**: `python -m secret_store <status|list|get|set|delete|init|migrate>` runs
  `main()` under `if __name__ == "__main__"`.

## State

- Owns no in-memory state. There is no cache, no loaded-secrets dict; every lookup
  hits `os.environ` and (if needed) re-reads `SECRETS_FILE` from disk. Secret values
  exist in memory only as transient local `str` objects inside the calling frame.
- Persistent state: the `[secrets]` section of `config.ini` (gitignored; the committed
  template is `config.ini.example`). Writes preserve the rest of the file
  byte-for-byte via targeted text edits (`_write_file_secret()`,
  `_delete_file_secret()`) because a `configparser` round-trip would strip every
  comment.
- Module-level constants: `ENV_PREFIX`, `SECRETS_FILE`, `KNOWN_SECRETS`,
  `CONFIG_LOCATIONS`, `_PLACEHOLDERS`. All are read-only after import (tests
  monkeypatch `SECRETS_FILE`).

## Concurrency

Single-threaded, synchronous, no locks. The read path is called from the asyncio bot
process but performs blocking file I/O; the file is small and read rarely (startup and
module init), so this is tolerated rather than offloaded.

Write safety relies on two properties, not locking:

- `_atomic_write_text()` writes a 0o600 temp file and `os.replace()`s it, so a reader
  never observes a torn file.
- The write paths are only reachable from the operator CLI and `migrate()`; the bot
  process itself never writes. The design is single-writer by convention. Two
  concurrent CLI writers would last-write-win (a read-modify-write race), which is
  accepted, not prevented.

## Failure behavior

| Condition | Behavior |
| --- | --- |
| Secrets file absent | `perms_ok()` returns `(True, "absent")`; `get()` returns the default. |
| File mode not exactly 0o600 (POSIX) | `get()` logs an error and returns the default - fail-closed, never reads the file. `set_value()` / `delete()` raise `PermissionError`. |
| `stat()` fails | `perms_ok()` returns `(False, ...)` - same refusals as above. |
| `configparser.Error` on read | `get()` logs the exception *type only* and returns the default; `list_stored()` treats the file tier as absent. |
| Newline (CR or LF) in a value | `set_value()` raises `ValueError` before touching the file. |
| Write interrupted | Temp file unlinked, exception propagates; the real file is untouched. |
| `migrate()` target missing | `FileNotFoundError` propagates. |
| Per-secret migrate error | Recorded as `error:<ExceptionType>` in the result dict; other secrets continue. `_cmd_migrate()` exits 1 if any errored. |

The asymmetry between read and write refusal is deliberate: a read failure degrades to
"secret unset" (the bot runs keyless), but a *delete* blocked by bad permissions raising
`PermissionError` instead of returning "not found" is load-bearing - an operator
removing a leaked credential must not be told it was already gone
(`secret_store.py - delete()` docstring, enforced in `_delete_file_secret()`).

## Security

- **Trust boundary**: the process environment and the local filesystem are trusted;
  the *contents* of config values are not blindly trusted (placeholder filtering), and
  values being written are validated (newline rejection).
- **Permission model** (`perms_ok()`): POSIX mode must be exactly `0o600`. Looser
  (0o644, 0o640) *and stricter* (0o400) modes are both refused - the check is equality,
  not "no group/world bits" (see Findings). Absent file passes. On Windows
  (`os.name == "nt"`) the check always passes with reason `"windows (acl-based)"` -
  POSIX bits are advisory there and NTFS ACLs are the real control, which this module
  does not inspect.
- **Fail-closed reads**: a value present in a world-readable file is never returned
  (`tests/test_secret_store.py - TestFailClosed`).
- **Injection**: `set_value()` rejects CR/LF in values because the file backend writes
  `name = value` as one line; an embedded newline could forge a section header or key
  (`tests/test_secret_store.py - TestNewlineInjection`). The `name` argument is not
  validated (see Findings).
- **What is never logged or printed**: secret values. Parse and migrate errors log
  exception type only (`_safe_exc()`), because `configparser` messages embed the
  offending line, which may contain a partial secret. `migrate()` deliberately omits
  even the file *path* from its log line to stay clear of CodeQL's
  `py/clear-text-logging-sensitive-data` taint heuristic. The CLI `get` prints
  `(set, N chars, backend=...)`, never the value; `list` prints name and backend label
  only; `set` reads the value via `getpass` when `--value` is omitted so it stays out
  of shell history.
- **Placeholders**: `_PLACEHOLDERS` (case-insensitive) - `changeme`,
  `your-key-here`, `set-via-secret-store`, `todo`, `xxx`, etc. - are treated as unset
  in both tiers of `get()`, in `list_stored()`'s file tier, and in `migrate()`, so a
  template value can never reach an outbound request.
- **Env naming convention**: config key `name` maps to environment variable
  `ENV_PREFIX + name.upper()`, i.e. `weatherapi_key` -> `INTERNETS_WEATHERAPI_KEY`.
  The reverse mapping is implicit; only `KNOWN_SECRETS` names are surfaced by
  `list`/`status`, but `get()` accepts any name.
- **Rotation**: `_cmd_migrate()` prints an explicit rotate-everything warning after
  moving values, because the pre-migration plaintext lives in git history.

## Classes

None. The module is a flat function namespace plus constants.

## Functions and methods

### Public API

#### `get(name, default="") -> str`

Tiered lookup. Env tier: reads `INTERNETS_<NAME_UPPER>`, strips whitespace, and applies
the placeholder filter - a placeholder or whitespace-only export falls through to the
file tier rather than masking it. File tier: refuses (returns `default`, logs) on bad
permissions; parses the file fresh with `configparser`; returns the stripped value if
non-empty and not a placeholder. `configparser` lowercases option names on read, so file
keys match case-insensitively. No exceptions escape except programming errors; callers:
`config.py - _secret_or_cfg()`, `modules/base.py - cred()`, `_cmd_get()`.

#### `set_value(name, value) -> str`

Validates `value` for CR/LF (`ValueError`), delegates to `_write_file_secret()`, and
returns the backend label `"file"`. The return value survives from the pre-3.0.0
multi-backend signature so existing callers and tests did not change. Raises
`PermissionError` if the file exists with wrong mode.

#### `delete(name) -> list[str]`

Returns `["file"]` on removal, `[]` if the key or file was absent. `PermissionError`
propagates - deliberately not swallowed (see Failure behavior).

#### `status() -> dict`

Non-sensitive diagnostic snapshot: file path, existence, permission verdict and reason,
env prefix. Used by `_cmd_status()`.

#### `list_stored() -> dict[str, str]`

For each name in `KNOWN_SECRETS`, reports `"env"`, `"file"`, or `""`. The file tier
applies permission and placeholder checks; the env tier tests only truthiness of the
raw variable, which diverges from `get()` (see Findings).

#### `perms_ok(path=SECRETS_FILE) -> tuple[bool, str]`

The permission gate described under Security. Pure check, no side effects; the reason
string doubles as the operator-facing remediation hint
(``run `chmod 600 ...` ``).

#### `migrate(config_path=Path("config.ini"), *, scrub=True) -> dict[str, str]`

For every entry in `CONFIG_LOCATIONS`, reads `parser.get(section, key)` from
`config_path`; skips absent (`skipped:absent`), empty, and placeholder
(`skipped:empty`) values; stores the rest via `set_value()` (`stored:file`). When the
target is `SECRETS_FILE` itself and the mode is not 0o600, it chmods to 0o600 *first*
so the subsequent writes pass the permission gate. With `scrub=True` (default) it then
blanks the migrated keys in their source sections via `_scrub_config_ini()`. Per-secret
exceptions are captured as `error:<Type>`; behavioral evidence in
`tests/test_secret_store.py - TestMigrate`.

### File backend internals

#### `_atomic_write_text(text) -> None`

Creates the parent directory, opens a sibling `.tmp` with `os.open(..., 0o600)` so the
0o600 mode applies from the first byte (no world-readable window), writes, and
`os.replace()`s over `SECRETS_FILE`. On any write error the temp file is unlinked and
the exception re-raised. A trailing `os.chmod(SECRETS_FILE, 0o600)` (POSIX) re-asserts
the mode in case `umask` or an existing-file replace left it different.

#### `_find_secrets_section(lines) -> tuple[int | None, int]`

Scans for a line whose strip equals `[secrets]`; returns the index range of the section
body (start = line after the header, end = next `[...]` header or EOF). Returns
`(None, len(lines))` when the header is absent. Note the exact-match requirement: a
header with a trailing inline comment (`[secrets]  ; note`) parses as a section to
`configparser` but is invisible here (see Findings).

#### `_write_file_secret(name, value) -> None`

The comment-preserving editor. Permission-gated. Cases, in order: empty/new file ->
write header plus line; no `[secrets]` section -> append one at EOF (adding a missing
final newline first); key present (matched case-insensitively on the text before the
first `=`, preserving indentation) -> replace that line in place; otherwise -> insert
before the section's trailing blank lines. Comments and other sections are untouched
byte-for-byte (`tests/test_secret_store.py - TestSecretsSectionMidFile`).

#### `_delete_file_secret(name) -> bool`

Mirror of the writer: permission-gated, finds the key line inside `[secrets]`, deletes
exactly that line, rewrites atomically. Returns `False` if file, section, or key is
absent.

### Migration internals

#### `_scrub_config_ini(cfg_path, names) -> None`

Line-oriented rewrite that blanks (`key =`) every line whose key matches a migrated
key name, in every section *except* `[secrets]`. The `[secrets]` exemption is
load-bearing: source and destination are the same file by default, so blanking there
would undo the migration just performed. Matching is by key name only - the section
component of `CONFIG_LOCATIONS` is not consulted (see Findings). Uses
`cfg_path.write_text()`, not `_atomic_write_text()`, because `cfg_path` may not be
`SECRETS_FILE`.

### CLI layer

| Function | Behavior | Exit codes |
| --- | --- | --- |
| `_cmd_status(_)` | Prints `status()` as a table plus the tier order. | 0 |
| `_cmd_list(_)` | Table of `KNOWN_SECRETS` name -> `env` / `file` / `(unset)`. The explicit equality branches exist to break CodeQL data-flow taint, per its docstring. | 0 |
| `_cmd_get(args)` | Presence check only: `(set, N chars, backend=...)`. No flag prints the value; the documented extraction path is a `python -c` one-liner. Backend attribution re-tests the env var itself. | 0 set, 1 unset |
| `_cmd_set(args)` | `--value` or `getpass` prompt; rejects empty; calls `set_value()`. | 0, 2 empty |
| `_cmd_delete(args)` | Calls `delete()`; reports which backend was touched. | 0 removed, 1 not found |
| `_cmd_init(args)` | Byte-for-byte copy of `config.ini.example` -> `config.ini` at mode 0o600 (via `os.open` with `O_EXCL`, or `_atomic_write_text()` under `--force`). Refuses to overwrite without `--force`; with it, warns that old values are lost and must be rotated. | 0, 1 exists, 2 no template |
| `_cmd_migrate(args)` | Wraps `migrate()`; prints stored/error summary and the rotate-everything banner. `--no-scrub` stores without blanking sources. | 0, 1 on any error |
| `_build_parser()` / `main(argv)` | Standard argparse subcommand dispatch via `set_defaults(func=...)`. | per-command |

## Implementation walk

- **Module docstring, imports, logger** (`secret_store.py:1-46`): contract summary
  (contains the stale encryption claim - Findings), stdlib imports, the
  `internets.secrets` logger.
- **Constants** (`ENV_PREFIX`, `SECRETS_FILE`, `KNOWN_SECRETS`, `CONFIG_LOCATIONS`,
  `_PLACEHOLDERS`): configuration data. `KNOWN_SECRETS` is the introspection and
  migration population; adding a name there (plus a `CONFIG_LOCATIONS` entry if it has
  a legacy location) is the entire integration cost of a new secret.
- **`_safe_exc()`**: security enforcement - exception-type-only logging.
- **`perms_ok()`**: validation; the single permission oracle every read and write path
  consults.
- **Public API block** (`get`, `set_value`, `delete`, `status`, `list_stored`):
  described above.
- **File backend block** (`_atomic_write_text`, `_find_secrets_section`,
  `_write_file_secret`, `_delete_file_secret`): resource management and state
  mutation; the comment-preserving text-edit strategy is forced by `configparser`'s
  lossy `write()`.
- **Migration block** (`_scrub_config_ini`, `migrate`): compatibility / one-time state
  transition.
- **CLI block** (`_cmd_*`, `_build_parser`, `main`, `__main__` guard): operator
  interface; every handler is print-plus-return-code with no additional logic beyond
  what the public API provides, except `_cmd_init`, which owns the template-copy
  behavior itself.

All blocks are reachable; no dead code was found beyond the `sasl_password` registry
entry noted below.

## Findings

- **doc-drift** | `secret_store.py` module docstring | Claims the module "provides
  encryption-at-rest, not hashing"; the implementation stores plaintext guarded only by
  0o600 file permissions - no encryption exists anywhere in the file. The claim reads
  as a leftover from the removed keyring backend.
- **questionable** | `secret_store.py - perms_ok()` | Equality check refuses modes
  *stricter* than 0o600: a read-only 0o400 config makes `get()` silently return
  defaults (bot runs keyless) even though the file is less exposed than required.
- **questionable** | `secret_store.py - set_value()` | Validates `value` for CR/LF but
  not `name`; `_write_file_secret()` writes the name raw, so a name containing a
  newline or `]` would corrupt the file. Only operator-controlled today (CLI argv), but
  the guard is asymmetric.
- **questionable** | `secret_store.py - _scrub_config_ini()` | Blanks by key *name*
  across every non-`[secrets]` section, ignoring the section component of
  `CONFIG_LOCATIONS`; an unrelated section reusing a key name (e.g. another
  `user_agent`) would be blanked by migration.
- **questionable** | `secret_store.py - list_stored()` / `_cmd_get()` | Env-tier
  detection tests only that the variable is non-empty, without the
  placeholder/whitespace filtering `get()` applies - an env var holding `changeme` is
  reported as backend `env` while `get()` would return the file value or default.
- **questionable** | `secret_store.py - KNOWN_SECRETS` (`sasl_password`) | No code
  anywhere calls `get("sasl_password")`; SASL PLAIN sends `NS_PW`
  (`internets.py - IRCBot._handle_cap()`), so the entry is inert and its "falls back to
  nickserv_password if unset" comment describes a fallback that is really "this key is
  never read".
- **questionable** | `secret_store.py - _find_secrets_section()` | Exact-match header
  detection misses `[secrets]` followed by an inline comment, which `configparser`
  accepts; `set_value()` would then append a duplicate `[secrets]` section.
- **test-gap** | `secret_store.py - _cmd_get()/_cmd_set()/_cmd_delete()/_cmd_list()/_cmd_status()/_cmd_migrate()/main()` |
  Only `_cmd_init()` has direct CLI-layer tests (`tests/test_secret_store.py - TestInit`);
  the other handlers' exit codes and never-print-the-value output contract are untested.
