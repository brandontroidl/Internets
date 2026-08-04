# Design Decision Records

Each entry documents a significant architectural or engineering choice, the
alternatives considered, the reasoning, and the consequences. These are not
exhaustive post-hoc documentation of every line of code; they capture the
decisions that would be non-obvious or counter-intuitive to an incoming
engineer.

---

## ADR-001: Single asyncio event loop, not threads or multiprocessing

**Context.** An IRC bot is I/O-bound: it spends most of its time waiting for
server lines and HTTP responses. CPU-bound work (password hashing, large JSON
parsing) is rare and brief.

**Decision.** One asyncio event loop owns all IRC I/O, state mutation, and
command dispatch. Blocking work (HTTP via `requests`, password hashing, disk
writes) runs under `asyncio.to_thread()`. Cross-thread reads of shared state
(`_authed`, `_nick_hosts`, `_chanops`, the three `Store` datasets) are guarded
by `threading.Lock` (not `asyncio.Lock`) so they work from both the loop thread
and `to_thread` workers.

**Alternatives.** (1) Thread-per-connection: too many shared-state races for
the complexity budget. (2) Multiprocessing: the entire module system depends on
sharing in-process state (`_commands`, `_modules`, `Store` in-memory dicts);
IPC would dominate. (3) `asyncio.Lock`: cannot be acquired from a synchronous
`to_thread` worker without being on the loop.

**Consequences.** All state mutation happens on one thread, simplifying
reasoning. `threading.Lock` is the correct primitive because `to_thread`
workers call `is_admin()`, `is_chanop()`, and `Store` accessors from off-loop.
The design holds under free-threaded/GIL-disabled Python without modification.
The 50-concurrent-task cap (`_MAX_TASKS`) and 60s per-command timeout
(`_CMD_TIMEOUT`) bound resource consumption within the single loop.

---

## ADR-002: Custom IRC parser, no library

**Context.** Python IRC libraries (irc, pydle, irc3) in 2024 handle basic
RFC 2812 but do not correctly handle: IRCv3 message tags (`@time=...`), SASL
PLAIN negotiation with cap-end fallback, the full ISUPPORT `CHANMODES` /
`PREFIX` mode-type system for parameter alignment, `chghost` / `extended-join`
/ `account-notify`, or server-time-tagged PING lines (which a naive parser
misses, causing ping-timeout disconnects).

**Decision.** Five pure functions in `protocol.py` (no state, no I/O, no
imports beyond `base64` and `re`): `strip_tags`, `parse_isupport_chanmodes`,
`parse_isupport_prefix`, `parse_mode_changes`, `parse_names_entry`, plus
`sasl_plain_payload`. All wire-facing parsers are total (return a value for
any `str`, never raise). The IRC state machine lives in `internets.py`'s
`_process` method with precompiled regexes.

**Alternatives.** Adopting an IRC library would trade parser maintenance for
library maintenance and would not cover the features above. Forking a library
would be worse: full library baggage with the same maintenance burden.

**Consequences.** The parser is small (~126 lines), pure, independently
testable (`tests/test_protocol.py`), and trivially extended. The cost is that
every new IRC feature (e.g., a new cap) requires manual implementation rather
than a library upgrade. The wire-facing parsers being total means a malformed
server line degrades to a partial result rather than crashing the read loop.

---

## ADR-003: Thread-local DNS pinning for SSRF, not IP-literal adapters

**Context.** Modules that fetch user-influenceable URLs (probe, scinews article
reader, url shortener) need SSRF protection. The standard approach is
resolve-then-connect-by-IP, but this has a TOCTOU window: DNS can rebind the
hostname to an internal IP between the validation resolve and urllib3's own
re-resolution at connect time. An IP-literal adapter (connecting to `10.0.0.1`
directly) breaks TLS SNI because under `requests 2.34` / `urllib3 2.7` the
`HTTPAdapter` `server_hostname` override does not propagate to the TLS
handshake.

**Decision.** `_netsafe.py` monkey-patches `socket.getaddrinfo` once at import
with a thread-local wrapper. During a `safe_open` call, the wrapper returns the
validated IP for the host being fetched, forcing urllib3 to connect to exactly
that IP. The real hostname stays intact in the `Host` header, SNI, and
certificate verification. The patch is a no-op for every other thread and code
path. Each redirect hop is re-resolved, re-validated, and re-pinned.

**Alternatives.** (1) IP-literal adapter: breaks TLS SNI under current
requests/urllib3 (handshake failure). (2) Resolve-then-connect-by-name: TOCTOU
open. (3) Custom transport adapter with `server_hostname`: the override does
not propagate in the versions pinned. (4) aiohttp's TCPConnector with local
addr: different API surface, only works with aiohttp.

**Consequences.** SSRF is fully closed (including across redirect hops) without
breaking TLS. The global `getaddrinfo` wrapper is the most invasive part; it is
idempotent, thread-local-scoped, and tested. Do not simplify to an IP-literal
adapter without verifying that SNI propagation is fixed in the pinned
requests/urllib3 versions.

---

## ADR-004: Capability-based weather dispatch with accuracy-dominant ranking

**Context.** A weather bot that depends on a single API is a single point of
failure. Multiple providers cover different capabilities (NWS has US alerts,
Stormglass has marine, Open-Meteo has pollen) and have different accuracy for
the same capability (NWS NDFD with human forecaster input is more accurate for
US forecasts than a GFS-derivative).

**Decision.** The dispatcher auto-discovers each provider's capabilities via
`hasattr` on method names (`get_weather`, `get_alerts`, etc.). For each
request, it sorts eligible providers by a 3-key tuple: (1) static reliability
rank (model science, encoded in `DEFAULT_RELIABILITY`), (2) live health score
(EMA of success rate, latency, rate-limit count), (3) config-defined
registration order. The static rank is the dominant key, so a more-accurate
provider always wins over a faster but less-accurate one, until its circuit
breaker trips. A per-provider circuit breaker (5 consecutive failures in 60s)
removes a failing provider from the chain for a 60s cooldown.

**Alternatives.** (1) Health-dominant ranking: a fast-but-inaccurate provider
would out-rank a slow-but-accurate one, degrading answer quality to optimize
latency. (2) Manual provider selection: doesn't scale to 14 capabilities x 32
providers; users would need to know which provider covers which capability.
(3) Round-robin: no accuracy awareness at all.

**Consequences.** The best available answer comes from the most accurate
source that is currently healthy. Adding a new provider requires only
defining the methods and a factory; the dispatcher discovers capabilities
automatically. The ranking rationale for each capability is documented in
`_dispatch.py:50`. The trade-off is that a slow but healthy provider holds
its rank slot, potentially adding latency; the per-call budget (30s) and
chain budget (45s) bound this.

---

## ADR-005: Two-tier secret store with env-var override

**Context.** The bot needs NickServ passwords, API keys, and contact
identifiers at runtime. These must be reversible (the bot sends them on the
wire), so hashing is not an option. OS keyring support was removed in 3.0.0
because headless deployments have no usable keyring backend, and the keyring
package pulled ~10 transitive dependencies (jeepney, secretstorage, jaraco-*).

**Decision.** `secret_store.py` resolves secrets through: (1) `INTERNETS_<NAME>`
env var, (2) `config.ini [secrets]` section. The file backend requires exactly
0600 POSIX permissions and fails closed (refuses to read) on looser perms.
Template placeholders (`changeme`, `your-key-here`, etc.) are filtered at both
tiers. The `[secrets]` section lives in the same `config.ini` file as
non-secret settings, which is 0600 and gitignored.

**Alternatives.** (1) Separate `secrets.ini`: adds a second file to manage,
back up, and deploy; the merge was done in v3.0.0. (2) OS keyring: removed for
the dependency and headless-backend reasons above. (3) HashiCorp Vault or
similar: out of scope for a single-host IRC bot. (4) Encrypted file backend:
adds key-management complexity without a commensurate threat; the file is
already 0600 and never committed.

**Consequences.** Deployment is one file (`config.ini`, 0600) plus optional env
vars. The env tier lets container deployments inject secrets without file
mounts. The `_secret_or_cfg` fallback chain in `config.py` preserves
backward compatibility with pre-2.5 installs that stored plaintext in `[irc]`.
The `migrate` CLI command moves legacy plaintext into `[secrets]` and scrubs
the source, with a mandatory rotate-everything warning because the old values
are in git history.

---

## ADR-006: HMAC-chained audit log, not plain SHA-256

**Context.** The pre-3.0.0 audit log used plain SHA-256 chaining. Anyone who
reads the algorithm (which is in the source code) and obtains a copy of
`audit.log` (a backup, an accidental commit) can recompute the chain from
scratch and forge entries that verify correctly.

**Decision.** Each record's hash is `HMAC-SHA256(key, prev_hash || ts || actor
|| host || action || args)` with NUL-byte field separators. The 32-byte HMAC
key lives in a separate 0600 sidecar file (`audit.log.key`). Verification
requires both the log and the key. Pre-3.0.0 records (no `v` field) are
backward-compatible verified using the legacy SHA-256 path.

**Alternatives.** (1) Keep plain SHA-256: forgeable by anyone with the log.
(2) GPG signing per record: heavyweight, key-management overhead. (3) External
append-only log: the right long-term answer but out of scope for a single-host
bot. (4) No chaining at all: no tamper detection.

**Consequences.** Tampering with any non-tail record is detectable via
`.audit verify`. The acknowledged limitation is pure tail truncation by an
attacker who has write access to both files; detecting that requires an
external sink (remote syslog). The key is generated once and never
regenerated unless corrupt (corrupt key is backed up to `.key.bad`, not
overwritten); a transient FS error that makes the key unreadable raises
`RuntimeError` rather than silently regenerating (which would void the chain).

---

## ADR-007: Checksummed v2 store envelope with quarantine-on-corruption

**Context.** The three state files (locations, channels, users) are written
by a background flush thread every 30s. A power loss, full disk, or OOM
during write can truncate the file. Without protection, the next startup
loads the truncated JSON (or fails to parse it and resets to empty), and
the next flush overwrites the only copy.

**Decision.** On-disk format: `{"schema": 2, "checksum": "<sha256>", "data":
<payload>}`. The checksum is over canonical JSON of `data` (sorted keys,
compact separators) so dict ordering and Python version differences don't
affect it. On read, a bad checksum, unknown schema, or type mismatch
triggers `_quarantine`: the file is renamed to `<name>.corrupt.<timestamp>`
and the dataset starts from default. Writes go to a temp file, `chmod 0600`
before rename, with a one-deep `.bak` backup of the prior good file.

**Alternatives.** (1) No envelope: silent data loss on corruption. (2)
SQLite: adds a dependency and a different corruption-recovery model; overkill
for three small JSON files. (3) WAL/journaling: complexity not justified by
the data volume.

**Consequences.** Corruption is detected on startup, the corrupt file is
preserved for manual recovery, and the bot starts with empty state rather than
crashing. The `.bak` file provides one-deep rollback. The trade-off is that a
quarantine event loses the most recent state (up to 30s of tracking data plus
saved locations). The `_prune_users` exclusion of `opted_out=True` records from
age-based pruning preserves privacy preferences across inactivity windows.

---

## ADR-008: Daemon thread for the interactive console, not asyncio.to_thread

**Context.** The interactive console reads stdin via `input()`, which parks its
thread on a blocking `read(0)` syscall that nothing short of process death
interrupts. `asyncio.to_thread` runs the callable on the default executor,
whose workers are non-daemon threads. `asyncio.run()`'s cleanup calls
`shutdown_default_executor()`, which waits for all non-daemon workers to return.
An `input()`-blocked worker never returns, so the entire process hangs on
shutdown.

**Decision.** The console dispatch loop runs on an explicit
`threading.Thread(daemon=True)`. A daemon thread cannot hold up interpreter
shutdown, so cleanup completes even if the thread never returns from `input()`.
`_main` closes `sys.stdin` during shutdown to unblock the pending `input()`
call (which raises `EOFError`, caught cleanly). The thread signals completion
via `loop.call_soon_threadsafe(done.set)`.

**Alternatives.** (1) `asyncio.to_thread`: hangs on shutdown (observed).
(2) Non-blocking stdin via `select`/`poll`: platform-dependent, complex on
Windows. (3) Skip the console entirely: loses the unauthenticated local admin
surface for debug/status/shutdown.

**Consequences.** The console works on all platforms, shuts down cleanly, and
provides admin-equivalent capability without IRC auth to anyone with stdin
access. The security gate is `should_skip_console()` (TTY check + `--no-console`
flag). Do not convert the thread to `asyncio.to_thread`; the shutdown hang is
the specific failure this decision prevents.

---

## ADR-009: No-data vs failure distinction for weather providers

**Context.** NWS serves US points only. When queried about Tokyo, it returns
HTTP 404 ("Data Unavailable For Requested Point"). The pre-fix dispatcher
treated this as a provider failure and called `record_failure()`, which
accumulated toward tripping the circuit breaker. Enough non-US queries would
trip the breaker and degrade US alerts to a less authoritative provider.

**Decision.** A provider that does not cover a point returns `None` (not an
exception). The dispatcher distinguishes this from a real failure: `None`
skips to the next provider without calling `record_failure()` or
`record_success()`. Exceptions route into the failure/breaker path.
`_scope.py` in the NWS package maps HTTP 400/404 (which for validated
coordinates mean "out of coverage") to an `OutOfCoverage` exception caught
by `none_if_uncovered()` and converted to `None`. Other statuses (401, 403,
429, 5xx) still propagate as real failures.

**Alternatives.** (1) Hardcoded bounding box: drifts when NWS changes
coverage, doesn't handle Aleutian antimeridian wrap. (2) Treat all non-200
as failures: degrades US service on international traffic. (3) Separate
"coverage check" API call: doubles request count.

**Consequences.** Every regional provider (NWS, AirNow, ECCC, NOAA CO-OPS,
Pollen.com) can implement the same pattern. The dispatcher's health tracking
reflects real reliability, not coverage mismatches. The pattern to follow when
adding a new regional provider is documented in `providers.md` section 4.9.

---

## ADR-010: Derived-field invariant in cross-provider weather gap-fill

**Context.** NWS station observations routinely null secondary fields
(dewpoint, pressure, visibility). The dispatcher gap-fills these from the
next provider in the chain. Before this invariant was established, it also
gap-filled `feels_like_c` and `dewpoint_c`. Observed: `.w yosemite national
park` printed `Temperature 24.2C :: Feels like 11.3C` because NWS read a
high-elevation station at 24.22C with no feels-like, and Open-Meteo's model
grid read 13.8C at a different point and contributed its feels-like of 11.9C.
The two providers were describing points 10.4C apart.

**Decision.** `feels_like_c` and `dewpoint_c` are excluded from
`_CURRENT_GAP_FIELDS`. A derived field must come from the same observation as
the temperature printed beside it. Providers populate them natively or leave
them `None`. After gap-filling completes, `derive_missing()` computes any
still-missing derived fields from the primary result's own temperature,
humidity, and wind.

**Alternatives.** (1) Gap-fill everything: produces self-contradictory output
when providers disagree on the base temperature. (2) Compute derived fields
from the gap-filled humidity/wind: still uses the primary's temperature, which
may differ from the donor provider's temperature that its humidity was measured
against.

**Consequences.** A missing feels-like shows as `N/A` rather than a borrowed
value that contradicts the displayed temperature. The invariant is tested by
`tests/test_dispatcher.py::TestGapFill` with two regression tests pinned to
the Yosemite incident.
