# Design Decisions

Architectural decision records for the choices an incoming engineer would
otherwise re-litigate or accidentally revert. This is not exhaustive coverage of
every line of code; it captures the decisions that are non-obvious, that cost
something, or whose "obvious cleanup" reintroduces a failure the project already
had.

Every entry below is substantiated by source, a test, a code comment, or
`CHANGELOG.md`. Where the reasoning is not recorded anywhere and has been
reconstructed from the implementation, the entry says **"The implementation
implies ..."** explicitly. Nothing here is invented history.

Format per entry: Decision, Context, Rationale, Alternatives, Tradeoffs,
Constraints. "Constraints" is what a future change must not break.

Implementation detail for any decision is one link away in
[internals/index](internals/index.md); the system-level view is in
[architecture](architecture.md).

---

## ADR-001: Single asyncio event loop with threading.Lock for shared state

**Decision.** One asyncio event loop owns all IRC I/O, protocol state mutation, and
command dispatch. Blocking work runs under `asyncio.to_thread`. Shared state is
guarded by `threading.Lock`, never `asyncio.Lock`.

**Context.** An IRC bot is I/O-bound. Command handlers are coroutines run as
bounded tasks, but roughly 130 call sites across `modules/` and `admin_cmds.py`
offload blocking work (`requests` HTTP, `verify_password`, audit writes, disk
saves) to threads. Those workers call back into bot accessors: `IRCBot.is_admin()`,
`IRCBot.is_chanop()`, and the `Store` methods are all reachable off-loop.

**Rationale.** Recorded at the lock sites. `IRCBot.is_chanop()` carries the comment
naming the race it prevents: `_chanops` is mutated on the loop thread while worker
threads read it, so an unguarded read can tear or raise "dict changed size during
iteration". `threading.Lock` is the only primitive that both sides can take;
`asyncio.Lock` cannot be acquired from a synchronous worker. The implementation
implies the second motive stated in `architecture.md`: not relying on the GIL as an
implicit lock keeps the design valid on free-threaded builds.

**Alternatives.** Thread-per-connection multiplies the shared-state surface for a
single-connection program. Multiprocessing is excluded by construction: the module
system, `_commands`, `_modules`, and the `Store` in-memory dicts are all
same-process state, so IPC would dominate. Using `asyncio.Lock` would force every
accessor to be a coroutine and break every `to_thread` caller.

**Tradeoffs.** All protocol processing serializes behind one thread, so a handler
that forgets to offload stalls everything. Nothing enforces the offload rule; it is
convention plus review. `modules/mathx.py - MathxModule.cmd_isprime()` is the live
proof, and it is an any-user denial of service (see
[architecture](architecture.md#22-offloading-is-a-convention-not-an-enforcement)).

**Constraints.** No accessor reachable from a worker may become a coroutine. No
`Store` method may take two dataset locks (the current one-lock-per-method rule is
what makes deadlock impossible). The 50-task cap (`IRCBot._MAX_TASKS`) and 60 s
timeout (`IRCBot._CMD_TIMEOUT`) are the only bounds on loop occupancy and must not
be removed without a replacement.

---

## ADR-002: IRC protocol handled in-repo, no third-party IRC library

**Decision.** Wire parsing lives in `protocol.py` as six pure functions with no
state, no I/O, and no imports beyond `base64` and `re`. The state machine lives in
`internets.py`. No IRC library is a dependency.

**Context.** `requirements.txt` lists `requests`, `aiohttp`, `argon2-cffi`,
`bcrypt`, `PyJWT`, `cryptography`, and `defusedxml`. There is no IRC library at any
version. `protocol.py` is 125 lines and is tested independently by
`tests/test_protocol.py` plus a block in `tests/run_tests.py`.

**Rationale.** The recorded reason is narrower than the absence of a library. The
`protocol.py` docstring states only that the parsers were "extracted from
internets.py to keep the main bot class focused on orchestration and state
management". The implementation implies the rest: the feature set the bot actually
negotiates is `multi-prefix`, `away-notify`, `account-notify`, `chghost`,
`extended-join`, `server-time`, `message-tags`, and `sasl` (`config.py -
DESIRED_CAPS`), together with ISUPPORT `CHANMODES`/`PREFIX` parameter alignment and
SASL PLAIN with a CAP-END fallback. No claim about what third-party libraries do or
do not support is recorded anywhere in the repo, so none is made here.

**Alternatives.** Adopting a library trades parser maintenance for library
maintenance and pins the bot's IRCv3 support to the library's release cadence.
Forking one carries the maintenance burden without the upgrade path.

**Tradeoffs.** Every new capability is hand-implemented. The cost is already
visible: `IRCBot._handle_cap()` mishandles the multiline `CAP LS 302` reply the bot
itself requests, a defect a maintained library would likely not have (see
[internals/internets](internals/internets.md#findings)).

**Constraints.** Every wire-facing parser must stay **total**: it returns a value
for any `str`, never raises, and has no error branch. The read loop's resilience
depends on that property, and a new parser must be added to
`tests/test_protocol.py`. `sasl_plain_payload()` is the one non-total function and
is deliberately not wire-facing: its inputs are the bot's own nick and the
configured password.

---

## ADR-003: Thread-local DNS pinning for SSRF, not an IP-literal adapter

**Decision.** `modules/_netsafe.py` patches `socket.getaddrinfo` once at import with
a thread-local wrapper. Inside a `safe_open()` call the wrapper returns the
validated IP for the host being fetched, forcing urllib3 to connect to exactly that
address while the real hostname stays in the `Host` header, the SNI, and
certificate verification. Every redirect hop is re-resolved, re-validated, and
re-pinned.

**Context.** Modules that fetch user-influenceable URLs (`modules/probe.py`,
`modules/scinews.py`) need SSRF protection. Resolve-then-connect-by-name leaves a
TOCTOU window: DNS can rebind between the validating resolve and urllib3's own
resolve at connect time.

**Rationale.** Stated in the `_netsafe.py` module docstring, including the specific
blocker for the obvious alternative: under `requests` 2.34 with `urllib3` 2.7 the
`HTTPAdapter` `server_hostname` override does not propagate, so connecting to an IP
literal fails the TLS handshake on SNI. Pinning `getaddrinfo` keeps the hostname
intact while still forcing the validated address. The docstring also records the
containment argument: the wrapper is a no-op unless the calling thread has set a
pin, and aiohttp uses the loop resolver rather than this path.

**Alternatives.** All four are named in the docstring or its immediate context: an
IP-literal adapter (breaks SNI on the pinned versions), resolve-then-connect-by-name
(TOCTOU open), a custom adapter with `server_hostname` (does not propagate), and
aiohttp's connector (different API, wrong subsystem).

**Tradeoffs.** Patching a stdlib function globally is the most invasive part of the
codebase. It is idempotent and thread-local-scoped, but it is process-wide
machinery serving a handful of modules.

**Constraints.** Do not simplify to an IP-literal adapter without first verifying
SNI propagation in the pinned `requests`/`urllib3` versions. The all-answers rule
matters: a DNS reply mixing one public and one internal address must fail as a
whole. Note that the two SSRF guards currently disagree on IPv6 site-local
`fec0::/10`: `_netsafe.ip_is_blocked()` blocks it, `modules/base.py -
resolve_public()` does not (see
[internals/modules/base](internals/modules/base.md#findings)).

---

## ADR-004: Capability-based weather dispatch, accuracy-dominant, with a circuit breaker

**Decision.** Providers are discovered by method presence, not declaration. For each
request the dispatcher sorts eligible providers by
`(static reliability rank, -live health score, registration order)` and walks that
chain serially under a time budget. Each provider carries a circuit breaker.

**Context.** Thirty-two providers cover fourteen capabilities with very uneven
coverage and accuracy: NWS has US alerts and NDFD forecasts, Stormglass has marine,
Open-Meteo is the broadest keyless one. Depending on a single API is a single point
of failure; treating them as interchangeable throws away the accuracy differences.

**Rationale.** All three keys are grounded in code.
`weather_providers/_dispatch.py - CAPABILITY_METHODS` is the discovery table, and
`_RegisteredProvider` computes a provider's capability set with `hasattr` plus
`callable` at registration. The 40-line comment above `DEFAULT_RELIABILITY` records
the per-capability ranking rationale (NWS/ECMWF-driven models over GFS derivatives,
radar-blended nowcasts over pure model output, and so on). The sort itself is
`Dispatcher.sort_chain()`, and `tests/test_dispatcher.py - TestAccuracySort` pins
that accuracy beats registration order and that unlisted providers sort last. The
breaker constants are in `weather_providers/_health.py`: five consecutive failures
inside a 60 s window opens it for a 60 s cooldown, and a 401/403 trips it
immediately through `mark_auth_failure()`.

**Alternatives.** Health-dominant ranking would let a fast, less accurate provider
outrank a slower, more accurate one, optimizing latency at the cost of the answer.
Manual selection does not scale to fourteen capabilities across thirty-two
providers. Round-robin has no accuracy awareness at all. A racing fan-out is
rejected in `_dispatch.py`'s own comment: the chain is deliberately serial so one
upstream request per attempt keeps quota use and rate-limit exposure minimal.

**Tradeoffs.** A slow but healthy top-ranked provider holds its slot and adds
latency; the 30 s per-call and 45 s chain budgets bound that, and the comment ties
45 s to the 60 s command timeout with headroom for formatting and the IRC send.
Capability sets are computed once at registration, so a provider that grows a method
later stays invisible until reconfigured.

**Constraints.** `DEFAULT_RELIABILITY` is the dominant sort key and must stay
complete: a provider absent from a capability's table silently sorts at rank 99.
That failure is live today for `stormglass` under `current`, and the completeness
test misses it because it configures with an empty parser so keyed providers never
register (see
[internals/weather-providers/dispatch](internals/weather-providers/dispatch.md#findings)).

---

## ADR-005: Two-tier secret store, OS keyring removed in 3.0.0

**Decision.** Every outbound credential resolves through `secret_store.py - get()`:
the `INTERNETS_<NAME>` environment variable first, then the `[secrets]` section of
`config.ini`, then the caller's default. The file tier is refused unless the mode is
exactly 0600. Template placeholders are filtered at both tiers.

**Context.** These values must be recoverable because the bot transmits them, so
hashing is not available (that boundary is `hashpw.py`, which handles the one
verify-only secret). Before 3.0.0 there were three tiers and two files.

**Rationale.** `CHANGELOG.md` 3.0.0 records both changes and both reasons. The
keyring backend was removed because the bot targets headless deployments where
`keyring` resolves to the "fail" backend, and because the optional desktop
integration pulled roughly ten transitive dependencies (`keyring`, `jeepney`,
`secretstorage`, `jaraco-*`, `importlib-metadata`, `zipp`, `more-itertools`) for no
practical benefit; `requirements.lock` dropped from 33 to 23 packages. The separate
`secrets.ini` was merged into `config.ini` because "a flat 0o600 file beside a flat
0o644 file isn't meaningfully more secure than one 0o600 file holding both; the
split mostly created friction". Verified against current source: `secret_store.py`
contains no `keyring` import, and the only backends in `get()` are the environment
and `SECRETS_FILE`.

**Alternatives.** Named in the changelog and the module: a separate secrets file
(adds a second artifact to manage, back up, and deploy), the OS keyring (removed for
the reasons above), an encrypted file backend (adds key management with no
commensurate threat, since the file is already 0600 and gitignored).

**Tradeoffs.** Deployment is one file plus optional environment variables, but a
credential rotation still needs a restart: `config.py` captures `NS_PW`,
`SERVER_PW`, and `OPER_PW` into module constants at import, and `_on_sighup()`
deliberately does not refresh them. `get()` itself is uncached, so module-level
credentials do pick up a rotation.

:::{warning}
**Not encryption at rest.**

The `secret_store.py` module docstring still claims the module "provides
encryption-at-rest, not hashing". It does not: the file backend is plaintext
guarded by POSIX permissions. The claim is a leftover from the removed keyring
backend and is a documented doc-drift finding.
:::

**Constraints.** `migrate` moves legacy plaintext out of `[irc]` and blanks the
source, and it prints a mandatory rotate-everything warning because the old values
are in git history. That warning must survive any refactor of the CLI. Secret values
are never logged or printed: parse errors log the exception type only, because
`configparser` messages embed the offending line.

---

## ADR-006: HMAC-chained audit log, not plain SHA-256

**Decision.** Each audit record's hash is
`HMAC-SHA256(key, prev_hash || ts || actor || host || action || args)` with NUL
field separators. The 32-byte key lives in a separate 0600 sidecar,
`audit.log.key`. Records carry `"v": 2`.

**Context.** The pre-3.0.0 chain was plain SHA-256. The algorithm is in the source,
so anyone with a copy of `audit.log` could recompute the chain and forge entries
that verify.

**Rationale.** Stated in `CHANGELOG.md` 3.0.0: "the tamper-evident audit chain used
plain SHA-256, which anyone with a copy of `audit.log` could recompute ... a leaked
log alone can no longer be forged." NUL joining in `audit_log.py - _canonical()`
exists so a value containing a delimiter cannot shift field boundaries in the hashed
form. `_stable_args_str()` exists so dict arguments re-serialize deterministically
and `verify()` can reproduce byte-identical input.

**Alternatives.** Keeping plain SHA-256 (forgeable from the log alone), per-record
GPG signing (key-management overhead disproportionate to a single-host bot), an
external append-only sink (the correct long-term answer, out of scope here), no
chaining (no tamper detection).

**Tradeoffs.** Two files must be protected instead of one, and the key must never
be regenerated: `_load_key()` raises `RuntimeError` rather than regenerating on an
unreadable existing key, because regeneration would silently void every prior
record's HMAC. Pure tail truncation by an attacker with write access to both files
is acknowledged as undetectable from the files alone. Rotated segments verify
independently but are not linked to each other, so deleting a whole rotated segment
is invisible to `verify()`. There is no `fsync`, so a power loss can lose the most
recent records.

:::{danger}
**Known defect: legacy verification downgrade.**

`AuditLog.verify()` picks the hash scheme from each record's own `v` field, so a
record written without `v` is verified with unkeyed SHA-256 at **any** chain
position. A writer to `audit.log` can therefore truncate at any record and append
forged legacy-format records that chain correctly, and `verify()` reports the chain
intact. This reduces the effective tamper evidence to the pre-3.0.0 scheme and
contradicts the module docstring. Verified; recorded in [known issues](known-issues.md).
Candidate fixes: accept legacy records only before the first v2 record, or pin a
cutover index.
:::

**Constraints.** The record shape and `_canonical()` field order are load-bearing:
adding a field to the hash input breaks verification of every existing record unless
it is gated on a new `v` value. Passwords, password lengths, and derivatives must
never reach `record()`; `cmd_auth()` passes the failure counter only, and
`cmd_raw()` passes its line through `sender.redact_secrets()` first.

---

## ADR-007: Checksummed store envelope with quarantine on corruption

**Decision.** On-disk shape is
`{"schema": 2, "checksum": "<sha256>", "data": <payload>}`. On read, any envelope
defect renames the file to `<name>.corrupt.<unix-ts>` and starts from the dataset
default. Writes go to a temp file in the same directory, are chmodded 0600 before
the rename, keep a one-deep `.bak`, and land with `os.replace`.

**Context.** Three state files are written by a background thread every 30 s. A
power loss, full disk, or OOM mid-write can truncate one. Without protection, the
next startup either loads truncated JSON or resets to empty, and the next flush
overwrites the only copy.

**Rationale.** The checksum is taken over canonical JSON (`sort_keys=True`, compact
separators) so dict ordering and interpreter differences cannot change it. The
quarantine, not the checksum, is the durability invariant: `store.py -
Store._quarantine()` preserves the evidence so the next flush cannot destroy saved
locations, channel-rejoin state, and privacy opt-out flags. The 0600 chmod happens
**before** the rename so the final file, which holds nick, hostmask, and timestamp
PII, is never even momentarily world-readable.

**Alternatives.** No envelope (silent data loss). SQLite (a dependency and a
different corruption-recovery model for three small JSON files). WAL or journaling
(complexity unjustified by the data volume).

**Tradeoffs.** A quarantine event loses up to 30 s of tracking plus whatever was in
that dataset, and recovery is a manual rename-back or a `.bak` restore. There is no
`fsync` before `os.replace`, so a power loss can still lose the newest version; the
envelope turns that into a detected, recoverable event rather than silent
corruption. Legacy v1 bare payloads are accepted on read and upgraded on the next
flush, which means they carry no integrity check while they exist.

:::{warning}
**Known defect: the .bak copy is not permission-tightened.**

`Store._write()` creates `<path>.bak` with `Path.write_bytes` and never chmods it,
so on first creation it takes umask-default permissions, commonly 0644. The PII the
0600-before-rename sequence protects in `users.json` is world-readable in
`users.json.bak`. Verified; recorded in [known issues](known-issues.md).
:::

**Constraints.** Any new dataset must follow the whole pattern (own lock, own dirty
flag, `_read` with a matching-type default, a branch in `flush()`) or it silently
never persists. Records with `opted_out: true` are exempt from age-based pruning:
an opt-out is a privacy preference that must outlive the inactivity window, or the
bot would resume tracking a user who asked it not to.

---

## ADR-008: Daemon thread for the interactive console

**Decision.** The console dispatch loop runs on an explicit
`threading.Thread(daemon=True)` named `console-input`. Shutdown closes `sys.stdin`
to unblock the pending `input()`.

**Context.** The console reads stdin with `input()`, which parks its thread on a
blocking read that nothing short of process death interrupts. Cancelling the
asyncio task does not interrupt the syscall.

**Rationale.** Recorded twice, at `console.py` and again in the long comment inside
`internets.py - _main()`: an `asyncio.to_thread` worker runs on the default
executor and is **not** a daemon, so `asyncio.run()`'s
`loop.shutdown_default_executor()` waits forever for an `input()`-blocked worker and
the whole process hangs on its last shutdown log line. A daemon thread cannot hold
up interpreter shutdown, so cleanup completes even if the thread never returns.
`CHANGELOG.md` records that documentation across three sites once described this as
a `to_thread` worker and had to be corrected, which is why the rationale is now
restated at the call site.

**Alternatives.** `asyncio.to_thread` (the hang, observed). Non-blocking stdin via
`select`/`poll` (platform-dependent, awkward on Windows). No console at all (loses
the local operator surface for debug, loglevel, status, and shutdown).

**Tradeoffs.** The console is admin-equivalent with **no authentication of any
kind**: there is no password prompt and no `is_admin` check anywhere in
`console.py`, because the trust boundary is physical access to the process's stdin.
That is why `console.should_skip_console()` fails safe and refuses a non-TTY stdin,
and why `--no-console` exists. `console.py` also has no test file at all and is in
the coverage `omit` set, so changes here are unguarded by the suite; the failure
this decision prevents is exactly the kind a green run will not catch.

**Constraints.** Do not convert the thread to `asyncio.to_thread`. Do not remove the
`sys.stdin.close()` in `_main()`; without it the thread stays parked. The TTY check
must stay fail-safe, returning `True` (skip) on `AttributeError` or `ValueError` as
well as on a non-TTY.

---

## ADR-009: A provider that does not cover a point returns None, not an exception

**Decision.** No-data and failure are distinct. A provider outside its coverage
returns `None`, which falls through to the next provider with **no** health record
at all: neither `record_success()` nor `record_failure()`. Only exceptions reach
the failure and breaker path.

**Context.** `api.weather.gov` serves US points only and says so three ways: HTTP
400 (`out of bounds`) from `/alerts/active?point=`, HTTP 404 (`Data Unavailable For
Requested Point`) from `/points/`, and a 200 whose payload simply carries no
station, forecast URL, or marine zone.

**Rationale.** `CHANGELOG.md` records the concrete failure: all three reached the
dispatcher as exceptions, so every non-US `.w` or `.al` recorded a failure against
NWS and dinged its circuit breaker; enough of them would open it and degrade US
alerts to a less authoritative provider. The NWS package maps those statuses to an
`OutOfCoverage` exception caught by `none_if_uncovered()` and converted to `None`.
Genuine outages, rate limits, and auth errors (401/403/429/5xx) still raise, so the
breaker still sees what it should. Pinned by
`tests/test_dispatcher.py - TestNoDataHandling.test_none_result_is_not_recorded_as_success`.

**Alternatives.** A hardcoded bounding box (drifts when coverage changes, and does
not handle the Aleutian antimeridian wrap). Treating every non-200 as a failure (the
original behavior, which degraded US service under international traffic). A
separate coverage-check request (doubles request count against a quota).

**Tradeoffs.** Health scores stop reflecting "how often did this provider answer"
and start reflecting "how often did this provider fail when it should have
answered". That is the intent, but it means a provider can be perfectly healthy and
still never return data for a given region.

**Constraints.** Every regional provider (NWS, AirNow, ECCC, NOAA CO-OPS,
Pollen.com) must follow the same pattern. Returning empty must not stop the chain:
`weather_providers/nifc` currently violates this by ending the dispatch outside US
coverage so non-US queries never fall through to `firms` (recorded in
[known issues](known-issues.md)).

---

## ADR-010: Single-source weather rule

**Decision.** One observation per reading. Multi-provider mean or median blending of
readings is a rejected alternative, not a future feature. The single bounded
exception is missing-secondary-field gap-fill for current conditions only, and
derived fields are excluded from even that.

**Context.** Thirty-two providers can answer the same question with different
numbers. The tempting move is to average them into a "better" figure.

**Rationale.** The rejection is recorded in
[internals/modules/weather](internals/modules/weather.md#the-single-source-rule-deliberate-rejected-alternative)
with its date (2026-07-22) and its reason: providers are not independent samples of
one truth. They disagree mostly on **which** location, elevation, or grid cell they
measured, not on measurement noise, so averaging them manufactures a number no
instrument produced.

The narrow rule inside it has a documented incident. `weather_providers/base.py`
carries the full narrative above `_CURRENT_GAP_FIELDS`, and `CHANGELOG.md` repeats
it: `.w yosemite national park` printed `Temperature 24.2C :: Feels like 11.3C` at
44% humidity and 6.6 mph wind, a figure no apparent-temperature formula produces.
NWS had reported 24.2C from a station at 2900 m with no feels-like, and Open-Meteo's
model grid contributed a feels-like of 11.9C computed against its own 13.8C. The
same query for San Dimas erred the other way. A derived value must come from the
same observation as the temperature printed beside it.

**What gap-fill actually does.** For the `current` capability only, and for at most
three contributing results including the primary, the dispatcher fills fields that
are missing from `_CURRENT_GAP_FIELDS`: `humidity`, `wind_kph`, `wind_dir`,
`pressure_mb`, `visibility_m`, `description`. Temperature is never filled.
`feels_like_c` and `dewpoint_c` are deliberately absent from the tuple; after
gap-filling, `WeatherResult.derive_missing()` computes them from the primary
observation's own temperature, humidity, and wind. Contributing sources are credited
as `[A + B]`.

**Alternatives.** Blending readings (rejected above). Gap-filling everything
(produces the self-contradicting output the incident showed). Computing derived
fields from gap-filled humidity and wind but the primary's temperature (still mixes
observations, since the donor's humidity was measured against the donor's
temperature).

**Tradeoffs.** A missing feels-like shows as unavailable rather than as a borrowed
number, and a sparse primary result can be returned when the chain is exhausted
because sparse beats nothing. Both are deliberate.

**Constraints.** `feels_like_c` and `dewpoint_c` must never enter
`_CURRENT_GAP_FIELDS`. `tests/test_dispatcher.py - TestGapFill` carries two
regression tests pinned to the Yosemite incident, including one asserting derived
fields are never imported. The display half of the same decision lives in
`modules/weather.py - _format_current()`: feels-like is shown whenever it is known,
because the old suppress-when-within-2-degrees rule made "unknown" and "same as the
temperature" indistinguishable.

---

## ADR-011: A fresh module object per load, with no sys.modules entry

**Decision.** `IRCBot.load_module()` builds each module with
`spec_from_file_location` plus `module_from_spec` plus `exec_module`, and never
registers the result in `sys.modules`. Reload is strictly unload then load.

**Context.** Modules are hot-loadable at runtime by an admin (`.load`, `.reload`,
`.reloadall`) and at startup from the `autoload` config key.

**Rationale.** The implementation implies the reason, and the internals
documentation states the consequence chain. Because no `sys.modules` entry exists,
each load re-executes the file from disk, so a source edit to a command module is
picked up by `.reload` without `importlib.reload` and without stale-bytecode
concerns. Nothing module-internal survives: globals, caches, and class objects are
all new.

**Alternatives.** `importlib.reload` requires a `sys.modules` entry and reuses the
existing module object, so module-level state persists across a reload. Restart-only
reloading would make every module edit a full outage.

**Tradeoffs.** The asymmetry is the trap. Imports made *by* a module (`from
modules.base import ...`, `from .geocode import geocode`, any third-party library)
go through the normal import machinery and **are** cached in `sys.modules`.
Re-executing `modules/weather.py` re-runs its import of `modules.geocode`, but the
machinery rebinds to the cached object without re-reading `geocode.py`. So
`.reload weather` refreshes command modules and not helpers; an edit to
`modules/geocode.py`, `modules/units.py`, or `modules/base.py` is invisible until
`.restart` replaces the interpreter with an empty `sys.modules`.

**Constraints.** Nothing may `import modules.<name>` elsewhere: that would create a
second, distinct module object registered in `sys.modules`, and class identity
across the two would differ. Nothing in the codebase does this today; it is a
constraint to preserve. A module that starts a background task owns cancelling it in
`on_unload()`, or the old module object stays alive through that reference.
`unload_module()` deliberately leaves a module fully loaded if `on_unload()` raises,
because half-removing a module that failed to tear down would strand its commands.

---

## ADR-012: Priority queue plus token bucket, not a flat send rate

**Decision.** All outbound traffic passes through one `asyncio.PriorityQueue` of
`(priority, seq, msg)` bounded at 200 entries, drained by a single task with a
token bucket of 5 burst tokens refilling at one per 1.5 s. Priority 0 bypasses the
bucket entirely and is never dropped in favour of a lower priority.

**Context.** IRC servers disconnect clients that flood. A naive global rate limit
applied to every line would also throttle PONG, CAP, NICK, and QUIT.

**Rationale.** Recorded in the `sender.py` module docstring (the priority scheme and
the bucket parameters) and in `Sender._safe_put()`'s docstring for the overflow
policy: "Priority-0 traffic (PONG/CAP/NICK/QUIT) MUST NOT be dropped on overflow:
losing a PONG causes a server ping-timeout disconnect, which produces a reconnect
storm worse than the original overflow." On a full queue the worst-ranked existing
entry is evicted from the heap and counted as a drop, and the priority-0 item is
inserted. The 200-entry bound is tagged `BUG-056` in source: bound the queue to
prevent OOM during disconnects. `seq` is a monotonic counter under `Sender._seq_lk`
that makes the heap a stable FIFO within a priority and keeps the non-comparable
message string out of the tuple comparison. Pinned by
`tests/test_sender.py - TestDrainRateLimiting`.

**Alternatives.** A single flat rate would throttle keepalive traffic and cause the
disconnects it exists to prevent. An unbounded queue trades a disconnect for an OOM.
Dropping the newest item unconditionally on overflow would drop PONGs.

**Tradeoffs.** The token wait is a 50 ms poll loop rather than a computed sleep, so
it wakes up to 30 times per throttled message. Eviction reaches into the private
`asyncio.PriorityQueue._queue` attribute, which is a CPython implementation detail;
the code acknowledges this and degrades to a loud error rather than crashing if it
breaks. The "never drop priority 0" guarantee is really "never drop the oldest
priority 0": a queue full of 200 priority-0 items evicts the highest-seq priority-0
entry. The docstring overstates it.

**Constraints.** Nothing except `Sender` may touch the `StreamWriter`, or flood
control stops being enforced in one place. `enqueue()` must stay thread-safe: it
takes `_seq_lk` for the sequence number and hands the queue mutation to the loop via
`call_soon_threadsafe`, because `asyncio.PriorityQueue` is not thread-safe and
module handlers enqueue from worker threads.

---

## ADR-013: Malformed ISUPPORT returns None, not a degraded table

**Decision.** `protocol.py - parse_isupport_chanmodes()` and
`parse_isupport_prefix()` return `None` for a structurally invalid token. The
caller keeps its current table and logs `event=isupport_malformed`. Both tables are
re-seeded from module-level defaults on every connect.

**Context.** ISUPPORT `CHANMODES` and `PREFIX` drive MODE parameter alignment. Get
them wrong and every parameter after the first shifts.

**Rationale.** `CHANGELOG.md` records the defect this fixed: "A present but
malformed `PREFIX=` stored an empty mode set, which silently ended all MODE-driven
chanop tracking." The `parse_isupport_chanmodes` docstring states the contract
directly: "Returns None if the token is structurally invalid, so the caller can keep
its current table instead of replacing it with a partial one."

The structural check matters more than an emptiness check would. A truncated
`CHANMODES=beI` parses to a perfectly non-empty `{b:A, e:A, I:A}`, so an
"is it empty?" guard accepts it and silently drops `k -> B` and `l -> C`. With `k`
untyped, `parse_mode_changes` consumes no parameter for it, and
`MODE #c +ko sekrit nick` shifts every following parameter so the channel key is
recorded as the operator nick. Symmetrically, a well-formed `PREFIX=()` returns
`(set(), {})`, a real "no membership prefixes" advertisement, which is exactly why
the failure signal had to be `None` and not an empty result.

**Alternatives.** Returning a partial table (the original defect). Returning an
empty result as the error signal (indistinguishable from a legitimate empty
advertisement). Raising (a hostile line would then have to be caught in the read
loop, breaking the totality property of ADR-002).

**Tradeoffs.** A server that advertises a genuinely new but malformed-looking token
keeps the previous table indefinitely, with only a log line to say so. The
`event=isupport_malformed` marker exists precisely so that is greppable when chanop
state looks wrong on one network.

**Constraints.** The tables are per-connection facts. `_connect()` re-seeds both
from `_DEFAULT_CHANMODE_TYPES` and `_DEFAULT_PREFIX_MODES` because a reconnect can
land on a different server via DNS round-robin, failover, or an ircd upgrade, and
the previous server's tables would otherwise govern parameter alignment until a new
005 arrived. Those defaults live at module level because two call sites need them
and a second hand-written copy would drift. Pinned by the "a malformed ISUPPORT
token never wipes the mode tables" test in `tests/run_tests.py`.

---

## ADR-014: Fail-closed validation at startup

**Decision.** Configuration errors that would disable a control terminate the
process at import with an actionable message, rather than being tolerated at
runtime.

**Context.** `config.py` and `botlog.py` both run their validation at import time,
before any network activity. Several config values gate security controls.

**Rationale.** Each guard names its own reason in source.

| Condition | Behavior | Recorded reason |
|---|---|---|
| `config.ini` missing | `SystemExit` | else a bare `KeyError: 'irc'` names nothing |
| empty `command_prefix` | `SystemExit` | every message would become a command |
| cooldowns <= 0 | floored to 1 | a 0 would disable the per-nick limiter |
| unknown hash prefix | `sys.exit(1)` | `verify_password` would raise on every auth |
| invalid mode strings | `sys.exit(1)` | only letters, `+`, `-`, spaces are legal |

"Cooldowns" are `[bot] api_cooldown` and `flood_cooldown`; "mode strings" are
`user_modes`, `oper_modes`, and `oper_snomask`; the hash prefix is the algorithm
tag on `[admin] password_hash`.

An **empty** `password_hash` is deliberately not fatal: authentication is disabled
with a warning, which is the intended first-run state. The cooldown floor is applied
twice, in `config.py` and again in `store.py - RateLimiter.__init__()`, as defence in
depth.

**Alternatives.** Warning and continuing would leave a control silently off. Lazy
validation at first use would surface the error hours later, in the middle of an
incident, from a worker thread.

**Tradeoffs.** Failure quality is inconsistent for equally likely operator errors:
the missing-file and empty-prefix cases get curated `SystemExit` messages, while a
present-but-incomplete `config.ini` produces a raw `KeyError` and a malformed
numeric produces a raw `ValueError` from `int()`. Validation also runs at *import*,
which couples every importer of the module graph to `sys.argv` and to the current
working directory.

**Constraints.** `reload_config()` does **not** re-apply these guards, so a rehash
can load an empty `command_prefix` into the live `cfg`; `IRCBot._cmd_prefix()` then
returns `""` because the key exists and the `CMD_PREFIX` fallback never fires. That
recreates exactly the hazard the import-time guard prevents and is an open finding
(see [internals/config](internals/config.md#findings)). Any new startup guard must
be a real conditional, not an `assert`, so it survives `python -O`.

---

## ADR-015: 0600 permission enforcement for secrets and PII at rest

**Decision.** Every file the bot creates that holds a credential or PII is created
or tightened to 0600 before it becomes visible under its final name. The secrets
file is refused unless its mode is exactly 0600.

**Context.** The process writes `config.ini` (secrets), `users.json` (nick,
hostmask, activity timestamps), `locations.json` (user-supplied location strings),
`shadow_bans.json`, `audit.log` (hostmasks and every privileged action), and
`audit.log.key`.

**Rationale.** The mechanism differs per file but the intent is uniform.
`store.py - Store._write()` chmods the temp file **before** `os.replace` so the
final file is never momentarily world-readable.
`secret_store.py - _atomic_write_text()` opens its temp file with `os.open(...,
0o600)` so the mode applies from the first byte, then re-asserts it after the
replace in case umask or an existing-file replace left it different.
`audit_log.py - _enforce_perms()` re-chmods after each append.
`IRCBot._save_shadow_bans()` uses mkstemp plus chmod plus `os.replace`.
`secret_store.py - perms_ok()` is the read-side gate and fails closed: a value in a
world-readable file is never returned, pinned by
`tests/test_secret_store.py - TestFailClosed`.

**Alternatives.** Relying on umask (not guaranteed, and does not fix an existing
file). Warning instead of refusing (a leaked credential that still works is worse
than a bot that runs keyless and says so). Encryption at rest (adds key management
without a commensurate threat for a single-host bot).

**Tradeoffs.** Read and write refusals are deliberately asymmetric. A read failure
degrades to "secret unset" and the bot runs keyless, but `delete()` raises
`PermissionError` rather than reporting "not found", because an operator removing a
leaked credential must not be told it was already gone. The check is equality, not
"no group or world bits", so a **stricter** 0400 is also refused and silently makes
`get()` return defaults.

:::{warning}
**Known inconsistency: 640 versus 600.**

`botlog.py` warns when `config.ini` is world-readable and advises
`chmod 640 config.ini`, while `secret_store.py - perms_ok()` requires exactly 0600.
An operator who follows the log's own advice makes the `[secrets]` section
unreadable and the bot runs keyless with only an error line. Verified against both
sources.
:::

**Constraints.** `Store._write()`'s `.bak` copy is currently exempt and takes
umask-default permissions (see ADR-007). Windows is out of scope for all of this:
POSIX mode bits are advisory there, `perms_ok()` returns true with reason
`"windows (acl-based)"`, and NTFS ACLs are the operator's responsibility.

---

## ADR-016: PID lockfile with liveness probing, not flock

**Decision.** A single-instance guard writes `pid|start_time|hostname` to
`./internets.pid`, created atomically with `os.open(..., O_CREAT | O_EXCL |
O_WRONLY)`. A pre-existing lockfile is resolved by probing the recorded PID.

**Context.** The three state files are written tmp-and-rename by a background
thread. Two concurrent bot processes in the same directory would silently clobber
each other's mid-flight changes. `audit_log.py` has no cross-process locking of its
own and relies on this guard transitively.

**Rationale.** Stated in the `process_lock.py` module docstring and in
`internets.py - _entry()`. The payload format is chosen for diagnosability: the
lockfile names its holder, so an operator can see who has it. `os.kill(pid, 0)`
performs the existence and permission check without delivering a signal. The
decision tree is deliberately conservative in both directions:

| Observation | Action | Reason |
|---|---|---|
| PID alive, same host | refuse (`LockHeld`) | a live holder |
| `PermissionError` from the probe | treat as alive, refuse | another user's live process |
| PID dead | unlink, proceed | crash recovery without manual cleanup |
| different hostname | refuse without probing | a foreign PID table cannot be probed |
| liveness unknowable | warn, unlink, proceed | better a benign race than refuse-to-start |
| corrupt or unreadable file | warn, unlink, proceed | untrusted input, not a holder |

**Alternatives.** `flock`/`fcntl` would close the stale-reclaim race below but
carries no diagnosable payload and behaves differently across platforms and on NFS.
No guard at all is the failure this exists to prevent.

**Tradeoffs.** A reused PID is indistinguishable from a live holder, so the lock is
refused and recovery is manual deletion; the recorded `start_time` is the lock
acquisition wall clock, not the process start time, so it cannot disambiguate.
The stale-reclaim path (examine, unlink, `O_EXCL` create) is not atomic: two
processes started simultaneously over the same stale file can interleave so that
the second unlinks the first's fresh lockfile and both acquire. The window is
microseconds and only reachable after a crash left a stale file. `release()`
re-reads the file and unlinks only if the recorded PID is still ours, which contains
the damage at exit but not during the run.

**Constraints.** The restart path must release the lock **before** `os.execv`.
`execv` preserves the PID and the `with` block's `__exit__` never runs after a
successful exec, so without the explicit release the new image reads the old
lockfile, probes its own live PID, and refuses to start. `internets.py - _main()`
does this and logs `restart_lock_release_failed` if it cannot; the exec proceeds
anyway and the new image would then self-deadlock, which is an accepted and visible
risk. The path is resolved at `acquire()` rather than construction so it honors the
working directory at startup, matching the cwd-relative state files it protects.

---

## Related reading

- System-level view of how these decisions compose: [architecture](architecture.md)
- Per-file implementation and per-file findings: [internals/index](internals/index.md)
- Threat model the security decisions serve: [security-model](security-model.md)
- Release history that substantiates the dated decisions: [changelog](changelog.md)
