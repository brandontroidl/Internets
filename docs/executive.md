# Executive summary

What the system is, how big it is, how it is built, how it is run, how it is
defended, and what is currently wrong with it. Written for someone deciding
whether to own, fund, deploy, or audit this codebase, before reading any of it.

Every count below was verified programmatically against the source on
2026-08-15, not taken from prose. Every risk in
[Current known risks](#exec-risks) was verified against the source or reproduced;
none are speculative.

## What it is

Internets is a modular IRC bot and multi-provider weather aggregator. It is one
long-lived Python process on one host, connected to one IRC network, answering
prefixed commands (default `.`) in channels and private messages. Version 5.0.0,
Python 3.10 or newer, ISC licensed.

It does three distinguishable things:

1. **A command service for IRC.** Weather, air quality, UV, pollen, alerts,
   tides, space weather, and wildfire lookups; stock, crypto, and FX quotes;
   dictionary, translation, scholarly and reference lookups; IP geolocation and
   reputation; DNS, TLS, and network calculation tools; science, security, and
   space news feeds; a calculator and unit/physics toolkit; and a set of
   entertainment commands.
2. **A weather aggregation engine.** 32 upstream providers behind one
   capability-based dispatcher with accuracy-ranked fallback, per-provider health
   tracking, circuit breakers, quota accounting, and bounded cross-provider
   gap-filling.
3. **Stateful IRC-native services.** Saved locations, seen tracking, offline
   messages, notes, reminders, channel management, and user-facing privacy
   controls (opt-out and erasure), all persisted across restarts.

There is no web UI, no database, no multi-tenancy, and no network listener except
an optional, unauthenticated Prometheus metrics exporter that is off by default.

## Scale

| Dimension | Count |
|---|---|
| Command modules | 70 loadable, 69 registering at least one command |
| Primary module commands | 165 (plus aliases) |
| Core commands | 4 public, 23 admin |
| Weather providers | 32 (20 key-gated, 12 keyless) |
| Weather capabilities | 14 dispatchable, 13 result types |
| Test files | 40 pytest files, plus a standalone `tests/run_tests.py` |
| Tests passing | 1738 passed / 3 skipped (pytest); 213 passed (`run_tests.py`) |

Notes on those numbers, because two of them are easy to misread:

- `modules/` holds 75 `.py` files. Five register no commands: `__init__`, `base`,
  `geocode`, `units`, and `_netsafe` are the module API and shared helpers. Of the
  70 loadable modules, `linktitle` registers zero commands and runs entirely from
  the raw-line fanout, which is why the command-registering count is 69.
  `scripts/gen-command-reference.py --check` regenerates and drift-checks the
  inventory; treat it as the source of truth over any prose count.
- Coverage is measured **core-only**. `pyproject.toml` `[tool.coverage.run]`
  omits `modules/*`, `weather_providers/*`, `internets.py`, and `console.py`, so
  the 75 percent `fail_under` gate covers the core files, not the bulk of the
  code. A large minority of modules have no behavioral test file at all; the
  tests review counted 44. See [testing.md](testing.md).

## Architecture in one paragraph

A single asyncio event loop owns the IRC socket, the line parser, the IRC state
machine, command dispatch, and shutdown orchestration
(`internets.py - IRCBot`); everything user-facing is a module under `modules/`
that declares a command-to-coroutine mapping and is loaded from a file path at
runtime rather than imported as a package
(`internets.py - IRCBot.load_module()`), so command modules hot-reload while
helpers and providers do not. Blocking work - HTTP, password hashing, disk
writes - runs under `asyncio.to_thread()`, and cross-thread state is guarded by
`threading.Lock` rather than `asyncio.Lock`, which keeps the model correct under
both GIL and free-threaded builds. Outbound IRC traffic passes a priority queue
with a token-bucket rate limiter (`sender.py`); state lives in four JSON files
with checksum envelopes, atomic replace, and quarantine on a bad read
(`store.py - Store`); privileged actions append to an HMAC-chained audit log
(`audit_log.py - AuditLog`); and the weather subsystem is a separate dispatch
layer (`weather_providers/_dispatch.py - Dispatcher`) that ranks 32 providers per
capability and walks the chain until one returns data. Details:
[architecture.md](architecture.md), then [internals/](internals/index.md).

## Operational posture

- **One process, one host, one network.** No clustering. The
  `O_CREAT | O_EXCL` lockfile in `process_lock.py - ProcessLock` exists
  specifically to make a second instance impossible, because two writers would
  clobber the JSON state.
- **Runs unprivileged by design.** `internets.py - _entry()` refuses to start as
  root unless `INTERNETS_ALLOW_ROOT=1` is set, and logs the override on every
  start.
- **The deployment directory is the deployment.** Config, lock, state, audit log,
  and bot log all resolve against the working directory, so a service unit
  without `WorkingDirectory` set addresses the wrong deployment.
  See [deployment.md](deployment.md).
- **Three refresh scopes, deliberately distinct.** `.reload` for command modules,
  `.rehash`/SIGHUP for config values read at use time, `.restart` (re-exec, PID
  preserved) for everything else. Picking the wrong one succeeds quietly and
  leaves stale code running. See [operations.md](operations.md#ops-refresh).
- **Observability is thin but sufficient.** A rotating log with per-subsystem
  debug control, four admin health commands (`.health`, `.stats`, `.audit`,
  `.providers`), and an optional Prometheus exporter of which four metrics are
  live and six are registered but never updated. See
  [metrics-and-observability.md](metrics-and-observability.md).
- **Recovery is file-level.** Stop the bot, restore the directory, start it.
  Worst-case loss on a hard kill is about 30 seconds of store mutations. There is
  no `fsync` anywhere in the codebase, verified, so durability stops at the
  kernel page cache.

## Security posture

The design is fail-closed at each boundary. In summary, with the detail behind
each pointer:

| Control | Mechanism | Detail |
|---|---|---|
| Transport | TLS 1.3 floor; downgrade to 1.2 needs `INTERNETS_ALLOW_TLS12=1` and logs a warning (`internets.py - IRCBot._connect()`) | [security-model.md](security-model.md) |
| Credential exposure | `internets.py - IRCBot._tls_or_refuse()` blocks sending any password on a plaintext link | [security-model.md](security-model.md) |
| Admin authorization | `internets.py - IRCBot.is_admin()` re-derives authority from the current hostmask on every command; an unknown or missing hostmask denies | [administration.md](administration.md) |
| Password storage | scrypt / bcrypt / argon2 with self-describing parameters; an unrecognized prefix exits at startup (`botlog.py - _validate_hash()`) | [internals/hashpw.md](internals/hashpw.md) |
| Outbound request safety | Thread-local DNS pinning re-validated per redirect hop (`modules/_netsafe.py`), closing the resolve/connect TOCTOU without breaking TLS SNI | [security-model.md](security-model.md) |
| Response handling | Every JSON fetch streams and caps the body before parse; no bare `r.json()` | [internals/modules/base.md](internals/modules/base.md) |
| Secrets | Environment variable, then `config.ini[secrets]` read only at mode exactly 0600, failing closed (`secret_store.py - perms_ok()`) | [configuration.md](configuration.md) |
| State integrity | Checksum envelope, atomic replace, quarantine instead of reset (`store.py - Store._read()`) | [state-and-persistence.md](state-and-persistence.md) |
| Tamper evidence | HMAC-SHA256 chained audit log with a separate 0600 key sidecar (`audit_log.py - AuditLog`) | [logging-and-auditing.md](logging-and-auditing.md) |
| Privilege | Refuses to run as root; no listener by default | [deployment.md](deployment.md) |

Accepted limitations, stated rather than defended: the metrics exporter has no
authentication of its own; the interactive console is admin-equivalent without
authentication to anyone holding the process's stdin; audit tail-truncation by
someone holding both the log and its key is undetectable without an external
append-only sink; and the log itself records user locations and browsed URLs that
the `.forgetme` erasure path cannot reach.

(exec-risks)=
## Current known risks

Seven verified defects are open. They are ordered by impact here; none has been
fixed, because each is a behavior change that belongs to the owner rather than to
a documentation pass. Full evidence for all of them, plus a long tail of
lower-severity findings, is in
[RECONSTRUCTION-LEDGER.md](../RECONSTRUCTION-LEDGER.md).

| # | Risk | Impact |
|---|---|---|
| 1 | Finance API keys published to the channel | Credential disclosure to every channel member |
| 2 | Provider fallback disabled for 11 of 13 result types | Severe-weather alerts silently suppressed |
| 3 | `.isprime` blocks the event loop | Any user can hang the whole bot |
| 4 | `privacy` absent from the shipped autoload | Erasure and opt-out unavailable by default |
| 5 | Audit chain verifies legacy records unkeyed | Tamper evidence defeatable by a local writer |
| 6 | Startup advises `chmod 640`, secrets require 0600 | Following the bot's own advice disables all secrets |
| 7 | `requirements.lock` unusable below Python 3.13 | CI red on `main`; hash-pinned installs fail |

**1. Finance API keys are published to the channel on a network failure.**
`modules/stocks.py - _try_providers()` appends `str(exception)` to the
"all providers failed" IRC reply. `urllib3` transport errors embed the full
request URL including `token=` and `apikey=` query parameters, so an upstream
outage while keys are configured broadcasts every finance API key to the channel.
`sender.py - redact_secrets()` is log-only and does not scrub `PRIVMSG`.
Reproduced empirically. A fix precedent already exists in-repo:
`weather_providers/pirateweather/_codes.py - safe_get_json()` implements the
redaction shape. Related, lower severity: the same URL-bearing pattern appears in
`log.warning` calls across `imdb`, `lastfm`, `youtube`, `steam`, and `twitch`.

**2. Provider fallback is silently disabled for 11 of 13 result types.**
`weather_providers/_dispatch.py - Dispatcher.dispatch()` decides "this provider
returned nothing, try the next" with
`result is None or (hasattr(result, "is_empty") and result.is_empty())`. Only
`WeatherResult` and `HourlyResult` implement `is_empty()`. For the other eleven
result types a hollow result counts as success, ends the chain, and no
lower-ranked provider is ever tried. The safety-relevant instance: a free-tier
`tomorrowio` key returns an empty `AlertsResult` on 401/403, which suppresses
NWS, GDACS, and ECCC severe-weather alerts entirely. Six further symptoms
previously reported as separate provider bugs are this one cause. It is an
interaction defect - every provider is correct in isolation - which is why
per-file review kept missing it. See
[internals/weather-providers/](internals/weather-providers/index.md).

**3. `.isprime` is an any-user denial of service.**
`modules/mathx.py - MathxModule.cmd_isprime()` runs the primality test
synchronously on the event loop, unlike `cmd_bignum()` which offloads with
`to_thread`, and a composite surviving trial division falls into an unbounded
Pollard rho. A pasted 100-digit semiprime hangs the entire bot: no commands, no
PING response, eventual disconnect.

**4. The shipped autoload template omits the privacy module.**
`config.ini.example` autoloads 67 modules including `seen`, `tell`, `linktitle`,
`notes`, `remind`, and `steam`, all of which record user-derived data, but not
`privacy`. A deployment that uses the template verbatim tracks users while
shipping no `.forgetme`, `.optout`, `.optin`, or `.privacy` command, so the
right-to-erasure entry point is absent by default. This is a compliance exposure,
not a code bug: the module works, it is simply not enabled. `health` is also
absent, which is merely inconvenient.

**5. The audit chain can be downgraded.**
`audit_log.py - AuditLog.verify()` selects the hash scheme from each record's own
`v` field, so a record written without `v == 2` is verified with unkeyed SHA-256
at any chain position. Anyone with write access to `audit.log` - no key needed,
the algorithm is in the source - can truncate at any point and append forged
legacy-format records that chain cleanly, and `verify()` reports the chain
intact. Effective tamper evidence is reduced to the pre-3.0.0 scheme. Severity
depends on where the key sits relative to the log; the log is 0600, so this is a
local-writer threat, not a remote one.

**6. The startup warning contradicts the secret store.**
`botlog.py` logs `config.ini is world-readable - consider: chmod 640 config.ini`
while `secret_store.py - perms_ok()` requires mode exactly 0600 and fails closed.
An operator who follows the bot's own printed advice makes `[secrets]`
unreadable, and the bot then runs keyless behind a single error line. The
equality test also rejects stricter modes, so 0400 fails the same way.

**7. The dependency lockfile is unusable on most supported Python versions.**
`requirements.lock` was generated on Python 3.14, violating the resolve-on-3.10
contract in `scripts/regen-lockfile.sh`, and therefore omits marker-gated
transitives such as `typing_extensions>=4.4`. Every `--require-hashes` install
below Python 3.13 fails, and the Tests workflow has been red on `main` since
2026-08-13. This is a release-readiness risk rather than a runtime one: the
project currently has no green CI signal.

## Invariants that should not be relaxed

Each of these has a recorded failure behind it; the reasoning is in
[design-decisions.md](design-decisions.md).

1. All IRC I/O and state mutation on one thread, blocking work off-loop, with
   `threading.Lock` around shared state.
2. `is_admin()` re-deriving authorization from the current hostmask, denying on
   an unknown or missing one.
3. DNS pinning for outbound request safety, re-validated per redirect hop -
   not an IP-literal adapter, which breaks TLS SNI.
4. `feels_like_c` and `dewpoint_c` excluded from cross-provider gap-fill,
   because a derived field imported from a provider that measured a different
   temperature produces a self-contradictory reading.
5. Quarantine, never silent reset, on an unreadable state file.
6. The HMAC key sidecar for the audit chain (see risk 5 for the part of this
   that is currently not holding).
7. `_tls_or_refuse()` as the single gate on sending any credential.
8. Streaming and capping every HTTP body before parse.
9. The process lock, released before `os.execv` because the PID survives it.

## Documentation map

| Section | Documents |
|---|---|
| Executive | this page |
| Architecture | [architecture.md](architecture.md), [irc-protocol.md](irc-protocol.md), [state-and-persistence.md](state-and-persistence.md) |
| Security | [security-model.md](security-model.md), [logging-and-auditing.md](logging-and-auditing.md), [security-policy.md](security-policy.md) |
| Operations | [deployment.md](deployment.md), [configuration.md](configuration.md), [operations.md](operations.md), [administration.md](administration.md), [troubleshooting.md](troubleshooting.md), [metrics-and-observability.md](metrics-and-observability.md), [integrations.md](integrations.md) |
| Development | [getting-started.md](getting-started.md), [command-reference.md](command-reference.md), [modules.md](modules.md), [writing-modules.md](writing-modules.md), [providers.md](providers.md), [writing-providers.md](writing-providers.md), [testing.md](testing.md), [contributing.md](contributing.md) |
| Rationale | [design-decisions.md](design-decisions.md) |
| Handoff | [handoff.md](handoff.md), [knowledge-recovery.md](knowledge-recovery.md) |
| Implementation reference | [internals/](internals/index.md), one document per source file |
| Generated API reference | `autoapi/index` |
| Project | [changelog.md](changelog.md) |

Reading order for a new owner: this page, then
[architecture.md](architecture.md), then [security-model.md](security-model.md),
then [handoff.md](handoff.md). Reading order for a new operator: this page, then
[getting-started.md](getting-started.md), then
[deployment.md](deployment.md) and [operations.md](operations.md).
