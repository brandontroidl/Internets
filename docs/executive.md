# Executive Binder

This section answers: what is this system, why does it exist, what are the
load-bearing design choices, and what must never change. Aimed at a CTO, Staff
Engineer, or incoming principal who needs the strategic picture before reading
code.

## What Internets is

Internets is a modular IRC bot and multi-provider weather aggregator built on
Python's asyncio event loop and the RFC 2812 IRC protocol. It provides 72
command modules spanning worldwide weather (current, forecast, hourly, nowcast,
air quality, UV, pollen, astronomy, alerts, wildfire, space weather, marine,
tides, historical), stock/crypto/FX prices, movie and music lookups, dictionary
and reference tools, a large developer/encoding/network/security toolkit, IP
geolocation and reputation, curated science/infosec news feeds, and stateful
IRC-native tools (seen, tell, remind, notes). It runs as a single long-lived
process on a single host, connecting to one IRC network.

## Why it exists

1. A single-binary IRC service layer that delivers real-world data (weather,
   financial, reference, security) to IRC channels without requiring users to
   leave their client.
2. A weather aggregation engine that queries 32 upstream providers through a
   capability-based dispatcher with accuracy-ranked fallback, per-provider
   health tracking, circuit breakers, and cross-provider gap-filling, so a user
   gets the best available answer from the first working source rather than a
   single point of failure.
3. A platform for IRC-native stateful services (reminders, offline messages,
   notes, user tracking with opt-out privacy controls) that persist across
   restarts.

## Users

- IRC channel members who invoke commands (`.weather`, `.ip`, `.stock`, etc.)
- One or more bot operators who authenticate via `.auth` and manage modules,
  configuration, and the process lifecycle

## Core design philosophies

### 1. One process, one event loop, modules are guests

The entire bot is a single asyncio event loop. The core owns the IRC state
machine, command dispatch, connection lifecycle, and shutdown orchestration.
Everything else is a module under `modules/`. A module declares commands, the
core routes invocations to them. Blocking I/O (HTTP, hashing, disk) runs under
`asyncio.to_thread()` so the loop is never blocked. Cross-thread state is
guarded by `threading.Lock` (not `asyncio.Lock`), making the design
correct under both GIL and free-threaded Python.

### 2. Plugin architecture with hot-reload

Modules load, unload, and reload without restarting the bot. Each
`.reload <module>` re-executes the file fresh from disk via `exec_module`
(deliberately not `importlib.reload`). Helper modules (`geocode`, `units`,
`_netsafe`, `weather_providers/`) are cached in `sys.modules` and require a
full `.restart` (which does `os.execv`) to refresh. This is an intentional
trade-off: command modules are the hot-edit surface, helpers are the stable
foundation.

### 3. Weather: capability-based dispatch over a provider fleet

The weather subsystem is not a wrapper around one API. It is a dispatch engine
over 32 provider packages. Each provider declares capabilities by defining
methods (`get_weather`, `get_forecast`, `get_alerts`, ...). The dispatcher
auto-discovers capabilities via `hasattr`, ranks providers by static accuracy
(model science), live health (EMA success rate, latency), and config-defined
registration order. On a request, it walks the sorted chain until one returns
real data, bounded by a 45s chain budget and a 30s per-call budget. For
current conditions, it gap-fills sparse results from secondary providers
(bounded to 3 contributors). A per-provider circuit breaker (5 consecutive
failures in 60s) removes a failing provider from the chain until a 60s
cooldown and successful probe. The accuracy ranking deliberately dominates
health so a more-accurate but slower provider wins until its breaker actually
trips.

### 4. Security by default, fail-closed everywhere

- TLS 1.3 minimum on the IRC connection (opt-down to 1.2 requires an
  explicit env var and logs a warning).
- Credentials are never sent on a plaintext connection (the `_tls_or_refuse`
  gate).
- Admin auth re-verifies the hostmask on every command, not just at login.
  Sessions are dropped (not migrated) on NICK change or QUIT.
- SSRF defense uses thread-local DNS pinning to close the resolve/connect
  TOCTOU, not an IP-literal adapter (which breaks TLS SNI).
- All outbound HTTP streams the body and caps it before decode/parse (256 KB
  default, 1 MB for providers). No `r.json()` anywhere.
- Config files are 0600. The secret store fails closed on looser permissions.
- State files use checksummed v2 envelopes; a corrupt file is quarantined,
  never silently overwritten.
- The process refuses to start as root unless explicitly overridden.

### 5. Two-tier secrets

Credentials resolve through: (1) `INTERNETS_<NAME>` env var, (2)
`config.ini[secrets]` section (0600 file perms required), (3) empty default.
Template placeholders (`changeme`, `your-key-here`, etc.) are filtered out
at both tiers. Outbound credentials (NickServ, API keys) are stored
reversibly (not hashed) because they must be transmitted on the wire;
the admin password is one-way hashed (scrypt/bcrypt/argon2) because it is
only verified locally.

### 6. Atomic persistence with quarantine

The three state files (locations, channels, users) use a v2 JSON envelope
with a SHA-256 checksum. Writes go to a temp file, `chmod 0600`, then
`os.replace` (atomic on POSIX) with a one-deep `.bak` backup. On read, a
bad checksum, corrupt JSON, or type mismatch quarantines the file to
`<name>.corrupt.<timestamp>` rather than silently resetting to empty.
This prevents a truncated power-loss file from being overwritten by the
next flush and losing all saved locations, channel state, and privacy
opt-out flags.

### 7. Tamper-evident audit logging

Every privileged admin action is recorded in an append-only,
HMAC-SHA256-chained log (`audit.log`). Each record's hash incorporates
the previous record's hash, so editing, reordering, or deleting any
non-tail record breaks the chain. The HMAC key lives in a separate 0600
sidecar file so the algorithm (which is in the source) alone cannot forge
entries. Tail truncation by an attacker with both files is the acknowledged
limitation; detecting that requires an external append-only sink.

## What should never change

These are the structural invariants. Relaxing any of them reintroduces a
class of failure that was observed in production.

1. **The single-threaded event loop model.** All IRC I/O and state mutation
   on one thread; blocking work off-loop via `to_thread`. The `threading.Lock`
   discipline around `_authed`, `_nick_hosts`, `_chanops`, and the three
   `Store` datasets assumes this model.

2. **The fail-closed admin hostmask binding.** `is_admin()` re-derives
   authorization from the current hostmask on every call. An `"unknown"` or
   missing hostmask denies. Removing or weakening this check reintroduces
   the nick-only session bug where a nick-grabber inherits an authed session.

3. **The netsafe DNS-pinning SSRF defense.** Thread-local `getaddrinfo`
   pinning with per-redirect-hop re-validation. Do not replace with an
   IP-literal adapter (breaks TLS SNI under requests 2.34 / urllib3 2.7).
   Do not simplify to resolve-then-connect-by-name (TOCTOU: DNS can rebind
   between check and connect).

4. **The derived-field invariant in weather gap-fill.** `feels_like_c` and
   `dewpoint_c` are excluded from cross-provider gap-fill because they are
   derived from a specific temperature. Importing them from a provider that
   measured a different temperature produces a self-contradictory output
   (observed: 24.2C with 11.3C feels-like at Yosemite, because NWS and
   Open-Meteo were reading points 10.4C apart).

5. **The store's quarantine-on-bad-read behavior.** A corrupt state file is
   renamed aside, not silently overwritten. Removing this loses the only
   copy of user locations, channel state, and privacy opt-out flags on the
   next flush cycle.

6. **The audit log HMAC chain.** Do not downgrade to plain SHA-256 (the
   pre-3.0.0 scheme). An attacker who copies only the log file can recompute
   a plain-SHA chain and forge entries. The HMAC key sidecar prevents this.

7. **The `_tls_or_refuse` credential gate.** The bot must never send
   NickServ, SASL, server, or OPER passwords on a plaintext connection.
   This gate is the single enforcement point.

8. **The outbound HTTP size cap discipline.** Every JSON fetch streams and
   caps before parse. A compromised upstream returning a multi-GB JSON bomb
   cannot OOM the process. The `+1`-byte pattern (`read(max+1)`, then
   `> max`) is the specific mechanism.

9. **The process lock.** Two instances racing on the JSON state files
   corrupt them. The `O_CREAT | O_EXCL` lockfile with stale-PID detection
   prevents this. `os.execv` preserves the PID, so the lock must be released
   before re-exec.

## Major architectural decisions

Each decision below is documented in detail in `docs/design-decisions.md`.
This is the summary.

| Decision | Why | Consequence |
|---|---|---|
| asyncio, not threads or multiprocessing | IRC is I/O-bound; one loop handles thousands of lines/sec; modules offload blocking I/O to threads | All state mutation on one thread; `threading.Lock` for cross-thread reads |
| Custom IRC parser, no library | No IRC library in 2024 handles IRCv3 caps, SASL PLAIN, server-time tags, and the ISUPPORT mode-type system correctly | 5 total parser functions in `protocol.py`, independently testable |
| `exec_module` not `importlib.reload` | Reload must re-read the file from disk; `importlib.reload` has subtle side effects with `sys.modules` caching | Helper modules are NOT refreshed by `.reload`; `.restart` required |
| Thread-local DNS pinning for SSRF | IP-literal adapters break TLS SNI under current requests/urllib3 | Global `getaddrinfo` wrapper, thread-local map, no-op for other threads |
| Checksummed v2 store envelope | A bare JSON file truncated by power loss is silently overwritten by the next flush | Quarantine preserves the corrupt copy for manual recovery |
| HMAC-chained audit log | Plain SHA-256 can be recomputed by anyone who reads the algorithm | Separate 0600 key sidecar; pre-3.0.0 SHA-256 records still verify |
| Provider accuracy dominates health in dispatch | A slower but more accurate provider should win until its breaker trips | The dispatcher never prefers a fast-but-inaccurate provider over a slow-but-accurate one at the same breaker state |
| No-data is not failure for weather providers | A US-only provider queried about Tokyo should not trip its circuit breaker | `None` return vs exception; the dispatcher distinguishes and records accordingly |
| scrypt as CLI default, argon2 as recommendation | Changing the CLI default would surprise operators; argon2 is the OWASP first-choice | `hashpw.py` defaults to scrypt but prints a recommendation for argon2 |

## Platform support

Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2. CI tests
Python 3.10 through 3.14. The only hard POSIX dependency is `os.chmod` for
0600 file permissions; Windows relies on filesystem ACLs instead.

## License

ISC.
