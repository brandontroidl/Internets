# Internets 5.0.0

An async, modular IRC bot and multi-provider weather aggregator.

- **Python:** 3.10 or newer (CI runs 3.10 through 3.14)
- **License:** ISC ([LICENSE.md](LICENSE.md))
- **Package name:** `internets-irc`
- **Full documentation:** [docs/index.md](docs/index.md)

New here? [docs/getting-started.md](docs/getting-started.md) goes from a fresh
clone to a working `.help` in a channel.

## Overview

Internets speaks IRC directly. `protocol.py` imports only `base64` and `re`;
`internets.py` imports the standard library plus this project's own files and
nothing else. No third-party IRC framework is involved, and `requirements.txt`
declares only HTTP, password-hashing, JWT, and XML libraries. The line format,
ISUPPORT negotiation, MODE and NAMES tracking, and SASL PLAIN are implemented
here against RFC 2812.

One `asyncio` event loop owns the connection, the IRC state machine, and command
dispatch. Everything user-facing is a module under `modules/`, and an
authenticated admin can load, unload, and reload a module against the running
process without restarting it, which is how a code change reaches a live bot.

Weather is an aggregator rather than a wrapper around one API.
`weather_providers/_dispatch.py` discovers each provider's capabilities by method
name, ranks the providers that support the requested capability, and walks that
ranked chain until one answers. Every provider normalizes its result into the
frozen dataclasses in `weather_providers/base.py`, so no command module ever
handles upstream JSON. Around weather sits a wider command surface: reference
lookup, developer utilities, network and security tooling, feeds, and stateful
per-user features.

The intended runtime is a long-lived process on a host you control, running as an
unprivileged user out of a git checkout, connected over TLS to one IRC network.
`internets.py - _entry()` exits when the effective uid is 0 unless
`INTERNETS_ALLOW_ROOT=1` is set, and a `ProcessLock` on `internets.pid` refuses a
second instance against the same state directory.

## Capabilities

The commands group into these domains. The generated inventory with every name,
alias, and syntax is [docs/command-reference.md](docs/command-reference.md); it
is regenerated from the source and checked in CI, so it is the list to trust.

| Domain | Coverage |
| --- | --- |
| Weather | Conditions, forecast, hourly, nowcast, air quality, UV, pollen, astronomy, alerts, wildfire, space weather, marine, tides, historical |
| Reference | Encyclopedia, dictionary, DOI, ISBN, RFC, preprint and scholarly paper search, tldr-pages, periodic table |
| Developer tools | Calculator, encoders, hashes, UUID and ULID, JWT decode, cron, epoch, timezone, color, semver |
| Network and security | Subnet math, DNS and RDAP, TLS certificate probe, TCP probe, HTTP headers, CVE and CVSS lookup, IP geolocation and reputation |
| Media and finance | Film and TV, music, video, streaming, games, tabletop, recipes, equities, crypto, FX |
| News and science | Science, infosec, AI and BSD feeds, aggregator front pages, orbital and space-agency data |
| Stateful and social | `seen`, `tell`, reminders, notes, saved locations, URL titles, quote database |
| Privacy | `.privacy`, `.optout`, `.optin`, `.forgetme` |
| Operator | Authentication, module lifecycle, rehash, restart, raw send, statistics, health, audit review, shadow-ban |

75 `.py` files live under `modules/`, of which 70 define a loadable `BotModule`
and 69 register at least one command. `linktitle` is the exception and runs from
the raw-line hook instead. A module whose upstream needs
an API key can report `is_configured()` as `False`, which hides its commands from
`.help` on an unkeyed deployment without disabling dispatch. See
[docs/modules.md](docs/modules.md) for what that gate does and does not do.

## Architecture at a glance

```
internets.py       event loop, IRC state machine, dispatch, reconnect
admin_cmds.py      AdminCommandsMixin: core and privileged commands
protocol.py        stateless IRC line parsing (ISUPPORT, MODE, NAMES, SASL)
sender.py          outbound priority queue, token bucket, credential scrubbing
store.py           in-memory datasets, periodic flush, rate limiters
config.py          config.ini parsing, CLI argparse, frozen constants
botlog.py          logging setup, log-injection guard, rotation
console.py         interactive stdin console (TTY only)
hashpw.py          admin password hash generation and verification
secret_store.py    two-tier secret resolution (env, then config.ini[secrets])
audit_log.py       hash-chained privileged-action log
metrics.py         opt-in Prometheus text exporter, disabled unless enabled
process_lock.py    PID lockfile with stale detection

modules/           command modules, hot-loadable at runtime
weather_providers/ 32 provider packages plus base, _dispatch, _health, _http
tests/             run_tests.py (standalone) and 40 pytest files
```

Each command invocation runs as its own `asyncio.Task` under a concurrency cap
and a timeout, so a slow handler cannot wedge the loop. Blocking work (HTTP via
`requests`, password hashing, disk writes) is pushed off the loop with
`asyncio.to_thread`. State is held in memory and flushed to JSON by a background
thread; there is no database.

The full treatment is [docs/architecture.md](docs/architecture.md), the reasoning
behind the load-bearing choices is
[docs/design-decisions.md](docs/design-decisions.md), and the per-file reference
is [docs/internals/index.md](docs/internals/index.md).

## Requirements

Python 3.10 or newer, per `requires-python` in `pyproject.toml`. CI exercises
3.10 through 3.14 on Linux, macOS, and Windows.

`requests` is the only hard dependency declared in `pyproject.toml`. Everything
below is optional in the packaging sense, and `requirements.txt` installs all of
it because that is the shape the bot is meant to run in. Lower bounds are
security floors, annotated inline in `requirements.txt` with the advisory each
one closes.

| Package | What it unlocks |
| --- | --- |
| `requests` | HTTP client for every module that calls a third-party API |
| `aiohttp` | Preferred async transport for provider calls; without it the bot falls back to `requests` in a worker thread |
| `argon2-cffi` | Argon2id admin password hashing, the recommended algorithm |
| `bcrypt` | bcrypt admin password hashing, the alternative |
| `PyJWT` and `cryptography` | ES256 JWT signing for the Apple WeatherKit provider only |
| `defusedxml` | Hardened XML parsing in `modules/qdb.py` |

Neither password-hashing package is mandatory: scrypt comes from stdlib
`hashlib`, so a bare install can still generate and verify an admin hash. Drop
`bcrypt` and `argon2-cffi` if your platform has no wheel and no C toolchain.

Platform assumptions are POSIX-shaped. The 0600 permission enforcement in
`secret_store.py - perms_ok()` and the `chmod` in `audit_log.py` are skipped on
Windows, where the code relies on filesystem ACLs instead. Full dependency and
supply-chain policy is [docs/dependencies.md](docs/dependencies.md).

## Installation

```
git clone https://github.com/brandontroidl/Internets
cd Internets
pip install -r requirements.txt
```

`pip install -e ".[dev]"` adds the test and lint tooling on top of an editable
install. Per-feature extras (`async`, `bcrypt`, `argon2`, `weatherkit`, `xml`,
`all`) are declared in `pyproject.toml` for a minimal footprint.

Run the bot from the checkout. A wheel or sdist install works only after two
manual repairs: `config.py - MODULES_DIR` defaults to a path resolved against the
working directory, so every autoload entry fails against `site-packages` until
`[bot] modules_dir` is pointed at the installed package by absolute path, and
`config.ini.example` ships in neither artifact, so the template has to be copied
out of the source tree by hand before `python -m secret_store init` will work.
That is item 14 in [docs/known-issues.md](docs/known-issues.md);
[docs/deployment.md](docs/deployment.md) has the worked package-install
procedure. `scripts/verify_install.sh` builds and hash-verifies the wheel as a
packaging gate regardless of how you deploy.

The step-by-step install, with the expected output at each point, is
[docs/getting-started.md](docs/getting-started.md).

## Configuration

Three things are needed before the first start: a config file, an admin password
hash, and IRC server settings.

```
python -m secret_store init          # config.ini from the template, mode 0600
python hashpw.py --algo argon2       # prompts, then prints one password_hash line
$EDITOR config.ini
```

In `config.ini` set `[irc] server`, `port`, and `nickname`, and paste the
generated line into `[admin] password_hash`. API keys, NickServ and oper
passwords, and the `weather_user_agent` contact identifier belong in the
`[secrets]` section of the same file. An optional gitignored `config.local.ini`
overlays `config.ini` if present, which is one way to keep the hash out of a file
you might share.

`config.ini` must be mode 0600. `secret_store.py - perms_ok()` compares for
equality and fails closed on anything else, including stricter modes such as
0400: the bot then starts with no secrets at all and one error line.

Do not follow the bot's own advice here. On a world-readable config it logs
`config.ini is world-readable - consider: chmod 640 config.ini`, and 640 makes
`[secrets]` unreadable. Use `chmod 600`. That is item 7 in
[docs/known-issues.md](docs/known-issues.md).

Environment overrides are narrow rather than general. Any secret in
`KNOWN_SECRETS` can be supplied as `INTERNETS_<NAME_UPPER>`, which wins over the
config file; separately, `INTERNETS_ALLOW_ROOT`, `INTERNETS_ALLOW_TLS12`,
`INTERNETS_ARGON2_MEM_MIB`, `INTERNETS_ARGON2_TIME`, and
`INTERNETS_BCRYPT_ROUNDS` change specific behaviors. Ordinary config keys have no
environment equivalent.

Every key the code reads, with its type, default, secret status, and reload
semantics, is in [docs/configuration.md](docs/configuration.md).

## First run

```
python internets.py
```

Flags, from the argparse parser built at module scope in `config.py`:
`--version`, `--debug [SUBSYSTEM ...]`, `--loglevel LEVEL`, `--debug-file PATH`,
`--no-console`. The interactive stdin console starts only when stdin is a TTY and
`--no-console` is absent.

Add the bot to a channel with `/INVITE Internets #yourchannel`, or `.join
#yourchannel` as a bot admin or a verified channel founder. Running it headless,
under a service manager, and through an upgrade is
[docs/deployment.md](docs/deployment.md) and
[docs/operations.md](docs/operations.md).

## Basic usage

```
.help                      list modules and commands, configured ones only
.w Boston                  current conditions
.f -om 90210               3-day forecast, forced to Open-Meteo
.regloc Portland, OR       save a default location for weather commands
.cc 2^16 / 1024            calculator
.dns example.org MX        DNS lookup
```

In PM the `.` prefix is optional. Command arguments are capped at 400 characters
(`internets.py` `_MAX_ARG_LEN`). Admin work starts with
`/MSG Internets AUTH <password>`; `internets.py - IRCBot._dispatch()` rejects
`.auth` and `.deauth` outside PM. The privileged surface and how far each command
reaches is [docs/administration.md](docs/administration.md).

## Security summary

Each mechanism below is given with its limit. The threat model and the point
where each control stops working is
[docs/security-model.md](docs/security-model.md); vulnerability reporting is
[SECURITY.md](SECURITY.md).

- **Transport.** TLS 1.3 is the floor by default in
  `internets.py - IRCBot._connect()`. `INTERNETS_ALLOW_TLS12=1` lowers it to TLS
  1.2 and logs a warning. `IRCBot._tls_or_refuse()` blocks the server password,
  NickServ password, oper password, and SASL password from being sent over a
  plaintext link.
- **SASL.** PLAIN, negotiated during CAP. `protocol.py - sasl_plain_payload()`
  builds the payload; its confidentiality rests entirely on the TLS layer above.
- **Secret resolution.** First hit wins: the `INTERNETS_<NAME>` environment
  variable, then `[secrets]` in `config.ini`. Storage is plaintext protected by
  file permissions, not encryption. Outbound credentials cannot be hashed,
  because the bot has to send the literal value.
- **Admin authentication.** scrypt, bcrypt, or argon2id, compared in constant
  time, with a per-nick lockout after 5 failures whose timer refreshes on each
  attempt so trickled guesses do not escape it. A session is bound to nick plus
  hostmask and dropped on nick change, quit, disconnect, or rehash.
- **Audit log.** `audit_log.py - AuditLog.record()` chains each privileged action
  with HMAC-SHA-256 over the previous record's hash, keyed by a 0600 sidecar
  file. This gives tamper evidence against someone holding `audit.log` alone. It
  does not cover an actor holding both files, who can truncate the tail
  undetectably; `verify()` also reads only the live file and not rotated
  segments, and it still accepts unversioned records under plain SHA-256 (item 5
  in [docs/known-issues.md](docs/known-issues.md)).
- **SSRF guard.** `modules/_netsafe.py` resolves the host, rejects
  private, loopback, link-local, ULA, IPv4-mapped, and cloud-metadata answers,
  then pins the connection to the validated address so DNS cannot change between
  check and connect, revalidating and re-pinning on every redirect hop.
- **Log redaction.** `sender.py - redact_secrets()` masks the argument after a
  credential verb. Its scope is logging: it is applied to the outbound debug log
  line and to inbound lines before they are logged or audited, not to the
  PRIVMSG payload that reaches a channel. A module that puts a key into a reply
  is not covered (item 1 in [docs/known-issues.md](docs/known-issues.md)).
- **Input handling.** No `eval` or `exec` anywhere; `modules/calc.py` walks the
  parsed AST against an explicit operator table instead. Module names and
  channel names are regex-validated, and `IRCBot.load_module()` refuses a module
  path that resolves outside `modules/`, which covers a symlink out of the
  directory. Message targets are checked for emptiness and embedded spaces
  rather than against a full grammar.
- **Resource limits.** 50 concurrent command tasks and a 60-second per-command
  timeout (`internets.py` `_MAX_TASKS`, `_CMD_TIMEOUT`), a 200-message bounded
  send queue (`sender.py` `MAX_QUEUE`), a 10 MB cap on state-file loads
  (`store.py` `_MAX_FILE_SIZE`), and per-nick flood, per-nick API, and
  per-channel rate limiters in `store.py - RateLimiter`.

Verified unfixed defects, several of which bear on the above, are catalogued in
[docs/known-issues.md](docs/known-issues.md) and prioritized by impact in
[docs/handoff.md](docs/handoff.md).

## Privacy summary

Only the modules that need it record user-derived data: `seen`, `tell`, `notes`,
`remind`, and `steam` keep their own stores, `location` stores through the core
store, and `linktitle` persists nothing but writes announced URLs to the log. The
`privacy` module supplies `.privacy`, `.optout`, `.optin`, and `.forgetme`, and
erasure works when it is loaded.

It is not in the shipped autoload. `config.ini.example` autoloads 67 modules
including all seven that collect data, and `privacy` is not among them, so a
deployment that copies the template verbatim tracks users and offers no erasure
command. Add `privacy` to `[bot] autoload` before running this for other people.
Erasure also cannot reach `internets.log`, which is written with the default
umask while `config.ini` is fail-closed at 0600. Those are items 4 and 15 in
[docs/known-issues.md](docs/known-issues.md). What is recorded, what leaves the
machine, how long it is kept, and what controls a user has are in
[PRIVACY.md](PRIVACY.md), with the normative per-datum table in
[docs/data-retention.md](docs/data-retention.md).

## Documentation

### Start here

| Document | For |
| --- | --- |
| [getting-started](docs/getting-started.md) | Fresh clone to a working command in a channel |
| [executive](docs/executive.md) | What the system is and what must not change, for a decision maker |
| [command-reference](docs/command-reference.md) | Generated inventory of every dispatchable command |

### Understand the system

| Document | For |
| --- | --- |
| [architecture](docs/architecture.md) | The system-level mental model before touching code |
| [irc-protocol](docs/irc-protocol.md) | What goes on the wire and what each inbound line changes |
| [modules](docs/modules.md) | How modules are discovered, loaded, gated, and reloaded |
| [providers](docs/providers.md) | Weather aggregation: selection, fallback, enabling and disabling |
| [state-and-persistence](docs/state-and-persistence.md) | Every file written to disk, its owner, durability, and privacy weight |
| [output-conventions](docs/output-conventions.md) | How replies are formatted and why that matters on real clients |
| [performance](docs/performance.md) | Capacity bounds read from source, with the estimates marked as estimates |
| [design-decisions](docs/design-decisions.md) | The choices whose obvious cleanup reintroduces a bug |
| [internals](docs/internals/index.md) | Per-file implementation reference for the whole tree |

### Extend Internets

| Document | For |
| --- | --- |
| [writing-modules](docs/writing-modules.md) | Building a new command module against the contract |
| [writing-providers](docs/writing-providers.md) | Adding a new upstream weather API |
| [testing](docs/testing.md) | Running both suites, test conventions, and the CI gates |
| [CONTRIBUTING](CONTRIBUTING.md) | Setup, conventions, commit format, and what CI will reject |

### Operate Internets

| Document | For |
| --- | --- |
| [deployment](docs/deployment.md) | Running headless, service units, process lock, upgrade procedure |
| [configuration](docs/configuration.md) | Every config key: type, default, secret status, reload semantics |
| [operations](docs/operations.md) | Start, stop, rehash, rotate, back up, upgrade |
| [administration](docs/administration.md) | The privileged command surface and how far each command reaches |
| [troubleshooting](docs/troubleshooting.md) | Symptom-driven diagnosis with the evidence that separates causes |
| [metrics-and-observability](docs/metrics-and-observability.md) | Log events, `.stats`, `.health`, and the Prometheus exporter |
| [service-objectives](docs/service-objectives.md) | What healthy means here, and which of it the code can measure |
| [incident-response](docs/incident-response.md) | Runbooks for a security incident on a running deployment |
| [disaster-recovery](docs/disaster-recovery.md) | What must be backed up, how to restore it, and a drill to prove it |

### Security and data

| Document | For |
| --- | --- |
| [security-model](docs/security-model.md) | Threat model, controls, and where each control stops working |
| [SECURITY](SECURITY.md) | How to report a vulnerability, and what is in scope |
| [logging-and-auditing](docs/logging-and-auditing.md) | The application log and the audit log, and their different guarantees |
| [integrations](docs/integrations.md) | Every third-party service, what unlocks it, and what data leaves |
| [PRIVACY](PRIVACY.md) | What the bot records about a user and what controls they have |
| [data-retention](docs/data-retention.md) | Normative per-datum retention, for answering an erasure request |
| [known-issues](docs/known-issues.md) | Verified unfixed defects, with symbol, evidence, and fix shape |

### Maintain the project

| Document | For |
| --- | --- |
| [handoff](docs/handoff.md) | Maintainer takeover: risks, coordination points, prioritized defects |
| [knowledge-recovery](docs/knowledge-recovery.md) | Re-deriving project knowledge from source when context is gone |
| [documentation-governance](docs/documentation-governance.md) | How this corpus stays true after the day it was written |
| [versioning-and-support](docs/versioning-and-support.md) | The compatibility contract and what a major bump has cost before |
| [release-process](docs/release-process.md) | Cutting, verifying, publishing, and retracting a release |
| [dependencies](docs/dependencies.md) | Pinning policy, lockfile regeneration, audits, and service supply chain |
| [CHANGELOG](CHANGELOG.md) | The historical release record |

Rendered HTML and PDF are produced by `scripts/build-docs.sh`.

## Development

```
python tests/run_tests.py                                    # standalone runner, stdlib only
pytest tests/ -v                                             # pytest suite, needs ".[dev]"
python scripts/verify-doc-citations.py                       # every doc citation resolves
python scripts/gen-command-reference.py --check docs/command-reference.md
scripts/verify_install.sh                                    # build, install, hash-verify the wheel
scripts/build-docs.sh                                        # Sphinx HTML and PDF
```

Both test runners are required and they are disjoint. `tests/run_tests.py` is
deliberately not named `test_*.py`, so pytest's default collection never picks it
up; CI runs them as separate steps. `.github/workflows/tests.yml` has four jobs:
`test`, `coverage`, `lint`, and `package`.

The coverage gate is core-only. `modules/*`, `weather_providers/*`,
`internets.py`, and `console.py` are omitted from the measured source in
`pyproject.toml`, so the headline percentage is not repo-wide coverage.

Test conventions, fixtures, and what each CI gate actually checks are in
[docs/testing.md](docs/testing.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. It covers
local setup, coding conventions, the documentation rules that the citation gate
enforces, commit format, and what CI will reject. Participation is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Security issues go through [SECURITY.md](SECURITY.md), not the public issue
tracker.

## License

ISC. See [LICENSE.md](LICENSE.md).
