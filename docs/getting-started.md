# Getting started

From a fresh clone to a working `.help` in a channel, in eleven steps. Each step
states what you should see when it worked and where to go when it did not.

This page assumes you can already reach an IRC server and have shell access on
the host that will run the bot. It does not assume any knowledge of the
codebase. For what the pieces are, read [architecture](architecture.md)
afterwards, not before.

## 1. Prerequisites

- Python 3.10 or newer. Check with `python3 --version`. CI covers 3.10 through
  3.14; nothing older will import.
- `git`, and a C toolchain only if your platform has no wheel for `bcrypt` or
  `argon2-cffi`. Neither is mandatory: scrypt comes from stdlib `hashlib`.
- An IRC server, its port, and a nick. TLS on 6697 is the default and the
  supported path.
- Optional but recommended: a NickServ account for the bot's nick, so it can
  authenticate with SASL during capability negotiation instead of racing the
  `IDENTIFY` reply.

The bot refuses to run as root. `internets.py - _entry()` exits when
`os.geteuid() == 0` unless `INTERNETS_ALLOW_ROOT=1` is set. Create an
unprivileged user for it.

## 2. Clone and install

```
git clone https://github.com/brandontroidl/Internets
cd Internets
pip install -r requirements.txt
```

`requirements.txt` is the full runtime stack: `requests`, `aiohttp`,
`argon2-cffi`, `bcrypt`, `PyJWT`, `cryptography`, `defusedxml`. Every lower
bound in it is a security floor, documented inline against the advisory it
closes.

Two narrower options exist. `pip install -e ".[dev]"` adds the test and lint
tooling (pytest, coverage, bandit, pip-audit, build) on top of an editable
install. The `[project.optional-dependencies]` table in `pyproject.toml` also
declares per-feature extras (`async`, `bcrypt`, `argon2`, `weatherkit`, `xml`,
`all`) if you want a minimal footprint.

**Worked when:** `python -c "import requests, aiohttp"` is silent.

**Did not work:** a compiler error building `bcrypt` or `argon2-cffi` means no
wheel exists for your platform. Drop both from the install and use scrypt in
step 4; the bot needs neither.

Installing as a package rather than running from the checkout is a separate
path. `scripts/verify_install.sh` builds the wheel and sdist, installs into a
throw-away venv, hash-verifies every installed file against the wheel's `RECORD`,
and confirms all thirteen declared top-level modules import. Run it after
touching `[tool.setuptools] py-modules` in `pyproject.toml`; that list omitting a
module is what shipped an uninstallable package in 3.0.0 and 4.0.0.

## 3. Create config.ini

```
python -m secret_store init
```

This copies `config.ini.example` to `config.ini` byte for byte, preserving every
inline comment and signup URL, and creates it with mode 0600 via
`os.open(..., O_CREAT | O_EXCL, 0o600)` so there is no readable window. It
refuses to overwrite an existing `config.ini` without `--force`.

`config.ini` is gitignored. `config.ini.example` is the committed template.
Never put real values in the template, and never commit `config.ini`.

**Worked when:** `ls -l config.ini` shows `-rw-------`.

## 4. Permissions: the one trap that silently disables secrets

`config.ini` holds both settings and the `[secrets]` section. The store reads
that section only when the file mode is **exactly 0600**.
`secret_store.perms_ok()` compares for equality and fails closed, which means
0400 and 0640 are both refused. When it fails, `secret_store.get()` returns the
default for every name, so the bot starts with no NickServ password and no API
keys and only one error line explains why.

```
chmod 600 config.ini
```

**Known defect (do not follow the bot's own advice):** when `config.ini` is
world-readable, `botlog.py` logs
`config.ini is world-readable - consider: chmod 640 config.ini`. Mode 0640 is
not 0600, so acting on that message is what breaks secret reading. Use
`chmod 600`. The contradiction between the two subsystems is recorded in
[known-issues.md](known-issues.md).

**Worked when:** `python -m secret_store status` reports the config file
readable rather than a permissions complaint.

**Did not work:** [troubleshooting: secret unavailable or wrong
permissions](troubleshooting.md#secret-unavailable-or-wrong-permissions).

## 5. Generate the admin password hash

```
python hashpw.py --algo argon2
```

`--algo` takes `scrypt` (default), `bcrypt`, or `argon2`. Argon2id is the
recommendation. The tool prints one self-describing line of the form
`algo$params$salt$dk`; every parameter needed to verify travels inside the hash,
so changing the default cost later does not invalidate existing hashes.

Paste that line into `config.ini` as `[admin] password_hash`. The shipped
template suggests putting it in `config.local.ini` instead, which is an overlay
read after `config.ini` and wins on conflict; either location works and both are
gitignored. Plaintext passwords are rejected: `botlog.py - _validate_hash()`
runs at import and calls `sys.exit(1)` on an unrecognized algorithm prefix.

Password rules enforced by `hashpw.py - check_password()`: minimum 8 characters,
maximum 128 **UTF-8 bytes** (not characters, so a non-ASCII passphrase can be
rejected at well under 128 characters), no leading or trailing whitespace, and
for bcrypt a hard 72-byte algorithm limit enforced at both hash and verify time.

**Worked when:** the tool prints a hash and reports how long it took. It warns
below 50 ms (too weak for the host) and above 1 s (auth latency).

**Did not work:** an `ImportError` means the algorithm's package is not
installed. Use `--algo scrypt`, which needs nothing.

## 6. Minimal IRC configuration

Only the `[irc]` block needs attention for a first start. Everything else in the
template has a working default.

```ini
[irc]
server = irc.example.org
port = 6697
ssl = true
ssl_verify = true
nickname = Internets
realname = IRC Bot
```

If the nick is registered, add its NickServ password to `[secrets]` at the
bottom of the same file rather than to `[irc]`:

```ini
[secrets]
nickserv_password = ...
```

Secret lookup order is environment variable `INTERNETS_<NAME_UPPER>` first, then
`[secrets]`. For containers and CI, `export INTERNETS_NICKSERV_PASSWORD=...`
replaces the file entry with no config change.

Two defaults worth knowing before you change them. `[bot] command_prefix`
defaults to `.` and must be non-empty; an empty prefix would make every message
a command, so `config.py` exits rather than accept it. `[bot] autoload` is a long
comma-separated module list loaded at startup. A module needing an API key can
hide its own commands from `.help` by overriding `is_configured()`, which seven
of them do, so an unkeyed autoload list is not a problem.

**Worth checking before you go live:** the shipped `autoload` includes `seen`,
`tell`, `linktitle`, `notes`, `remind`, `steam`, and `location`, all of which
record user-derived data. `privacy` - the module that provides `.forgetme`,
`.optout`, `.optin`, and `.privacy` - is now listed alongside them, as is
`health`. If you are carrying a `config.ini` from an earlier release, its
`autoload` line predates that and will still omit both; append them.

The complete key list, with defaults, types, and which changes need a restart
rather than a rehash, is [configuration](configuration.md).

**Did not work:** [troubleshooting: config rejected at
startup](troubleshooting.md#config-rejected-at-startup).

## 7. First run

```
python internets.py
```

The bot takes a `ProcessLock` on `internets.pid` first and refuses to start if
another instance holds it, which is what keeps two processes from interleaving
writes into the same JSON state files. The interactive console starts only when
stdin is a TTY and `--no-console` was not passed, so a systemd unit or a pipe
skips it automatically.

Useful flags. The argparse parser is built at module scope in `config.py`, which
is why an unrecognized argument exits during import rather than at startup:

| Flag | Effect |
| --- | --- |
| `--version` | Print the version and exit |
| `--debug [SUBSYSTEM ...]` | Global debug, or per-subsystem (`--debug weather store`) |
| `--loglevel LEVEL` | Base level: DEBUG, INFO, WARNING, ERROR |
| `--debug-file PATH` | Mirror all DEBUG output to a separate file |
| `--no-console` | Disable the stdin console for daemonized use |

**Did not work:** the two import-time exits are the common ones. A missing
`config.ini` produces an actionable message naming the path it looked for; an
unrecognized `password_hash` prefix exits from `_validate_hash()`.

## 8. Verify the connection

Watch stdout or `internets.log`. In order, the events that matter:

| Event | Meaning |
| --- | --- |
| `event=connect_ok host=... port=...` | TCP and TLS established |
| `event=caps_requested caps=...` | IRCv3 capability negotiation started |
| `event=sasl_success nick=...` | SASL PLAIN authenticated (only when a NickServ password is set) |
| `event=rejoin nickserv=confirmed` | Services identification confirmed, saved channels being rejoined |

Nothing is saved to rejoin on a first run, so the last line appears only after
the bot has joined a channel once.

Two behaviors to expect rather than debug. If the configured nick is taken the
bot appends `_` and retries, and SASL then authenticates as the bumped nick,
which is correct. Reconnect backoff is 15s, 30s, 60s, 120s, 240s with plus or
minus 25 percent jitter, capped at 5 minutes.

**Did not work:** [troubleshooting: cannot connect](troubleshooting.md#cannot-connect),
[TLS validation failure](troubleshooting.md#tls-validation-failure), or
[SASL failure](troubleshooting.md#sasl-failure).

## 9. Get the bot into a channel

The bot ships with no channel list; it is invite-only by design.

```
/INVITE Internets #yourchannel
```

The IRC server itself enforces who may invite. The alternative, `.join
#yourchannel`, requires either bot admin (step 10) or a verified channel
founder: `modules/channels.py` runs a WHOIS for the caller's services account
and a services `INFO #channel` for the founder line and compares them, with a
15-second timeout.

Joined channels persist to `channels.json` and are rejoined on reconnect.

**Worked when:** the bot appears in the channel and `.help` gets a reply.

**Did not work:** [troubleshooting: not joining
channels](troubleshooting.md#not-joining-channels).

## 10. Run `.help`

```
.help
```

`.help` lists modules grouped by category, showing only modules whose
`is_configured()` passes, so a module missing its API key is invisible rather
than broken. `.help <module>` narrows to one module, `.help <command>` to one
line, `.help all` prints the full grid, and `.help admin` lists the privileged
set once you have authenticated.

In a PM the `.` prefix is optional. Arguments are capped at 400 characters.

**Did not work:** [troubleshooting: command missing or not
responding](troubleshooting.md#command-missing-or-not-responding).

## 11. Authenticate and load a module

`.auth` is accepted only in a private message. Sending it in a channel gets a
refusal, not an attempt, so the password never reaches a channel.

```
/MSG Internets AUTH yourpassword
```

The reply is `<nick>: authenticated.`. The session is keyed by nick **and**
hostmask: a nick change, a quit, a disconnect, or a `.rehash` invalidates it.
Five failures trigger a 300-second lockout on that nick, and every attempt is
written to the HMAC-chained audit log.

Then exercise the loader. `geocode` and `units` are libraries rather than
modules, so the loadable names outside `autoload` are `privacy` and `health`;
on a `config.ini` carried over from an earlier release, `privacy` is the one you
actually want, per the callout in step 6:

```
.load privacy
.modules
```

`.load` compiles and executes the file fresh, with no `sys.modules` entry, so a
subsequent `.reload privacy` picks up edits. `.unload privacy` removes it. On a
current template `privacy` is already loaded, so try `.reload privacy` instead.

One reload behavior to know before you rely on it: `reload_module()` unloads and
then loads, so a **failed** reload leaves the module unloaded rather than
reverting to the running copy. Editing a module with a syntax error in it
deregisters its commands with nothing to restore them, and `.reloadall` does this
for every name in its `FAILED:` list.

**Did not work:** [troubleshooting: module failed to
load](troubleshooting.md#module-failed-to-load). The usual causes are a module
name outside `^[a-z][a-z0-9_]*$`, a command word already registered by another
module (the second load is rejected), or a `COMMANDS` value naming a method that
is not `async def`, which raises `TypeError` at class definition rather than at
first use.

## Where to go next

| You want to | Read |
| --- | --- |
| Understand the system before changing it | [architecture](architecture.md), then [design-decisions](design-decisions.md) |
| See every command that exists | [command-reference](command-reference.md) |
| Tune configuration properly | [configuration](configuration.md) |
| Run it as a service | [deployment](deployment.md) and [operations](operations.md) |
| Use the admin surface | [administration](administration.md) |
| Add a command | [modules](modules.md), then [writing-modules](writing-modules.md) |
| Add a weather source | [providers](providers.md), then [writing-providers](writing-providers.md) |
| Review it for security | [security-model](security-model.md) |
| Take over maintenance | [handoff](handoff.md) |

## Two things that will bite you early

**Hot reload only refreshes command modules.** `.reload weather` re-executes
`modules/weather.py`. It does not refresh `modules/base.py`, `modules/geocode.py`,
`modules/units.py`, `modules/_netsafe.py`, or anything under
`weather_providers/`, because those stay cached in `sys.modules`. Edits to a
helper are invisible until `.restart`, which replaces the whole process image.

**Some constants are frozen at import.** `config.py` binds `SERVER`, `PORT`,
`NICKNAME`, and the credential constants at import time. `.rehash` re-reads the
live `cfg` object but does not re-bind those, so changing the server or NickServ
password needs a full restart. The command prefix is the deliberate exception:
`internets.py - IRCBot._cmd_prefix()` reads it from `cfg` at use time so a rehash
does change it.
