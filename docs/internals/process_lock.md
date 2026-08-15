# process_lock.py - PID-based single-instance lock with stale detection

## Purpose

Prevents two bot instances from running concurrently against the same on-disk
state. The bot's JSON state files (locations / channels / users / secrets)
are written tmp-and-rename; two concurrent writers would silently clobber
each other's mid-flight changes (module docstring). The lock is a classic
PID lockfile - `internets.pid` containing `pid|start_time|hostname` - with
liveness probing so a crash (including `kill -9`) does not permanently wedge
startup.

## Responsibilities / boundaries

Belongs here:

- Atomic lockfile creation (`O_CREAT | O_EXCL`), payload format, release.
- Stale-lock detection: PID liveness probing, corrupt-file recovery,
  foreign-host refusal.
- Context-manager and explicit acquire/release APIs; the `LockHeld` error
  type.

Deliberately not here:

- Choosing the lock path or deciding when to release early: that is the
  caller's job. `internets.py - _entry()` picks
  `Path("./internets.pid").resolve()` and `internets.py - _main()` releases
  the lock explicitly before `os.execv()` on the restart path.
- Any fcntl/flock or lock inheritance: this is purely a lockfile protocol,
  chosen for portability and diagnosability (the payload names the holder).
- Guarding the audit log or any specific file; the lock guards the whole
  process instance, and other modules (e.g. `audit_log.py`, which has no
  cross-process locking of its own) rely on it transitively.

## Dependencies and dependents

Dependencies: stdlib only (`errno`, `logging`, `os`, `socket`, `sys`,
`time`, `pathlib`). One *optional* runtime dependency: `psutil`, imported
lazily inside `_pid_is_alive()` and only on non-POSIX platforms; its absence
degrades to fail-open with a warning, never an import error.

Dependents:

- `internets.py - _entry()` wraps the entire event loop in
  `with ProcessLock(lock_path) as lock:` and maps `LockHeld` to a critical
  log line plus `sys.exit(1)`.
- `internets.py - _main(lock)` receives the same instance so the restart path
  can release it before `execv`.
- `tests/test_process_lock.py` - full behavioral coverage.

## Lifecycle

Imported at `internets.py` import time. Constructed once per process in
`_entry()`; `acquire()` runs via `__enter__` before the event loop starts and
resolves the path at that moment (relative paths resolve against the *then
current* CWD by design, though `_entry()` already passes an absolute path).
`release()` runs via `__exit__` on any normal or exceptional exit, or
explicitly pre-`execv` on restart. No destructor; an abrupt death leaves the
file behind for the next start's stale detection.

## State

- In-memory: `_path_arg` (as given), `_path` (resolved at acquire), `_owned`,
  `_pid`, `_start_time` (wall-clock at acquire, not the OS process start
  time), `_hostname`.
- Persistent: the lockfile, mode 0o644, one line
  `pid|start_time:.3f|hostname\n`. Not secret; world-readable is fine and
  aids diagnosis.

## Concurrency

No threads and no in-process lock: one instance is acquired once at startup
on the main thread. The interesting concurrency is *inter-process*:

- Two clean simultaneous starts: both race `os.open(..., O_CREAT | O_EXCL)`;
  exactly one wins, the loser gets `FileExistsError` mapped to `LockHeld`
  ("appeared during acquire"). The exclusive create is the real mutual
  exclusion primitive.
- Two simultaneous starts over a *stale* file: both can observe the stale
  file, both unlink it, and the interleaving `A-unlink, A-create, B-unlink,
  B-create` lets B unlink A's fresh lockfile and both proceed owned. The
  `release()` PID guard prevents the wrong process from unlinking on exit,
  but not the dual-run itself. See Findings.
- Ordering within `acquire()`: examine-existing (raise / reclaim / fail-open)
  strictly before create; payload write happens after the exclusive create on
  the same fd.

## Failure behavior

- **Holder alive (same host)**: `LockHeld` with pid, host, and start time in
  the message; the existing lockfile is left untouched (evidence:
  `tests/test_process_lock.py -
  TestContention.test_lockheld_does_not_remove_existing_lock`).
- **Holder dead (`kill -9`, crash, reboot)**: the leftover file's PID probes
  dead (`ProcessLookupError` from `os.kill(pid, 0)`), the file is logged and
  unlinked, and acquisition proceeds. This is the whole point of stale
  detection: no manual cleanup after a crash.
- **PID reused by an unrelated process**: indistinguishable from a live
  holder; the probe says alive and the lock is refused conservatively
  (module docstring: "better safe than corrupt state"). The stored
  `start_time` is diagnostic only - it is never compared against the probed
  process's actual start time, so recovery from PID reuse is manual deletion
  of the lockfile by the operator.
- **Liveness unknowable** (non-POSIX without psutil, or an unexpected
  `OSError` from the probe): fail-open - log a warning, unlink, take the
  lock. The docstring's rationale: better a benign race on an admin's
  Windows box than refuse-to-start.
- **Foreign hostname** (shared NFS / Docker volume): probing another host's
  PID table is impossible, so the lock is refused conservatively without
  probing at all (evidence: `tests/test_process_lock.py -
  TestDifferentHost` asserts the probe is never called). The hostname in the
  payload exists precisely so the operator can diagnose this case. Note the
  atomicity of `O_EXCL` itself on ancient NFSv2 is not addressed (NFSv3+ is
  atomic); the hostname check is diagnosis, not an NFS-safe protocol.
- **Corrupt / empty / unreadable lockfile**: `_read_existing()` returns
  `None`; the file is logged, removed, and acquisition proceeds.
- **Cannot create** (missing parent dir, permissions): any non-`FileExists`
  `OSError` is wrapped in `LockHeld` ("could not create lockfile"), so
  `_entry()` handles every acquisition failure through one exception type.
- **Release anomalies**: file vanished -> read fails -> pid mismatch -> no-op
  with a warning; file overwritten by another owner -> pid mismatch -> the
  other owner's file is left alone. `release()` is idempotent and safe to
  call without a prior acquire.

## Security

Low-trust surface. The lockfile content is written by whoever last held the
lock; `_read_existing()` treats it as untrusted input (strict int parse for
the PID, float parse with a 0.0 fallback for start time, malformed input
rejected as `None` rather than trusted). A hostile local user who can write
the lockfile can at worst deny startup (plant a live PID) or disable the
guard (delete the file) - both require filesystem access to the bot's
directory, at which point the state files themselves are already exposed.
`os.kill(pid, 0)` sends no signal; `PermissionError` (a live process owned by
another user) is deliberately read as *alive*. No secrets, no network.

## Classes

### LockHeld

`Exception` subclass carrying a human-readable `reason` (also passed to the
base constructor, so `str(e)` works). Raised only from `acquire()`. Caught in
`internets.py - _entry()`.

### ProcessLock

Responsibility: one lockfile, full protocol. Constructor stores the path
argument *unresolved* - resolution is deferred to `acquire()` so a relative
path honors the CWD current at startup, not at import (`_resolved_path()`
deliberately avoids `Path.resolve()` because the parent directory may not
exist yet; it only absolutizes against the CWD).

Important fields: `_owned` is the single source of truth for whether release
should act; `_pid` is captured at acquire and re-checked against the file at
release.

Invariants: `_path` is non-None from the first `acquire()` on; `_owned` is
True only between a successful `acquire()` and the next `release()`; while
owned, the file *should* contain our PID (release re-verifies rather than
assumes).

Collaboration: constructed and driven by `internets.py` only. Concurrency
assumptions: single-threaded use per instance; inter-process safety per the
Concurrency section. Extension constraints: the three-field `|`-separated
payload is parsed leniently (`pid` required, the rest optional), so appending
fields is backward compatible; reordering is not.

## Functions and methods

### _pid_is_alive(pid) -> Optional[bool]

Tri-state liveness probe: `True` live, `False` dead, `None` unknowable
(fail-open path). Algorithm:

1. `pid <= 0` -> `False` (also covers the parse-failure sentinel values).
2. POSIX: `os.kill(pid, 0)` - signal 0 performs the permission/existence
   check without delivering anything. Clean return -> `True`.
   `ProcessLookupError` -> `False`. `PermissionError` -> `True` (the process
   exists but belongs to another user; conservative refusal beats clobbering
   state). Any other `OSError`: `errno == ESRCH` -> `False` (belt-and-braces;
   `ProcessLookupError` *is* the ESRCH subclass), else log and `None`.
3. Non-POSIX: import `psutil`; missing -> warn and `None`;
   `psutil.pid_exists(pid)` -> its boolean; any exception -> warn and `None`.

Callers: `ProcessLock.acquire()` (same-host branch only). Every branch is
pinned by `tests/test_process_lock.py - TestPidIsAlive`.

### ProcessLock.acquire() -> None

1. Resolve path; capture own pid, wall-clock start time, hostname
   (`socket.gethostname()`, `"unknown"` on failure).
2. If a lockfile exists, parse it via `_read_existing()`:
   - Parse failure -> warn, unlink, continue.
   - Same host -> probe `_pid_is_alive(other_pid)`; different host -> treat
     as alive without probing (cannot verify a foreign PID).
   - alive `True` -> raise `LockHeld` (file untouched).
   - alive `False` -> warn, unlink the stale file, continue.
   - alive `None` -> warn, unlink, continue (fail-open).
3. `os.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o644)` - the atomic claim.
   `FileExistsError` (lost a same-instant race) -> `LockHeld`; other
   `OSError` -> `LockHeld`.
4. Write `pid|start_time|hostname\n`, close the fd in a `finally`, set
   `_owned`, log.

Postcondition on success: file exists containing our PID; `_owned` True.
Postcondition on `LockHeld`: nothing created, `_owned` False.

### ProcessLock.release() -> None

Idempotent. No-op unless `_owned` and `_path` set. Re-reads the file and
unlinks only if the leading field is exactly our PID (digit-check then int);
on mismatch it warns and leaves the file - the guard that stops a process
from destroying a lock now owned by someone else (see the stale-reclaim race
in Concurrency). Always clears `_owned`, even when it declined to unlink, so
the instance never retries.

### ProcessLock._read_existing() -> Optional[tuple[int, float, str]]

Parses `pid|start|host`. Raises `RuntimeError` if called before the path is
set (a real conditional rather than an `assert`, so the invariant survives
`python -O`; the comment cites Bandit B101). Missing/unreadable/empty file or
non-integer PID -> `None`; bad float -> `0.0`; missing host -> `""`.

### Remaining members

| Symbol | Purpose |
|---|---|
| `_resolved_path()` | Default `./internets.pid`; absolutize against current CWD without `resolve()` |
| `_safe_unlink(p)` | Unlink; `FileNotFoundError` silent, other `OSError` logged not raised |
| `__enter__` / `__exit__` | acquire / release; returns `None` so exceptions are never swallowed (pinned by `TestContextManager.test_exit_does_not_swallow_exception`) |
| `path` (property) | Resolved path, `None` before first acquire |
| `owned` (property) | Current ownership flag |

## The restart / execv interaction (caller contract)

Verified against `internets.py`:

- `_entry()` acquires the lock around `asyncio.run(_main(lock))` and passes
  the instance in, explicitly so the restart path can release it early
  (`_entry()` docstring).
- On `.restart` (`admin_cmds.py - cmd_restart()` sets `bot._restart_flag`),
  `_main()` reaches its tail, flushes and closes log handlers, then calls
  `lock.release()` *before* `os.execv(sys.executable, [sys.executable] +
  sys.argv)` (internets.py:1478-1502). The reason, from the in-line comment:
  `execv` replaces the process image but **preserves the PID**, and the
  `with`-block's `__exit__` never runs after a successful `execv` - so
  without the explicit release, the new image's `acquire()` would read the
  old lockfile, probe its own (live) PID, and refuse to start. The
  fail-open/None path would not rescue it either, since the PID probes
  genuinely alive.
- A release failure there is logged (`restart_lock_release_failed`) and
  `execv` proceeds anyway; the new image would then hit exactly that
  self-deadlock. Accepted risk, and visible in the log.
- On Windows the restart is `subprocess.Popen` + `sys.exit(0)` instead. The
  explicit `lock.release()` sits *before* the platform branch, so the
  lockfile is already gone when the child is spawned; the parent's normal
  exit then runs `__exit__`, whose second `release()` is an idempotent no-op
  (the in-line comment calls the early release "for symmetry" on this path).

## Implementation walk

- Lines 1-37 (docstring): threat (state-file corruption), payload rationale,
  probe semantics incl. PID-reuse conservatism, Windows psutil fail-open,
  hostname-for-NFS diagnosis, acquire-time path resolution.
- Lines 39-50 (imports, logger).
- Lines 53-58 (`LockHeld`): error type with `reason`.
- Lines 61-96 (`_pid_is_alive`): the tri-state probe (validation).
- Lines 99-127 (`ProcessLock.__init__`): deferred-resolution state.
- Lines 131-138 (`_resolved_path`): path policy (initialization).
- Lines 142-218 (`acquire`): stale-decision tree then atomic create then
  payload write (control flow + resource management). The decision tree is
  the algorithmic heart; every branch lands in warn-and-reclaim, refuse, or
  proceed.
- Lines 220-242 (`release`): PID-guarded unlink (cleanup + validation).
- Lines 246-253 (`_safe_unlink`): tolerant unlink (error handling).
- Lines 255-280 (`_read_existing`): untrusted-input parse (validation).
- Lines 284-291 (context manager), 295-303 (properties), 306 (`__all__`).

Nothing dead or unreachable found.

## Findings

- **Questionable** - `process_lock.py - ProcessLock.acquire()`: the
  stale-reclaim path (examine, unlink, then `O_EXCL` create) is not atomic.
  Two processes started simultaneously over the same stale lockfile can
  interleave so that the second unlinks the first's freshly created lockfile
  and both acquire, which is the dual-run the lock exists to prevent. The
  `release()` PID guard contains the damage at exit but not during the run.
  Window: microseconds, and only reachable after a crash left a stale file;
  a flock-based or link-count-based protocol would close it.
- **Questionable** - `process_lock.py` - the recorded `start_time` is
  captured but never used by any decision (liveness ignores it) and it is
  the *lock acquisition* wall-clock, not the process start time, so it
  cannot detect PID reuse either; it is purely a diagnostic field, which the
  docstring does state, but a comparison against `/proc/<pid>/stat` start
  time on POSIX would convert the conservative PID-reuse refusal into
  automatic recovery.
- **Test-gap** - `tests/test_process_lock.py`: no test exercises the
  concurrent stale-reclaim race (two acquirers over one stale file), and no
  test covers the restart contract (release-before-execv) at the
  `internets.py` level; the lock-side behavior it depends on (release
  idempotence, PID guard) is covered.
