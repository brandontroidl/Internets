# audit_log.py - append-only, HMAC-chained audit trail for privileged bot actions

## Purpose

Records every privileged admin action (auth, load/unload/reload, restart, raw
protocol injection, shadow-bans, shutdown, ...) as one JSON line in
`audit.log`, chained so that editing, reordering, or deleting a record is
detectable by re-walking the chain. It exists separately from the ordinary
`botlog` stream because the audit trail has different requirements: durable,
bounded, permission-restricted (0600, records carry hostmasks which are PII),
and tamper-evident rather than merely informational.

## Responsibilities / boundaries

Belongs here:

- Record format, canonicalization, and the hash chain (legacy SHA-256 and
  current HMAC-SHA-256).
- HMAC key lifecycle: generation, 0600 sidecar persistence, fail-closed
  handling of an unreadable key.
- Size-based rotation.
- Chain verification (`verify()`) and cheap record counting (`count()`).
- The process-wide singleton accessor `default()`.

Deliberately not here:

- Deciding *what* to audit or sanitizing actor strings. Callers own that:
  `admin_cmds.py - AdminCommandsMixin._audit()` resolves the hostmask and
  strips control bytes via `admin_cmds.py - _clean_actor()` before calling
  `record()`.
- Rendering records for humans. `admin_cmds.py - cmd_audit()` and its helpers
  (`_audit_parse()`, `_audit_format()`, `_audit_haystack()`) read the file
  directly and format for IRC.
- Cross-process locking. The module docstring states this explicitly: one
  `threading.Lock` serializes writers *within* a process; there is no fcntl
  flock, so two processes writing the same `audit.log` would interleave. The
  single-instance guarantee comes from `process_lock.py`, not from this file.
- Passwords. By caller convention, no password, password length, or derivative
  ever reaches `record()`; see Security below.

## Dependencies and dependents

Dependencies (all stdlib): `hashlib`, `hmac`, `json`, `logging`, `os`,
`secrets`, `threading`, `datetime`, `pathlib`. One optional soft dependency:
`record()` best-effort increments `metrics.registry.audit_records_total`
(`metrics.py:213`) inside a broad try/except, so a missing or broken metrics
module never affects auditing.

Dependents:

- `admin_cmds.py` - imports `default as _audit`. `_audit()` (the mixin method)
  writes records for every privileged handler; `cmd_audit()` reads the file
  back and calls `verify()`/`count()`; `cmd_stats()` calls `count()`;
  `_count_audit_mentions()` scans the file for `.fingerprint`.
- `modules/health.py - HealthModule._verify_audit()` - runs
  `audit_log.default().verify()` for the `.health` report, importing lazily so
  a removed `audit_log.py` degrades to "unavailable" instead of breaking the
  module.
- `tests/test_audit_log.py` - full behavioral coverage (helpers, chain,
  verify, key handling, rotation, singleton).

## Lifecycle

Imported at `admin_cmds.py` import time (module-level `from audit_log import
default as _audit`). The singleton `AuditLog` is constructed on the first
`default()` call, which resolves `./audit.log` against the process CWD at that
moment (the bot runs from its deployment directory, so this lands next to the
other state files). Everything else is lazy: the HMAC key is loaded or
generated on first use (`_load_key()`), the chain tip is read from disk on the
first `record()` (`_load_tip()`), and the log file itself is created 0600 on
the first write. There is no shutdown hook; each `record()` closes its file
handle before returning, so process exit needs no cleanup.

## State

- In-memory, per instance: `_tip` (cached hash of the last record, so each
  `record()` avoids re-reading the file), `_key` (cached 32-byte HMAC key),
  `_lock` (a `threading.Lock`).
- Persistent: `audit.log` (JSON lines, chmod 0600), `audit.log.key` (64 hex
  chars = 32 bytes, created 0600 via `os.open` mode bits), rotated segments
  `audit.log.<UTC stamp>`, and `audit.log.key.bad` if a malformed key was ever
  moved aside.
- Module-level: `_default_instance` / `_default_lock` for the singleton.

The cached `_tip` is the only state that can go stale relative to disk, and
only if something else writes the file; within the documented
single-process-writer model it cannot.

## Concurrency

All three public methods (`record()`, `verify()`, `count()` excepted - see
below) serialize on `self._lock`. `record()` and `verify()` take the lock;
`count()` does **not** (it is a read-only line count and tolerates a torn
read - the worst case is an off-by-one during a concurrent append, since
records are single-line writes).

`record()` is a blocking disk write. Callers choose their threading model:

- Most admin handlers call `AdminCommandsMixin._audit()` synchronously from
  the event loop; the write is small and rare (one admin action), so the
  blocking cost is accepted.
- The failed-auth path is the exception: `admin_cmds.py - cmd_auth()` wraps
  `_audit` in `asyncio.to_thread(...)` because that path is reachable by
  unauthenticated users under flood, and the surrounding comment records the
  three load-bearing decisions (outside `_auth_lock`, off the loop, capped by
  the lockout so a flood cannot churn the log through rotation).

Cross-process concurrency is explicitly out of scope (no flock); the process
lock in `process_lock.py` is what makes that safe in practice.

## Failure behavior

- Unreadable existing key: `_load_key()` raises `RuntimeError` (fail-closed)
  rather than regenerating, because regeneration would silently void every
  prior v2 record's HMAC. `record()` therefore also raises. The caller
  (`AdminCommandsMixin._audit()`) catches everything and logs a warning, so an
  admin action still completes when auditing fails - availability over audit,
  a deliberate caller-side choice.
- Malformed or short key: backed up to `audit.log.key.bad` (never truncated in
  place) and a fresh key generated. Prior v2 records then fail `verify()`
  under the new key (evidence: `tests/test_audit_log.py -
  TestVerify.test_wrong_key_fails_verify`); recovery is manual, from the
  `.bad` backup.
- Key persistence failure: logged as an error ("audit chain will not survive
  restart") but the in-memory key is still used, so auditing continues for the
  life of the process.
- Log write failure: `record()` logs and re-raises `OSError`; again absorbed
  by the caller.
- Corrupt line mid-file: `_load_tip()` treats it as a terminating boundary
  (tip = last good hash before it); `verify()` reports `(False, idx)` at that
  line; `count()` still counts it (it counts non-blank lines, not valid
  records).
- Rotation rename failure: logged, and the log simply keeps growing past
  `_MAX_BYTES` until a later attempt succeeds - fail-open on boundedness,
  fail-safe on data (nothing is dropped).

## Security

- **Threat model** (module docstring): an attacker who obtains a *copy* of
  `audit.log` (backup, accidental commit) cannot forge chain entries, because
  the HMAC key lives only in the 0600 sidecar. Pure tail truncation by an
  attacker with write access to both files is acknowledged as undetectable
  from the file alone; the docstring claims all *non-tail* edits are caught.
  That claim does not survive the legacy fallback - see Findings.
- **File permissions**: log and key are created 0600 via `os.open` mode bits
  and re-chmodded after each append (`_enforce_perms()`, POSIX only). The log
  carries hostmasks (PII) and every privileged action, hence the restriction.
- **Field injection**: `_canonical()` joins fields with NUL separators so a
  value containing a delimiter cannot shift field boundaries in the hashed
  form. Display-side injection (IRC formatting bytes in a nick forging an
  extra column in `.audit` output) is prevented at write time by
  `admin_cmds.py - _clean_actor()`, which strips C0/C1 control bytes from
  actor and hostmask. Note the `args` field is *not* control-stripped at
  either end; in practice args are admin-supplied or structured dicts.
- **Never recorded**: passwords or any derivative. `cmd_auth()` passes
  `args=None` on success and only the failure counter on failure ("args
  carries the counter only - never the password, its length, or any
  derivative"). `cmd_raw()` passes its line through
  `sender.redact_secrets()` before auditing, because `.raw` is the one command
  whose argument can carry a credential (`identify <pw>`, `oper ...`) - the
  wire gets the full line, the durable record gets the redacted copy.
- **Durability is not fsync**: `record()` writes and closes, which flushes
  Python's buffer to the kernel page cache, but never calls `os.fsync()`.
  Records survive process death from that point on, but a power loss or host
  crash can lose the most recent records. See Findings for the drifted caller
  comment that claims otherwise.

## Classes

### AuditLog

Responsibility: the whole mechanism - one instance owns one log path plus its
key sidecar.

Constructor: `AuditLog(path="./audit.log")`. Resolves `path` immediately
(`Path(path).resolve()`), derives `_key_path` as `<name>.key` in the same
directory, creates the lock, and leaves `_tip`/`_key` as `None` for lazy
initialization. Construction touches no files.

Invariants:

- `_tip`, when non-None, equals the `this_hash` of the last record in the
  current (post-rotation) file, or the genesis hash `"0" * 64`.
- Every v2 record's `this_hash` is the HMAC over
  `prev_hash|ts|actor|host|action|stable(args)` (NUL-joined), and
  `prev_hash` equals the prior record's `this_hash` (genesis for the first).
- The key file, when valid, holds at least 32 bytes hex-encoded.

Concurrency assumptions: one process, any number of threads; `self._lock`
guards all mutation and `verify()`.

Extension constraints: the record dict shape and `_canonical()` field order
are load-bearing - `verify()` must reproduce byte-identical input from the
stored fields, which is why `_stable_args_str()` exists (deterministic
re-serialization of dict args with sorted keys). Adding a field to the hash
input would break verification of all existing records unless gated on a new
`v` value.

## Functions and methods

| Symbol | Purpose | Notes |
|---|---|---|
| `_iso_utc_now()` | UTC timestamp `YYYY-mm-ddTHH:MM:SS.ffffffZ` | 27 chars, tested for shape |
| `_stable_args_str(args)` | Deterministic string form of args for hashing | `None` -> `""`; str passes verbatim; else compact sorted JSON with `default=str`; `repr()` fallback for unserializable (e.g. circular) values |
| `_canonical(...)` | NUL-joined UTF-8 bytes of the six hashed fields | Delimiter cannot be forged across field boundaries |
| `_sha_record(...)` | Legacy plain SHA-256 digest | Verify-only; pre-3.0.0 records |
| `_hmac_record(key, ...)` | HMAC-SHA-256 digest under the audit key | Used for every new record |
| `_is_jsonable(x)` | True if `x` survives `json.dumps` without `default=` | Decides whether `args` is stored structurally or as its stable string |
| `default()` | Process-wide singleton at `./audit.log` | Double-checked locking under `_default_lock` |

### AuditLog._load_key()

Returns the cached key, else reads the sidecar. Three branches: (1) readable
and >= 32 bytes: cache and return; (2) read raises `OSError`: raise
`RuntimeError` - fail-closed, an existing-but-unreadable key must never be
overwritten because the `O_TRUNC` in the generation path would void all prior
HMACs; (3) malformed/short (including non-hex, which `bytes.fromhex` maps to
`b""`): rename aside to `.bad`, then generate 32 fresh bytes from
`secrets.token_bytes`, write 0600 (`os.open` with mode, plus an explicit
`chmod` on POSIX). A failed persist is logged but the in-memory key is used
anyway.

### AuditLog._load_tip()

Streams the file line by line, keeping the last well-formed `this_hash` (a
64-char string). A JSON-corrupt line is a terminating boundary: the tip is
whatever preceded it, so the next `record()` chains from the last good record
and `verify()` independently flags the corrupt index. Missing file or read
error yields genesis. Callers: `record()` (first write of the process only,
under the lock).

### AuditLog._rotate_if_oversize()

Caller must hold `_lock`. If `stat().st_size > _MAX_BYTES` (5 MiB), renames
the file to `audit.log.<UTC %Y%m%dT%H%M%SZ>` and resets `_tip` to genesis, so
the new file starts a fresh chain. Forensic implications:

- Each rotated segment remains independently verifiable against the same key
  (evidence: `tests/test_audit_log.py -
  TestRotation.test_rotated_segment_independently_verifies`), but nothing
  links segments cryptographically: deleting an entire rotated segment is
  undetectable by `verify()`, which only ever reads the live file.
- All read paths (`.audit`, `.audit verify`, `count()`,
  `_count_audit_mentions()`, `.health`) look only at the live `audit.log`;
  history that has rotated is invisible to the bot's own tooling and must be
  inspected on disk.

### AuditLog.record(actor, host, action, args=None) -> str

Stringifies inputs, then under the lock: load key, rotate if oversize, lazily
load tip, compute `this_hash = HMAC(key, prev|ts|actor|host|action|args_str)`,
append one compact JSON line, re-chmod, advance `_tip`, best-effort metrics
increment, return the new hash. `args` is stored structurally when
JSON-serializable (so `.audit grep` and `.fingerprint` can match inside it)
and as the stable string otherwise. First-ever write creates the file 0600
with `O_APPEND` from the start. Ordering matters: rotation runs *before* the
tip is used so a post-rotation record correctly chains from genesis.
Failure: `OSError` propagates (see Failure behavior); `RuntimeError` from the
key path propagates.

### AuditLog.verify() -> tuple[bool, int]

Under the lock, re-walks the whole live file from genesis. Per non-blank line:
parse JSON (fail -> `(False, idx)`), require a dict, require
`prev_hash == prev`, recompute the digest - HMAC when `v == 2`, plain SHA-256
otherwise (legacy pre-3.0.0 records) - and compare to `this_hash`. Blank
lines are tolerated and do not advance `idx`. Returns `(True, -1)` for an
intact or absent file. Callers: `cmd_audit()` (the `verify` subcommand),
`modules/health.py`. The legacy branch is a security hole - see Findings.

### AuditLog.count() -> int

Non-blank line count, no lock, `0` on any read error. Callers: `cmd_stats()`,
`cmd_audit()`.

## Implementation walk

- Lines 1-36 (docstring): design goals, threat model, the tail-truncation
  concession, legacy compatibility, wire-up note. One inaccuracy ("append-
  binary mode") - see Findings.
- Lines 38-55 (imports, constants): `_GENESIS_HASH` = 64 zeros,
  `_RECORD_VERSION` = 2, `_MAX_BYTES` = 5 MiB.
- Lines 58-104 (pure helpers): timestamp, stable args serialization,
  canonical byte form, the two digest functions. All deterministic; the whole
  verify story rests on them.
- Lines 107-127 (`__init__`): path resolution, key-path derivation, lock,
  lazy caches.
- Lines 131-175 (`_load_key`): key lifecycle including the fail-closed branch
  and the backup-then-regenerate branch (security enforcement).
- Lines 177-200 (`_load_tip`): tip recovery incl. corrupt-line boundary
  (error handling).
- Lines 202-210 (`_enforce_perms`): best-effort chmod, POSIX-gated
  (security enforcement).
- Lines 212-232 (`_rotate_if_oversize`): size check, timestamped rename,
  fresh-genesis reset (resource management).
- Lines 236-294 (`record`): the write path described above (state mutation +
  security enforcement). The metrics increment at 289-293 is best-effort
  behind a blanket except; its `# nosec B110: best-effort cleanup` comment
  mislabels a metrics increment as cleanup - cosmetic only.
- Lines 296-350 (`verify`): full chain re-walk (validation).
- Lines 352-364 (`count`): cheap line count (introspection).
- Lines 367-373 (`_is_jsonable`): args shape decision for `record`.
- Lines 376-390 (singleton): module lock + double-checked `default()`.

Every block is reachable and pulls weight; nothing dead found.

## Findings

- **Defect** - `audit_log.py - AuditLog.verify()`: the legacy fallback is a
  downgrade-forgery hole. Any record lacking `v == 2` is verified with
  *unkeyed* SHA-256 at *any* chain position, so an attacker with write access
  to `audit.log` alone (no key needed - the algorithm is in this file) can
  truncate the chain at any record k and append forged legacy-format records
  whose SHA chain links from record k-1's `this_hash`; `verify()` reports the
  chain intact. This contradicts the module docstring's claim that "Editing,
  reordering, or deleting any non-tail record IS caught" and reduces the
  effective tamper-evidence to that of the pre-3.0.0 scheme. A fix would
  accept legacy records only before the first v2 record (a chain, once
  upgraded, never downgrades), or gate legacy acceptance behind a flag.
- **Questionable** - `audit_log.py - AuditLog._rotate_if_oversize()`: the
  rotation stamp has one-second granularity and `Path.rename()` silently
  replaces an existing destination on POSIX, so two rotations within the same
  second would destroy the first rotated segment. Reaching 5 MiB of records
  twice in one second is implausible under the capped auth-flood path, but
  the failure is silent forensic loss.
- **Questionable** - `audit_log.py - AuditLog.record()`: no `os.fsync()`;
  durability stops at the kernel page cache, so a power loss can drop the
  most recent records - including a `restart`/`shutdown` record written
  moments before the process exits, the exact records the callers take care
  to write early (`cmd_restart()`, `cmd_shutdown()` comments).
- **Questionable** - `audit_log.py - AuditLog.verify()` +
  `admin_cmds.py - cmd_audit()`: `verify` and every read path examine only
  the live `audit.log`; rotated segments are never verified or displayed, so
  `.audit verify` reports "intact" even if a rotated segment was tampered
  with or deleted.
- **Doc-drift** - `admin_cmds.py - cmd_auth()` comment (admin_cmds.py:270):
  "record() writes and fsyncs" - `record()` never calls fsync.
- **Doc-drift** - `audit_log.py` module docstring: "opens the file in
  append-binary mode" - `record()` opens in text mode (`"a"`,
  `encoding="utf-8"`); only the create-path `os.open` uses raw flags, also
  wrapped in a text-mode `fdopen`.
- **Test-gap** - `tests/test_audit_log.py`: no multi-thread `record()` test,
  although in-process thread safety is a documented guarantee of the class;
  no test pinning the (absent) fsync behavior either way; no test covering
  the rotation same-second rename collision.
