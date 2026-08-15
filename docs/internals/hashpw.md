# hashpw.py - admin password hashing CLI and verify layer

## Purpose

`hashpw.py` is both the operator CLI that generates the `[admin] password_hash` value
for `config.ini` and the library the live bot uses to verify admin passwords at auth
time. It exists to keep hash creation and hash verification in one file so their
policies (byte caps, accepted algorithms, error contracts) cannot drift apart - the
module previously had exactly that failure: creation accepted 1024 characters while
verification's caller rejected 128, so an operator could mint a password that could
never authenticate (`hashpw.py - check_password()` docstring;
`admin_cmds.py - AdminMixin.cmd_auth()` comment).

This is the *verify-only* secret path. The admin password is stored as a one-way hash
because the bot only ever needs to compare it, never transmit it. Every credential the
bot must send outbound (NickServ, SASL, API keys) is the opposite case and lives in
`secret_store.py` as a recoverable value - hashing those would break the
authentication they perform. See `docs/internals/secret_store.md`.

## Responsibilities and boundaries

Belongs here:

- The three hash constructors (`hash_scrypt`, `hash_bcrypt`, `hash_argon2`) and the
  self-describing `algo$rest` storage format.
- `verify_password()` - prefix dispatch plus per-algorithm fail-closed verification.
- The shared password policy (`check_password()`, `MIN_PASSWORD_LEN`,
  `MAX_PASSWORD_BYTES`, `BCRYPT_MAX_PASSWORD_BYTES`).
- Cost-parameter selection: env-tunable, clamped (`_env_int`, `_argon2_params`,
  `_bcrypt_rounds`, `_best_scrypt_params`).
- The interactive CLI (`main()`), including its self-test and latency sanity checks.

Deliberately not here:

- Reading or storing the hash - `botlog.py - get_hash()` reads it from the layered
  config; the operator pastes the CLI output into `config.ini` by hand.
- Startup validation of the stored hash - `botlog.py - _validate_hash()`.
- Lockout, rate limiting, hostmask binding, auditing -
  `admin_cmds.py - AdminMixin.cmd_auth()`.

## Dependencies and dependents

- Standard library: `hashlib.scrypt`, `hmac.compare_digest`, `base64`, `os.urandom`,
  `getpass`, `argparse`, `time`, `logging`.
- Optional third-party, imported lazily inside the functions that need them:
  `bcrypt` and `argon2-cffi`. A missing package aborts the CLI via `sys.exit()` at
  hash time and raises `ValueError` at verify time (see Failure behavior). scrypt
  needs no extra package and is therefore the CLI default.
- Dependents: `admin_cmds.py` imports `MAX_PASSWORD_BYTES` and `verify_password`
  (the only production caller of verification); `botlog.py - _validate_hash()` keeps
  its own copy of the accepted prefixes (`_VALID_HASH_PREFIXES`) for startup
  validation; `tests/test_hashpw.py` exercises everything directly with real
  (floor-cost) hash calls.

## Lifecycle

- **As a CLI**: `python hashpw.py [--algo scrypt|bcrypt|argon2]` runs `main()`:
  prompt twice via `getpass`, policy-check, hash, print the `config.ini` snippet,
  self-test (positive and negative verify), exit.
- **As a library**: imported by `admin_cmds.py` at bot startup; `verify_password()` is
  invoked per auth attempt via `asyncio.to_thread`. No module-level state is created
  beyond constants; nothing to tear down.
- Cost parameters are resolved *per call* from the environment (`_argon2_params()`,
  `_bcrypt_rounds()`), not at import, so tests and operators can adjust without
  reloading the module.

## State

Stateless. All functions are pure apart from environment reads, `os.urandom` salt
generation, logging, and CLI I/O. The stored hash embeds its own algorithm tag and
cost parameters, which is the rotation story: bumping the default costs affects only
newly minted hashes; existing hashes keep verifying with their embedded parameters
until the operator re-runs the CLI. (The docstring's pointer to `KEY_ROTATION.md` is
dangling - see Findings.)

## Concurrency

No shared mutable state, so thread-safe by construction. `cmd_auth` runs
`verify_password` in a worker thread (`asyncio.to_thread`) because argon2 at 128 MiB /
t=3 and scrypt at N=2^17 are deliberately slow; running them on the event loop would
stall the whole bot. `_verify_argon2()` constructs a fresh `PasswordHasher` per call -
no cross-thread reuse. Note that verification cost is attacker-influenced work on an
unauthenticated path; the serialization and lockout that bound it live in `cmd_auth`,
not here.

## Failure behavior

`verify_password()` error contract - the load-bearing part of this module:

**Raises `ValueError`** (configuration problems the operator must fix; the caller may
log the message text because these strings never contain the password):

- Empty or `None` stored hash: `"No password hash configured."`
- Unrecognised prefix (not `scrypt$` / `bcrypt$` / `argon2$`).
- `bcrypt` / `argon2-cffi` not installed - the `ImportError` is wrapped in
  `ValueError` inside `_verify_bcrypt()` / `_verify_argon2()` so it flows through the
  same operator-facing channel.

**Returns `False`, never raises** (anything attacker-influenceable):

- Wrong password (all algorithms).
- Malformed stored hash *payload*: scrypt catches `ValueError`/`OSError`/`MemoryError`
  (bad field count, bad base64, kernel refusing the embedded cost); bcrypt catches
  `ValueError`/`TypeError`/`IndexError`; argon2 catches `VerifyMismatchError`/
  `VerificationError`/`InvalidHashError`/`IndexError`.
- A bcrypt candidate over 72 bytes (see Security).

The split exists because exception *text* from the backends can echo fragments of the
input or hash, and because distinguishable errors on the auth path are an oracle.
`cmd_auth` completes the contract on its side: it catches `ValueError` as a logged
config error, and any *other* unexpected exception is treated as a failed attempt and
logged as the exception type only (`admin_cmds.py - AdminMixin.cmd_auth()`).

Hash-time failures are loud, not swallowed: missing packages `sys.exit()`, an
over-long bcrypt password raises `ValueError`, and `_best_scrypt_params()` raises
`RuntimeError` if every parameter set fails.

## Security

- **Trust boundary**: the candidate password arrives from an unauthenticated IRC user
  (via `cmd_auth`); the stored hash comes from operator-controlled config. Nothing in
  this module trusts the candidate; the stored hash's prefix is trusted only enough to
  dispatch, and payload parsing is fail-closed.
- **Byte-denominated caps**. `MAX_PASSWORD_BYTES = 128` is measured in UTF-8 bytes
  because every downstream bound is a byte bound (the 512-byte IRC frame, `.encode()`
  in every hasher). A code-point check would admit a 128-character CJK passphrase that
  is 384 bytes on the wire (`tests/test_hashpw.py -
  TestPasswordPolicy.test_check_password_limits_are_byte_denominated`). The cap must
  stay at or below `internets.IRCBot._MAX_ARG_LEN` (400) or the auth-side guard would
  be unreachable behind the dispatcher's argument-length rejection; pinned by
  `tests/test_hashpw.py - TestPasswordPolicy.test_cap_cannot_be_shadowed_by_the_dispatch_guard`,
  which reads the constant out of `internets.py` source.
- **The bcrypt 72-byte wall** (`BCRYPT_MAX_PASSWORD_BYTES = 72`; the 2026-07-22 fix,
  verified present in current code). bcrypt ignores every byte past 72: bcrypt < 5.0
  silently truncates (an auth bypass - any string sharing the stored password's first
  72 bytes verifies), bcrypt >= 5.0 raises. The guard is enforced at **both** ends:
  - Hash time: `check_password(pw, "bcrypt")` returns an error in the CLI, and
    `hash_bcrypt()` itself raises `ValueError` as defense in depth for direct callers,
    so a hash that under-protects the account can never be minted
    (`tests/test_hashpw.py - test_bcrypt_refuses_past_its_72_byte_wall`,
    `test_over_long_bcrypt_password_can_never_be_hashed`).
  - Verify time: `_verify_bcrypt()` refuses candidates over 72 bytes and returns
    `False`, closing the collision against *already-stored* bcrypt hashes
    (`test_verify_refuses_a_candidate_bcrypt_would_truncate`). The accepted tradeoff:
    an operator whose pre-existing bcrypt password exceeds 72 bytes is locked out and
    must re-hash. The refusal `log.warning` deliberately omits the candidate's length
    so the log cannot become a password-length oracle on this attacker-reachable path.
  - The exact boundary is verified on the allow side too: 72 bytes hashes and
    verifies (`test_bcrypt_accepts_exactly_72_bytes`), and the guard does not leak
    into algorithms without the limit
    (`test_verify_unaffected_for_algorithms_without_the_limit`).
- **Timing**: scrypt comparison goes through `_ct_eq()`
  (`hmac.compare_digest`); bcrypt and argon2 comparisons are constant-time inside
  their libraries. The bcrypt length refusal returns faster than a real verification,
  but it reveals only a property of the attacker's own input. Exception-text leakage
  is handled by the contract above.
- **What is never logged**: the password, any derivative, or its length on the verify
  path. CLI input goes through `getpass` (no echo, no argv, no shell history). The
  produced hash is printed to stdout by design - it is the artifact the operator came
  for. `_env_int()` logs raw env values, which are cost tunables, not secrets.
- **Parameter clamping as a guard**: `_env_int()` clamps to hard ranges
  (argon2 memory 19 MiB - 4 GiB, time 1 - 20, bcrypt rounds 10 - 16) so a
  misconfigured env var can neither disable the work factor (below OWASP floors) nor
  OOM the bot (terabyte memory costs).

## Classes

None. Flat functions, constants, and two dispatch dicts (`_ALGOS`, `_NOTES`, kept
key-consistent by `tests/test_hashpw.py - TestRegistries`).

## Functions and methods

### Policy

#### `check_password(pw, algo="") -> str | None`

Returns an operator-readable error string or `None`. Checks, in order: minimum length
(`MIN_PASSWORD_LEN = 8`, measured in characters), maximum size (128 UTF-8 bytes),
the bcrypt 72-byte limit when `algo == "bcrypt"`, and leading/trailing whitespace.
The whitespace rule exists because the bot strips command arguments before dispatch, so
an edge-whitespace password would hash fine and then never match over IRC - rejected at
creation instead of discovered at lockout. Shared by `main()` (hash time); the
verify-time caller (`cmd_auth`) enforces the same `MAX_PASSWORD_BYTES` constant
directly rather than calling this function.

### Parameter selection

| Function | Behavior |
| --- | --- |
| `_env_int(name, default, lo, hi)` | Reads an int env var; blank/unset -> default, non-integer -> default with warning, out of range -> clamped with warning. |
| `_argon2_params()` | `(memory_cost_kib, time_cost, parallelism)` from `INTERNETS_ARGON2_MEM_MIB` (exposed in MiB, converted to the KiB argon2-cffi expects) and `INTERNETS_ARGON2_TIME`; parallelism fixed at 4. |
| `_bcrypt_rounds()` | `INTERNETS_BCRYPT_ROUNDS`, default 13, clamped 10 - 16. |
| `_best_scrypt_params()` | Probes a fixed descending ladder of `(N, r, p)` sets - 2^17 (OWASP 2024) down to 4096 - by running a real 16-byte scrypt against a throwaway salt, returning the first the host's OpenSSL accepts (its `maxmem` cap or FIPS mode can refuse high N). Raises `RuntimeError` if all fail. The ladder order is load-bearing: it is a deliberate degradation chain, strongest first. |

### Hash constructors

All return `algo$<payload>` strings; the payload carries salt and cost parameters so
verification never depends on current defaults.

- `hash_scrypt(password)`: probes params, 32-byte random salt, 64-byte derived key,
  format `scrypt$N$r$p$<salt_b64>$<dk_b64>` (6 `$`-fields).
- `hash_bcrypt(password)`: lazy import (exits the CLI if missing), enforces the
  72-byte wall with `ValueError`, `bcrypt.hashpw` with `gensalt(rounds)`, format
  `bcrypt$<modular-crypt-string>`.
- `hash_argon2(password)`: lazy import, builds a `PasswordHasher` from
  `_argon2_params()` with 32-byte hash / 16-byte salt, format
  `argon2$<argon2-cffi encoding>` (the payload itself starts `$argon2id$...`).

### Verification

- `verify_password(password, stored) -> bool`: the dispatch and contract described
  under Failure behavior. Callers: `admin_cmds.py - cmd_auth()` (via `to_thread`),
  `main()`'s self-test, tests.
- `_verify_scrypt()`: splits into exactly 6 fields, recomputes with the embedded
  `N/r/p` and `dklen=len(expected)`, compares via `_ct_eq()`. Fail-closed on
  malformed input and on resource errors.
- `_verify_bcrypt()`: import guard (raises `ValueError`), 72-byte candidate refusal
  (returns `False`, logs without the length), then `bcrypt.checkpw` against the
  payload after the first `$`. Fail-closed on malformed payloads.
- `_verify_argon2()`: import guard (raises `ValueError`), `PasswordHasher().verify`
  on the payload; mismatch and malformed-hash exceptions -> `False`. Verification
  reads parameters from the hash string, so the default-parameter `PasswordHasher()`
  is correct here.
- `_ct_eq(a, b)`: `hmac.compare_digest` wrapper; constant-time equality for the
  scrypt path.

### CLI

#### `main() -> None`

Argparse (`--algo`, choices from `_ALGOS`, default `scrypt` - kept for operator
expectation stability, with a printed nudge toward argon2 for anything else), double
`getpass` prompt with mismatch exit, `check_password` gate, timed hash, per-algorithm
parameter echo, latency sanity checks (`< 0.050 s` warns the parameters are weak
against GPU/ASIC attackers; `> 1.0 s` notes the login-latency cost and points at the
env tunables), the `config.ini` snippet, and a mandatory round-trip self-test: the
minted hash must verify the entered password and must reject `"wrong"`, otherwise
`sys.exit`. CLI behavior is covered end-to-end in
`tests/test_hashpw.py - TestMainCLI` with monkeypatched `getpass` and `time.monotonic`.

## Implementation walk

- **Docstring and imports** (`hashpw.py:1-50`): usage, algorithm comparison table,
  the stable-default rationale, env tunables, OWASP 2024 reference parameters.
- **Argon2 parameter block** (`_ARGON2_*` constants, `_env_int`, `_argon2_params`):
  validation and configuration; the long comment records the GPU-throughput reasoning
  behind 128 MiB.
- **Password policy block** (`MIN_PASSWORD_LEN`, `MAX_PASSWORD_BYTES`,
  `BCRYPT_MAX_PASSWORD_BYTES`, `check_password`): security enforcement; each constant
  carries its coupling comment (dispatcher cap, bcrypt truncation history).
- **scrypt block** (`_best_scrypt_params`, `hash_scrypt`): the probe ladder
  (compatibility) and constructor.
- **bcrypt block** (`_BCRYPT_*`, `_bcrypt_rounds`, `hash_bcrypt`): constructor plus
  the hash-time half of the 72-byte guard.
- **argon2 block** (`hash_argon2`): constructor.
- **Verification block** (`verify_password`, `_verify_scrypt`, `_verify_bcrypt`,
  `_verify_argon2`, `_ct_eq`): protocol processing and security enforcement; the
  error contract lives here.
- **Registries** (`_ALGOS`, `_NOTES`): dispatch data for the CLI.
- **Self-test constants and `main()`** (`_FAST_HASH_THRESHOLD_S`,
  `_SLOW_HASH_THRESHOLD_S`, `main`, `__main__` guard): operator interface. The
  comment above the threshold constants claims automatic cost backoff that the code
  does not perform (Findings).

All code is reachable; the `else` branch of `main()`'s per-algorithm timing echo
(`hashpw.py:407-408`) is unreachable while `_ALGOS` holds exactly the three handled
algorithms, but it is a harmless default arm for a future addition rather than dead
logic.

## Findings

- **doc-drift** | `hashpw.py - verify_password()` docstring | "See KEY_ROTATION.md" -
  no such file exists anywhere in the repo (already recorded in
  `docs/security-model.md`); the rotation story the sentence describes is otherwise
  accurate.
- **doc-drift** | `hashpw.py` comment above `_FAST_HASH_THRESHOLD_S` | Claims
  "we back off automatically (drop memory by 25%, then time_cost by 1)"; no backoff
  code exists - `main()` only prints a NOTE telling the operator to lower the cost via
  env vars.
- **doc-drift** | `tests/test_hashpw.py -
  TestPasswordPolicy.test_over_long_bcrypt_password_can_never_be_hashed` docstring |
  Its "DOCUMENTED RESIDUAL" paragraph states the verify side "cannot" be closed and
  was "deliberately not made here", but `_verify_bcrypt()` does refuse over-long
  candidates and `test_verify_refuses_a_candidate_bcrypt_would_truncate` in the same
  file proves it - the residual paragraph is stale.
- **questionable** | `hashpw.py - hash_scrypt()` / `hash_argon2()` | Neither enforces
  `MAX_PASSWORD_BYTES` in-function (only bcrypt has an internal guard); a direct
  library caller can mint an argon2 hash for a >128-byte password that
  `cmd_auth` will then reject as a candidate before verification - exactly the
  hash-but-never-authenticate drift `check_password()` exists to prevent, avoided only
  when callers route through the CLI.
- **questionable** | `hashpw.py - _verify_scrypt()` | Maps `MemoryError`/`OSError`
  (a host too small for the hash's embedded `N`, i.e. an environment failure) to a
  silent `False`, indistinguishable from a wrong password and logged nowhere; an
  operator moving a high-cost hash to a smaller box sees only "wrong password".
- **questionable** | `botlog.py - _VALID_HASH_PREFIXES` (cross-cutting) | Duplicates
  `verify_password()`'s prefix set as a hand-maintained parallel enumeration; adding
  an algorithm to `_ALGOS` without updating it makes startup validation reject a hash
  that `verify_password()` accepts.
