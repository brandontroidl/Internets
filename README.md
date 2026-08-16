# Internets 5.0.0

An async modular IRC bot and multi-provider weather aggregator, written on
Python's `asyncio` and RFC 2812 with no third-party IRC library. Command
functionality lives in hot-reloadable plugin modules; weather is served by a
capability-based dispatcher that ranks 32 upstream providers per request and
falls back through them.

- **Python:** 3.10+ (CI runs 3.10 through 3.14)
- **Platforms:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2
- **License:** ISC
- **Full documentation:** [docs/index.md](docs/index.md), or build it with
  `scripts/build-docs.sh` (HTML + PDF)

New here? Follow [docs/getting-started.md](docs/getting-started.md), which walks
from a fresh clone to a working `.help` in a channel.

## Capabilities

`modules/` holds 75 `.py` files. 70 of them define a loadable module and 69
register commands, for 165 primary commands; `linktitle` is the exception and
runs entirely from the raw-line fanout. The core adds 4 public and 23 admin
commands. The generated inventory with every name and alias is
[docs/command-reference.md](docs/command-reference.md).

| Area | What it covers |
| --- | --- |
| Weather | Current, forecast, hourly, nowcast, air quality, UV, pollen, astronomy, alerts, wildfire, space weather, marine, tides, historical |
| Reference | Wikipedia, dictionary, Urban Dictionary, DOI, ISBN, RFC, arXiv, OpenAlex/ORCID paper search, tldr-pages, periodic table |
| Developer tools | Calculator, encoders (base64/hex/base32/morse), hashes, UUID/ULID, JWT decode, cron, epoch, timezone, colors, semver |
| Network and security | CIDR/subnet math, DNS/RDNS/CAA, RDAP whois and ASN, TLS certificate probe, TCP probe, HTTP headers, CVE and CVSS lookup, hash identification, IP geolocation and reputation |
| Media and finance | IMDb, Last.fm, YouTube, Twitch, Steam, MTG, Pokemon, D&D 5e, recipes, cocktails, stocks, crypto, FX |
| News and science | Science/infosec/AI/BSD feed reader, Hacker News, Reddit, xkcd, ISS, SpaceX, NASA APOD, near-earth objects, satellite passes |
| Stateful and social | seen, tell, remind, notes, saved locations, URL titles, QDB, privacy and right-to-erasure commands |
| Operator | Auth, module load/unload/reload, rehash, restart, raw send, stats, health, audit log review, shadow-ban, fingerprint |

A module that needs an API key can hide its commands from `.help` until the key
is configured by overriding `is_configured()`, so an unkeyed deployment stays
usable. Seven modules do this today; `modules/search.py` does not, and still
advertises `.si` and `.gi` without a Brave key.

## Architecture

`internets.py` owns one asyncio event loop, the IRC state machine, and command
dispatch. Everything user-facing is a module under `modules/`: a class deriving
from `modules.base.BotModule` with a `COMMANDS` dict mapping a command word to
an async handler `(nick, reply_to, arg)`. Each invocation runs as its own
`asyncio.Task` under a 50-task cap and a 60-second timeout, so one slow handler
cannot wedge the bot. Outbound traffic is serialized through `sender.Sender`, an
`asyncio.PriorityQueue` drained under a token bucket, with protocol traffic
(PONG, CAP, NICK) at priority 0 bypassing the rate limit. Blocking work (HTTP
via `requests`, password hashing, disk writes) is pushed off the loop with
`asyncio.to_thread`. State lives in memory and is flushed to JSON by a
background thread; there is no database.

Weather requests go to `weather_providers._dispatch.Dispatcher`, which discovers
each provider's capabilities by method name (`get_weather`, `get_alerts`, ...),
ranks the providers supporting the requested capability by static accuracy rank,
then live health, then registration order, and walks that chain until one
answers. Every provider normalizes to the frozen dataclasses in
`weather_providers/base.py`, so no command module ever sees upstream JSON.

```
internets.py       event loop, IRC state machine, dispatch, reconnect
admin_cmds.py      AdminCommandsMixin: core and privileged commands
console.py         interactive stdin console (TTY only)
protocol.py        stateless IRC parsing (ISUPPORT, MODE, NAMES, SASL)
sender.py          outbound priority queue, token bucket, credential scrubbing
store.py           in-memory datasets, periodic flush, rate limiters
config.py          config.ini parsing, CLI argparse, frozen constants
botlog.py          logging setup, log-injection guard, rotation
hashpw.py          admin password hashing and verification
secret_store.py    two-tier secret resolution (env, then config.ini[secrets])
audit_log.py       HMAC-chained tamper-evident privileged-action log
metrics.py         opt-in Prometheus text exporter (off by default)
process_lock.py    PID lock with stale detection

modules/           75 .py files; 70 loadable, 69 registering commands
weather_providers/ 32 provider packages plus base, _dispatch, _health, _http
tests/             run_tests.py (standalone) + 40 pytest files
```

Full treatment in [docs/architecture.md](docs/architecture.md); the reasoning
behind the load-bearing choices is in
[docs/design-decisions.md](docs/design-decisions.md).

## Requirements

Python 3.10 or newer. `scrypt` comes from stdlib `hashlib`, so a bare install
can hash admin passwords with no extra package.

| Package | Needed for |
| --- | --- |
| `requests>=2.32.3` | HTTP client for every module that calls a third-party API |
| `aiohttp>=3.14.3` | Preferred async transport for provider calls; falls back to `requests` in a thread if absent |
| `argon2-cffi>=23.1.0` | Argon2id admin password hashing (recommended) |
| `bcrypt>=4.2.0` | bcrypt admin password hashing (alternative) |
| `PyJWT` + `cryptography` | Apple WeatherKit ES256 JWT signing; only if WeatherKit is configured |
| `defusedxml>=0.7.1` | Hardened XML parsing in `modules/qdb.py` |

## Installation

```
git clone https://github.com/brandontroidl/Internets
cd Internets
pip install -r requirements.txt
```

`pip install -e ".[dev]"` adds the test and lint tooling; per-feature extras
(`async`, `bcrypt`, `argon2`, `weatherkit`, `xml`, `all`) are declared too.

## Minimal configuration

Three things are needed before the first start: a config file, an admin password
hash, and IRC server settings.

```
python -m secret_store init          # config.ini from the template, mode 0600
python hashpw.py --algo argon2       # prints one password_hash line
$EDITOR config.ini
```

In `config.ini` set `[irc] server`, `port`, `nickname`, and paste the hash into
`[admin] password_hash`. API keys, NickServ and oper passwords, and the
`weather_user_agent` contact identifier go in the `[secrets]` section at the
bottom of the same file.

`config.ini` must be mode **0600 exactly**. `secret_store.perms_ok()` fails
closed on anything else, including stricter modes such as 0400: the bot then
starts with no secrets at all and a single error line.

**Known defect:** on a world-readable config, `botlog.py` logs
`consider: chmod 640 config.ini`. Following that advice breaks secret reading.
Use `chmod 600`. See [docs/known-issues.md](docs/known-issues.md).

Every key the code reads, with defaults and reload semantics, is in
[docs/configuration.md](docs/configuration.md).

## First start

```
python internets.py
```

Flags, parsed by an argparse parser built at module scope in `config.py`:
`--version`, `--debug [SUBSYSTEM ...]`, `--loglevel LEVEL`, `--debug-file PATH`,
`--no-console`. A `ProcessLock` on `internets.pid` refuses a second instance
against the same state directory, and the stdin console starts only when stdin
is a TTY and `--no-console` is absent.

Add the bot to a channel with `/INVITE Internets #yourchannel`, or `.join
#yourchannel` as a bot admin or verified channel founder.

## Basic usage

```
.help                      list modules and commands (only configured ones)
.w Boston                  current conditions
.f -om 90210               3-day forecast, forced to Open-Meteo
.regloc Portland, OR       save a default location for weather commands
.cc 2^16 / 1024            calculator
.dns example.org MX        DNS lookup
```

In PM the `.` prefix is optional. Arguments are capped at 400 characters. Admin
work starts with `/MSG Internets AUTH <password>` in PM, the only context
`.auth` accepts.

## Security highlights

Each of these is described with its limits in
[docs/security-model.md](docs/security-model.md).

- **Secret store.** First hit wins: `INTERNETS_<NAME>` environment variable,
  then `[secrets]` in `config.ini`. Storage is plaintext protected by 0600 file
  permissions, not encryption. Outbound credentials cannot be hashed because the
  bot must send the literal value.
- **Audit log.** `audit_log.py` chains each privileged action with HMAC-SHA-256
  over the previous record's hash. The key is a 0600 sidecar, so a stolen copy
  of `audit.log` alone cannot forge entries. Tail truncation by someone holding
  both files stays undetectable from the files alone.
- **SSRF guard.** `modules/_netsafe.py` resolves the host, rejects
  private/loopback/link-local/metadata answers, then pins the connection to the
  validated address so DNS cannot change between check and connect, revalidating
  every redirect hop.
- **Admin auth.** scrypt, bcrypt, or argon2id; constant-time comparison;
  per-nick lockout after 5 failures; sessions bound to nick plus hostmask and
  dropped on nick change, quit, disconnect, or rehash.
- **Transport.** TLS 1.3 minimum by default; TLS 1.2 only with
  `INTERNETS_ALLOW_TLS12=1`. Credentials are refused on a non-TLS link.
- **Input handling.** No `eval` or `exec` anywhere; the calculator is an AST
  walker with an explicit whitelist. Module names, channel names, and message
  targets are regex-validated; the module loader blocks symlink traversal.
- **Resource limits.** 50 concurrent command tasks, 200-message send queue,
  10 MB cap on state file loads, per-nick and per-channel rate limiters.

Known unfixed defects, including several that affect the above, are catalogued
in [docs/known-issues.md](docs/known-issues.md) and prioritized with impact in
[docs/handoff.md](docs/handoff.md).

## Documentation map

| Document | Written for |
| --- | --- |
| [getting-started](docs/getting-started.md) | First install through first working command |
| [executive](docs/executive.md) | What the system is and what must not change, for a decision maker |
| [architecture](docs/architecture.md) | The system-level mental model before touching code |
| [irc-protocol](docs/irc-protocol.md) | Exactly what goes on the wire and what each inbound line changes |
| [modules](docs/modules.md) | How modules are discovered, loaded, gated, and reloaded |
| [writing-modules](docs/writing-modules.md) | Building a new command module against the contract |
| [providers](docs/providers.md) | Weather aggregation: selection, fallback, enabling and disabling |
| [writing-providers](docs/writing-providers.md) | Adding a new upstream weather API |
| [command-reference](docs/command-reference.md) | Generated inventory of every dispatchable command |
| [configuration](docs/configuration.md) | Every config key: type, default, secret status, reload semantics |
| [deployment](docs/deployment.md) | Running headless, service units, process lock, upgrade procedure |
| [operations](docs/operations.md) | Day-to-day procedures: start, stop, rehash, rotate, back up, upgrade |
| [administration](docs/administration.md) | The privileged command surface and how far each command reaches |
| [state-and-persistence](docs/state-and-persistence.md) | Every file written to disk, its owner, durability, and privacy weight |
| [logging-and-auditing](docs/logging-and-auditing.md) | The application log and the audit log, and their different guarantees |
| [metrics-and-observability](docs/metrics-and-observability.md) | Log events, `.stats`, `.health`, and the Prometheus exporter |
| [security-model](docs/security-model.md) | Threat model, controls, and where each control stops working |
| [integrations](docs/integrations.md) | Every third-party service, what unlocks it, and what data leaves |
| [testing](docs/testing.md) | Running the suites, test conventions, and the CI gates |
| [troubleshooting](docs/troubleshooting.md) | Symptom-driven diagnosis with the evidence that separates causes |
| [design-decisions](docs/design-decisions.md) | ADRs for the choices whose "obvious cleanup" reintroduces a bug |
| [known-issues](docs/known-issues.md) | Catalogue of verified unfixed defects, with symbol, evidence, and fix shape |
| [handoff](docs/handoff.md) | Maintainer takeover: risks, coordination points, prioritized defects |
| [knowledge-recovery](docs/knowledge-recovery.md) | How to re-derive project knowledge from source when context is gone |
| [internals/index](docs/internals/index.md) | Per-file implementation reference for the whole tree |
| [changelog](docs/changelog.md) / [contributing](docs/contributing.md) / [security-policy](docs/security-policy.md) | Project files rendered into the docs build |

## Development

```
python tests/run_tests.py        # standalone runner, no dependencies needed
pytest tests/ -v                 # 40 pytest files, needs ".[dev]"
```

Both are required. They are disjoint suites: `tests/run_tests.py` is deliberately
not named `test_*.py`, so pytest's default collection never picks it up. CI runs
them as separate steps in `.github/workflows/tests.yml`, whose four jobs are
`test`, `coverage`, `lint`, and `package`.

The coverage gate (`fail_under = 75`) is core-only. `modules/*`,
`weather_providers/*`, `internets.py`, and `console.py` are omitted from the
measured source, so the headline percentage is not repo-wide coverage.

```
scripts/gen-command-reference.py            # regenerate the command inventory
scripts/gen-command-reference.py --check docs/command-reference.md
scripts/verify-doc-citations.py             # content-check every doc citation
scripts/verify_install.sh                   # build, install, hash-verify the wheel
scripts/build-docs.sh                       # Sphinx HTML + PDF
scripts/regen-lockfile.sh                   # regenerate requirements.lock on 3.10
```

**Known defect:** CI has been red on `main` since 2026-08-13. `requirements.lock`
was resolved on Python 3.14 instead of 3.10, so `typing_extensions` is missing
its marker-gated pin and the `--require-hashes` install fails on every leg below
3.13. See [docs/known-issues.md](docs/known-issues.md).

## License

ISC. See [LICENSE.md](LICENSE.md).
