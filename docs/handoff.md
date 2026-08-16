# Maintainer handoff

Written for the case where nobody who built this is available. It answers what a
senior engineer needs before touching the system: what the pieces are, which
ones are load-bearing, where a careless change breaks something silently, what
must be updated together, and what is currently broken.

It deliberately does not restate the per-file reference. That now lives in
[internals/index.md](internals/index.md), one page per source file, structured
identically across the tree. When this page says "see internals", it means the
detail is there and duplicating it here would create a second copy to keep in
sync.

Read in this order on day one: [executive](executive.md) for why the system
exists, [architecture](architecture.md) for the mental model, this page for the
risks, then [design-decisions](design-decisions.md) before proposing any
cleanup. The last one matters: several parts of this codebase look like they
want simplifying and do not.

## Architecture map

Three layers, each with its own entry document.

| Layer | Code | Entry document |
| --- | --- | --- |
| Core | 13 top-level `.py` files | [architecture](architecture.md) |
| Commands | `modules/` (75 files, 70 loadable, 69 registering commands) | [modules](modules.md) |
| Weather | `weather_providers/` (32 packages) | [providers](providers.md) |

The core owns one asyncio event loop. Commands and providers are the only two
extension points, and they extend by different mechanisms: modules by name
lookup in a `COMMANDS` dict, providers by `hasattr` on capability method names.

Import order is load-bearing and constrains everything else:
`config.py` (which imports `secret_store`) then `botlog.py` (which imports from
`config`) then everything else. Both `config.py` and `botlog.py` run validation
at import and can `sys.exit(1)` before the event loop exists. That is why a
misconfiguration is a startup failure rather than a runtime surprise, and why
neither file can grow a dependency on anything below it.

Protocol-level behavior (what goes on the wire, what each inbound line changes)
is [irc-protocol](irc-protocol.md). Everything written to disk is
[state-and-persistence](state-and-persistence.md).

## Critical files and why

Ordered by what breaks if you get them wrong.

| File | Why it is critical |
| --- | --- |
| `internets.py` | The event loop, the IRC state machine, dispatch, module loading, reconnect, shutdown, and the restart path. Every other file is reachable from here. |
| `admin_cmds.py` | `AdminCommandsMixin` holds every privileged command. The auth path and its TOCTOU defenses live here. |
| `secret_store.py` | The only path to an outbound credential. Its permission check is fail-closed, so a bug here silently unkeys the whole bot. |
| `hashpw.py` | Admin authentication. Both hashing and verification, sharing one validation function so the two ends cannot drift. |
| `store.py` | All durable user state plus the rate limiters. Its corruption quarantine is the only thing between a bad read and permanent data loss. |
| `audit_log.py` | The tamper-evident record of privileged actions. Its value is entirely in the chain, not the text. |
| `sender.py` | Every outbound byte. Line splitting, flood control, and credential scrubbing for logs. |
| `modules/_netsafe.py` | The SSRF defense for user-influenceable URLs. Used by `probe`, `urls`, `linktitle`, and the article readers. |
| `modules/base.py` | The module contract itself, plus `fetch_json` size caps, `strip_ctrl` sanitation, and `cred` credential loading. |
| `weather_providers/_dispatch.py` | Provider selection, fallback, and the chain budget. One conditional in here currently disables fallback for most capabilities (see open defects). |

## High-risk code paths

### Code execution

`.load <module>` (`admin_cmds.py - cmd_load()`, `internets.py -
IRCBot.load_module()`) runs `exec_module` on any `.py` file in the modules
directory with the bot's full process privileges. The module name is
regex-constrained to `^[a-z][a-z0-9_]*$` and the loader blocks symlink
traversal, but any file already sitting in that directory is trusted. Write
access to `modules/` is equivalent to code execution as the bot user.

`.raw <line>` (`admin_cmds.py - cmd_raw()`) puts an unvalidated IRC line on the
wire. Only CR, LF, NUL, and the 510-byte cap are enforced. What the line does
(`KILL`, `OPER`, `SAMODE`) is whatever the admin typed. It is audit-logged with
credentials redacted, but the blast radius is the full privilege of the bot's
IRC connection, including any oper privileges it holds.

The interactive console (`console.py`) is admin-equivalent with no
authentication. The only boundary is physical or shell access to stdin. Any
daemonized deployment should pass `--no-console`; the module also skips itself
when stdin is not a TTY.

### State corruption

Two instances against the same state directory will interleave writes.
`process_lock.py` prevents this, but deleting `internets.pid` while the bot runs
reopens the race. The JSON writers use `mkstemp` plus `os.replace`, which makes
each individual write atomic and does nothing about two writers racing.

Editing `config.ini` while the bot runs is safe only if the edit completes before
a rehash reads it. The `cfg` object is live in memory; `.rehash` and `SIGHUP`
re-read both files. There is no lock, so a rehash landing mid-write produces a
partial config. Edit, save, then rehash.

### Authentication

`internets.py - IRCBot.is_admin()` re-derives the caller's hostmask on every
call and compares it against the one captured at auth time. This looks like it
could be a set membership test and cannot be: the re-check is what stops a
nick-grab from inheriting a live session, and the `"unknown"` sentinel denial
closes a window where the admin quits during `verify_password`. Sessions are
dropped on nick change and quit, never migrated, because migration is exactly
how a malicious server or a nick takeover would launder a session.

`admin_cmds.py - cmd_auth()` snapshots the hostmask before offloading
`verify_password` to a thread and rejects if it changed by the time the hash
returns. It also re-reads the failure counter inside the lock after the await,
because a concurrent attempt can bump it during the hash.

### Weather gap-fill

`weather_providers/base.py - fill_gaps()` copies missing secondary fields from
the next-ranked provider, bounded to three contributors, crediting both sources.
`feels_like_c` and `dewpoint_c` are deliberately excluded from
`_CURRENT_GAP_FIELDS`: they are derived from the same observation's temperature
and humidity, so importing them from a different provider produces a reading
that never existed anywhere. ADR-010 in [design-decisions](design-decisions.md)
is the full argument, and a proposal to average providers was rejected on the
same grounds.

## Security boundaries

Each of these is a place where trust changes. [security-model](security-model.md)
carries the threat model and the limits; this is the map.

| Boundary | Enforced by | Fails |
| --- | --- | --- |
| Untrusted IRC user to bot | `internets.py - IRCBot._dispatch()`: shadow-ban drop, PM-only gate, flood and channel limiters, 400-char arg cap, 50-task cap, 60s timeout | Closed (drop or refuse) |
| Unauthenticated to admin | `is_admin()` hostmask re-check, `_require_admin()` | Closed (deny on unknown) |
| Process to credential | `secret_store.get()` with `perms_ok()` at exactly 0600 | Closed (returns default) |
| Bot to network, plaintext link | `internets.py - IRCBot._tls_or_refuse()` | Closed (CRITICAL log, no send) |
| User-supplied URL to outbound request | `modules/_netsafe.py - safe_open()` with DNS pinning, revalidated per redirect hop | Closed (refuse) |
| Untrusted text to IRC line | `modules/base.py - strip_ctrl()` then `sender.py` transport-byte strip | Closed (strip) |
| Untrusted text to log | `botlog.py - _SafeFormatter` | Closed (strip) |
| Local operator to bot | None; the console is unauthenticated by design | Open (physical access is the boundary) |
| Metrics exporter to network | `metrics.py - expose()` refuses unspecified bind addresses | Closed (refuse) |

Two known inconsistencies in this table are recorded in the open defects
section: `modules/base.py - resolve_public()` and `modules/_netsafe.py -
ip_is_blocked()` disagree about IPv6 site-local, and `audit_log.py - verify()`
accepts a weaker scheme than `record()` writes.

## External dependencies

Runtime packages, and what happens without each.

| Package | Load-bearing | Without it |
| --- | --- | --- |
| `requests` | Yes | Nothing that touches HTTP works. The only hard dependency in `pyproject.toml`. |
| `aiohttp` | No | Provider calls fall back to `requests` under `asyncio.to_thread`. Slower, correct. |
| `argon2-cffi` | No | Argon2 hashes cannot be verified. Only matters if the configured hash is argon2. |
| `bcrypt` | No | Same, for bcrypt hashes. Version matters: below 5.0 it silently truncates at 72 bytes, which is why `hashpw.py` enforces the limit itself at both ends. |
| `PyJWT` + `cryptography` | No | Apple WeatherKit cannot sign its ES256 token and does not register. |
| `defusedxml` | No | `modules/qdb.py` loses its billion-laughs guard on top of stdlib XXE protection. |

Version floors are security floors, annotated inline in `requirements.txt` and
`pyproject.toml` with the advisory each closes. `requirements.lock` is the
hash-pinned install used by CI and must be regenerated on Python 3.10 via
`scripts/regen-lockfile.sh`, because resolving on a newer interpreter omits
marker-gated backports that older legs need. That is currently broken; see open
defects.

The upstream service dependencies are a different surface: 32 weather providers
plus roughly forty module APIs. [integrations](integrations.md) enumerates them
with what unlocks each and what user-derived data leaves the machine. The ones
worth knowing on day one: Nominatim (all geocoding, and it requires a real
contact identifier in the User-Agent or the bot disables geocoding entirely),
NWS (rank 1 for US weather), and Open-Meteo (the keyless global fallback under
most capabilities).

## Secrets inventory

`secret_store.KNOWN_SECRETS` holds 41 names.
`secret_store.CONFIG_LOCATIONS` maps 40 of them to a `(section, key)` pair for
the `migrate` command.

| Group | Count | Notes |
| --- | --- | --- |
| IRC authentication | 4 | `nickserv_password`, `sasl_password`, `server_password`, `oper_password` |
| Contact identifier | 1 | `weather_user_agent`; PII, sent in every outbound User-Agent |
| Weather provider keys | 26 | Includes the four `weatherkit_*` values and the two `meteomatics_*` values |
| Module API keys | 10 | `omdb_key`, `lastfm_key`, `youtube_key`, three stock providers, `steam_key`, two `twitch_*`, `brave_key`, `abuseipdb_key` |

Operating rules that are not negotiable. Outbound credentials are stored
reversibly because the bot must send the literal value on the wire; hashing them
would break the authentication they exist for. The admin password is the
opposite case and is only ever stored hashed. No CLI flag prints a secret value,
deliberately, to keep values out of shell history and scrollback; extraction for
rotation goes through `python -c "import secret_store; print(secret_store.get('name'))"`.

Three gaps to be aware of:

- `sasl_password` is in `KNOWN_SECRETS` and the template but has no consumer.
  The SASL path uses the NickServ password. A distinct value is silently ignored.
- `nasa_api_key` is read by `modules/apod.py` and `modules/astro2.py` but is in
  neither `KNOWN_SECRETS` nor `CONFIG_LOCATIONS`. It works through `get()` and
  through `INTERNETS_NASA_API_KEY`, but `secret_store list` cannot see it and
  `migrate` will not relocate it.
- `weather_user_agent` is read by 49 of the 75 module files as the global
  User-Agent and is a fail-closed gate: `modules/geocode.py - _ua_has_contact()`
  disables all Nominatim geocoding unless it contains an `@domain` or an
  `http(s)://` URL. A deployment that leaves it blank gets weather and location
  commands failing with no obvious cause.

## Deployment assumptions

The code assumes all of the following. Each one is a real constraint, not a
preference.

- **The working directory is the state directory.** `config.py` resolves
  `config.ini` against the current working directory at import. So do all the
  JSON state files, the lock file, the log, and the audit log. A service unit
  must set `WorkingDirectory`, not just `ExecStart`.
- **The process runs as an unprivileged user.** `internets.py - _entry()`
  refuses `euid 0` unless `INTERNETS_ALLOW_ROOT=1`.
- **One instance per state directory.** Enforced by `process_lock.py` with PID
  liveness probing. A lockfile naming a different host is always treated as
  live, which is conservative and correct on shared storage.
- **The filesystem honors POSIX modes.** The 0600 enforcement is skipped on
  Windows, where the code relies on filesystem ACLs instead.
- **`os.replace` is atomic.** True on POSIX, best-effort on NTFS. The durability
  story stops there: no writer in this repo calls `os.fsync`, so a power loss
  can lose the last write even though it will never see a torn file.
- **Restart is a full process replacement.** `.restart` uses `os.execv`, which
  preserves the PID, which is why the restart path must release the process lock
  before exec or the new image sees its own old PID as a live holder.
- **The live instance may not be the repo checkout.** Deployments here have
  historically run from a copied directory. Confirm which copy is live by
  reading the running process's `cwd` before debugging against a checkout.

[deployment](deployment.md) has the service unit, the upgrade procedure, and the
headless specifics.

## Persistent state

No database. Everything is a file in the working directory, written by five
independent owners. Full treatment in
[state-and-persistence](state-and-persistence.md).

| File | Owner | Recoverable |
| --- | --- | --- |
| `locations.json`, `channels.json`, `users.json` | `store.py` | Yes: checksum envelope, `.bak`, quarantine on bad read |
| `seen.json`, `tells.json`, `notes.json`, `reminders.json`, `steamids.json` | the owning module | No: a corrupt file loads as empty and is overwritten on the next save |
| `shadow_bans.json` | `internets.py` | No: same, so an unclean restart silently unbans everyone |
| `audit.log`, `audit.log.key` | `audit_log.py` | Chain-verifiable; rotated segments each start a fresh chain |
| `internets.log` and rotations | `botlog.py` | Not applicable; 5 MB, 3 backups |
| `internets.pid` | `process_lock.py` | Rebuilt on start |
| `config.ini`, optional `config.local.ini` | operator | Backup target |

Only `store.py`'s three datasets carry a checksum envelope and quarantine. The
module-owned files do not, which is the largest single asymmetry in the
durability story.

The flush thread writes dirty datasets every 30 seconds, so a hard crash loses
at most about 30 seconds of user-tracking timestamps. Location and channel
changes also flush on `.shutdown`, `.restart`, and the signal handlers.

Privacy weight sits mostly in `users.json` (nick to hostmask to channel to
timestamps) and `locations.json`. `.forgetme` reaches both, plus every module
that overrides `forget()`. It does not reach `.bak` files, `.corrupt.*`
quarantine files, rotated audit segments, or rotated logs.

## Common maintenance procedures

### Choosing between reload, rehash, and restart

| Change | Action | Why |
| --- | --- | --- |
| Edit a command module | `.reload <module>` | Re-executes that file only |
| Edit `modules/base.py`, `geocode.py`, `units.py`, `_netsafe.py`, or any provider | `.restart` | Cached in `sys.modules`; a reload will not see the edit |
| Change a live config value (prefix, cooldowns, autoload list) | `.rehash` | Re-reads both config files into `cfg` |
| Change server, port, nick, or any credential | `.restart` | Bound as import-time constants in `config.py` |
| Change the admin password hash | `.rehash` | Re-read and re-validated, and all sessions cleared |
| Upgrade a dependency | Stop, install, `python tests/run_tests.py`, start | No hot path for this |

`.rehash` clears admin sessions only on the happy path. Its two early returns
(config reload failure, bad hash prefix) return before the clear, so a config
syntax error leaves existing sessions authenticated.

A **failed** `.reload` leaves the module unloaded. `reload_module()` unloads and
then loads, so a syntax error introduced while editing deregisters the module's
commands with nothing to restore them. `.reloadall` does this for every name in
its `FAILED:` list. Two related loader hazards: `setup(bot)` runs before the
command-conflict check, so a module's side effects fire even on a load that is
then rejected, and `on_load()` runs before registration with no `on_unload()` on
the raise path, so a failure there leaks whatever it acquired.

### Rotating a secret

Extract the current value if you need it, set the new one, then restart or
rehash depending on which secret it is.

```
python -c "import secret_store; print(secret_store.get('omdb_key'))"
python -m secret_store set omdb_key
```

Module API keys are read live and take effect on the next command. IRC
credentials are import-time constants and need a restart.

### Verifying the audit chain

`.audit verify` walks the chain and reports the first broken index. Note that it
reads only the current segment, not rotated ones, and that the legacy-record
fallback described under open defects weakens what a clean result proves.

### Backup set

Back up together, since they are only meaningful as a set: `config.ini`, any
`config.local.ini`, `audit.log` and `audit.log.key` and rotated segments, every
`*.json` state file, and any `*.bak` or `*.corrupt.*` recovery files.
`config.ini` contains secrets; handle it accordingly.

### Recovering from corruption

A corrupt file in `store.py`'s set is renamed to `<name>.corrupt.<timestamp>`
and the bot starts with empty state for that dataset. The `.bak` file is a
one-deep copy from the last successful write. Recovery is manual inspection and
restoration.

A corrupt audit key is moved to `audit.log.key.bad` and a fresh key generated.
Old segments remain verifiable with the recovered old key; new records start a
new chain.

A stale lockfile from a `kill -9` clears automatically when the PID is dead. If
the PID was reused, or the file names another host, delete `internets.pid` by
hand after confirming no instance is running.

## Changes that require coordinated updates

This is the section that saves the most time. Each of these is a change whose
obvious edit is incomplete, and whose incompleteness does not fail loudly.

### Adding a core command

Touches three places:

1. `admin_cmds.py - AdminCommandsMixin._CORE` (the name to method mapping).
2. `_CORE_PUBLIC` if it should work unauthenticated. Anything not in that
   frozenset is derived as admin-only by `_core_admin_cmds()`, so the help
   output and the admin gate both follow automatically once the set is right.
3. The command-reference drift gate:
   `scripts/gen-command-reference.py --check docs/command-reference.md`.

The count in [command-reference](command-reference.md) is generated, so
regenerate rather than hand-editing.

### Adding a command module

`COMMANDS` values must name `async def` methods on the class;
`modules.base.BotModule.__init_subclass__` validates this at class-definition
time, so a typo or a sync handler raises `TypeError` at import rather than at
first use. Beyond that: add the module to `[bot] autoload` if it should load at
startup, implement `is_configured()` returning `False` when a required key is
absent so the module hides cleanly from `.help`, and override `forget(nick)` if
it persists anything per-nick, because `.forgetme` reaches modules only through
that hook. Two modules registering the same command word is a load-time
rejection of the second.

See [writing-modules](writing-modules.md) for the full contract.

### Adding a weather provider

Touches five places, and skipping any one of them fails quietly rather than
loudly:

1. The package under `weather_providers/<id>/`, with capability methods named
   `get_*` (the dispatcher discovers them by `hasattr`, so a misspelled method
   name means the capability simply does not exist).
2. The factory registration in `weather_providers/__init__.py`, returning `None`
   when the key is absent.
3. `secret_store.KNOWN_SECRETS` and `secret_store.CONFIG_LOCATIONS`, or the key
   is invisible to `secret_store list` and to `migrate`.
4. `weather_providers/_dispatch.py - DEFAULT_RELIABILITY` for every capability
   the provider answers. A capability missing from the table gets rank 99 and is
   effectively last, silently.
5. `config.ini.example` under `[weather_providers] provider_priority`, and the
   `[secrets]` block.

The reliability table is the easiest of these to get wrong in both directions:
it currently ranks two providers for capabilities they do not implement, and
omits one provider from a capability it does. See
[writing-providers](writing-providers.md).

### Adding a password hash algorithm

`hashpw.py` and `botlog.py` maintain separate enumerations of valid hash
prefixes. Adding an algorithm to `hashpw.py` without updating
`botlog.py - _VALID_HASH_PREFIXES` makes the bot refuse to start on a hash that
`verify_password()` handles perfectly well.

### Adding a field to a result dataclass

`weather_providers/base.py` result types are frozen dataclasses with
hand-enumerated merge and gap-fill logic. A new field must be added to the
dataclass, to `_CURRENT_GAP_FIELDS` if it is gap-fillable (and deliberately not
if it is derived), and to the formatter in `modules/weather.py` that renders it.
The round-trip tests will pass without any of this, because a field left at its
default compares equal to itself.

### Editing a source file that documentation cites

Documentation citations in this corpus are symbol-primary for exactly this
reason. Where a line number does appear, editing the file above it invalidates
it silently: the cited line still exists, it just says something else.
`scripts/remap-doc-citations.py` rebuilds the mapping from a git ref, and
[knowledge-recovery](knowledge-recovery.md) describes why the content check
matters more than the renumbering.

## Test strategy

Two disjoint suites. Both are required; neither subsumes the other.

```
python tests/run_tests.py        # standalone, no third-party dependency
pytest tests/ -v                 # 40 test_*.py files
```

`tests/run_tests.py` is deliberately not named `test_*.py`, so pytest's default
collection does not pick it up. It is self-contained on a `@test` decorator so
it runs on a bare checkout before any install, and it holds the completeness
gates (enumerations that must stay in sync) plus the geocode and formatting unit
checks. Running only pytest skips it entirely and the result still looks green.

CI runs both as separate steps. `.github/workflows/tests.yml` has four jobs:
`test`, `coverage`, `lint`, `package`. `security.yml` adds bandit, pip-audit,
and gitleaks; `codeql.yml` runs `security-extended`.

The coverage gate is `fail_under = 75` and is **core-only**: `modules/*`,
`weather_providers/*`, `internets.py`, and `console.py` are omitted from the
measured source. The headline number describes the top-level orchestration files
and nothing else. Do not read it as repo-wide coverage and do not "improve" it
by including the omitted paths without reading the comment in `pyproject.toml`
first.

Known gaps, so you do not mistake green for covered: roughly 44 of the modules
have no behavioral test, there is no end-to-end dispatch test (`test_dispatcher.py`
tests the weather dispatcher, not command dispatch), `console.py` has no test
file, and `pyproject.toml` sets `asyncio_mode = "auto"` while the suite's own
convention is manual event loops. [testing](testing.md) has the full inventory
and the conventions a new test must follow.

## Troubleshooting entry points

[troubleshooting](troubleshooting.md) is organized by symptom and is the right
first stop. The four surfaces it draws on:

| Surface | Use it for |
| --- | --- |
| `internets.log` | Structured `event=...` lines for every lifecycle transition. `.loglevel` and `.debug <subsystem>` adjust it live. |
| `.stats` | Traffic counters, send-queue depth, dropped messages, module count, RSS, audit size |
| `.health` | Per-subsystem snapshot, admin-only |
| `.providers` | Per-provider health score, success rate, latency, breaker state, capability matrix |

Four signals worth a standing alert: any `*.corrupt.*` file appearing (a state
file failed to load), a non-zero dropped-message count (the 200-message send
queue overflowed), `event=dispatch_budget_exhausted` (the 45-second provider
chain ran out before trying every provider), and a provider sitting in `open`
breaker state, which usually means a revoked key rather than an outage.

[metrics-and-observability](metrics-and-observability.md) covers the Prometheus
exporter, which is off by default and refuses to bind a non-loopback address.

## Extension points

| To add | Start at | Touches |
| --- | --- | --- |
| A command | [writing-modules](writing-modules.md) | One file under `modules/`, plus `autoload` |
| A weather source | [writing-providers](writing-providers.md) | Five places; see the coordinated-updates section above |
| A metric | `metrics.py - registry` | Register it, then increment at the call site. Six of the ten current metrics have no call site at all. |
| A log subsystem | `botlog.py - DebugFilter` | Name the logger under the `internets.` hierarchy and it becomes addressable by `.debug <name>` |
| An audit action | `admin_cmds.py - _audit()` | Call it after the action succeeds, never before |

The deliberate non-extension points: there is no plugin hook for the IRC
protocol layer (add handling in `internets.py - IRCBot._process()`), no
class auto-discovery in the module loader (`setup(bot)` is the only entry
point), and no configuration mechanism for the timing constants (keepalive,
backoff, command timeout, dispatch budget are all hardcoded).

## Open defects, prioritized

Every entry below is verified against source and unfixed. Full evidence, the
reproduction, and the shape a fix would take are in
[known-issues](known-issues.md), numbered as cited here. This section exists to
give the priority order and the one-line impact, not to repeat the catalogue.

None of these is a documentation problem. Each changes runtime behavior and
belongs to whoever owns the project.

### Fix before anything else

| # | Symbol | Impact |
| --- | --- | --- |
| 1 | `modules/stocks.py - _try_providers()` | A network outage prints every configured finance API key to the channel; `redact_secrets` is log-only and does not scrub PRIVMSG |
| 2 | `weather_providers/_dispatch.py - Dispatcher.dispatch()` | Fallback is dead for 11 of 13 result types, so a hollow result ends the chain; severe weather alerts are silently suppressed when Tomorrow.io 401s |
| 3 | `modules/mathx.py - MathxModule.cmd_isprime()` | Any user can stop the whole process with one pasted semiprime; the primality test runs on the event loop with an unbounded fallback |
| 4 | `config.ini.example` `[bot] autoload` | The shipped template collects user data and omits `privacy`, so a verbatim deployment has no right-to-erasure command at all |
| 5 | `audit_log.py - AuditLog.verify()` | A writer to `audit.log` can rewrite the chain as legacy records and have verification report it intact |

Items 1 through 3 are user-facing and reachable today. Item 4 is a compliance
exposure that costs one line of config to close. Item 5 gates the value of the
audit log.

### Fix soon

| # | Symbol | Impact |
| --- | --- | --- |
| 6 | `requirements.lock` vs `scripts/regen-lockfile.sh` | CI red on `main` since 2026-08-13; the lock was resolved on 3.14 so every leg below 3.13 fails its hash-pinned install |
| 7 | `botlog.py` warning vs `secret_store.py - perms_ok()` | The bot advises `chmod 640`, which its own fail-closed 0600 check then rejects, leaving the deployment silently keyless |
| 8 | `weather_providers/noaa_coops/tides.py - fetch()` | `.tides` reports the day's first high and low with no time filter, so it is wrong for most of the day |
| 9 | `modules/health.py - HealthModule.COMMANDS` | The module's public `.uptime` is shadowed by the core admin one and can never run, while help still advertises it |
| 10 | `modules/health.py - HealthModule.cmd_health()` | Two store counters print `?` permanently because the field names do not match |
| 11 | `weather_providers/purpleair/_codes.py - epa_correct()` | EPA correction coefficients are applied to the wrong PM2.5 variant, worst during smoke episodes |
| 12 | `internets.py - _save_shadow_bans()` and two modules | Shared dicts are serialized in a worker thread with no lock, so a concurrent mutation silently skips the save; no writer anywhere calls `os.fsync` |

### Carried, lower severity

Item 13 in [known-issues](known-issues.md) holds the rest. The ones most likely
to cost a maintainer time:

- `.forgetme` cannot reach the application log, where `modules/linktitle.py` and
  `modules/location.py - cmd_regloc()` write per-channel URL and location data.
- `base.py - resolve_public()` and `modules/_netsafe.py - ip_is_blocked()`
  disagree on IPv6 site-local, so which guard a module uses changes what is
  reachable.
- `_dispatch.py - DEFAULT_RELIABILITY` ranks two providers for capabilities they
  do not implement and omits one from a capability it does; the test that should
  catch this registers no keyed providers.
- Six of the ten default metrics have no update call site and read as constant
  zero. Do not build a dashboard on them.
- `config.py - reload_config()` skips import-time validation, so a `.rehash` can
  install the empty `command_prefix` the startup guard exists to prevent.
- `config.ini.example` cannot configure much of what the code reads: several
  state-file keys and nine per-module sections are absent.
- Timezone labelling is inconsistent across providers, and providers disagree on
  whether no-coverage is an exception or a `None`. `nws/_scope.py` is the
  reference implementation for the latter.

### Test gaps that let these through

Recorded at the end of [known-issues](known-issues.md). The four that matter:
there is no end-to-end dispatch test, `console.py` has no test file, 44 of the
75 files under `modules/` have no behavioral test, and
`tests/test_physcalc.py - TestRc.test_five_band` asserts a miscalculated result,
locking a defect in as expected behavior. Fixing `modules/physcalc.py` therefore
requires changing that test in the same commit.
