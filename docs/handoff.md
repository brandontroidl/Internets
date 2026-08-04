# Handoff Binder

Assume every original developer disappears tomorrow. This document answers the
questions a new maintainer with a CS degree and production engineering
experience - but zero prior knowledge of this codebase - needs answered before
they can safely maintain, extend, debug, and deploy this system.

Read `docs/executive.md` first for the strategic picture.

---

## How is the system organized

```
internets.py          the bot core: event loop, IRC state machine, dispatch
admin_cmds.py         AdminCommandsMixin: every privileged admin command
console.py            interactive stdin admin console (TTY only)
protocol.py           pure IRC protocol parsers (stateless, no I/O)
sender.py             async outbound queue with token-bucket flood control
store.py              in-memory state + periodic disk flush, plus RateLimiter
config.py             config.ini parsing, CLI argparse, module-level constants
botlog.py             logging setup, startup validation, debug/loglevel handlers
hashpw.py             admin password hashing (scrypt/bcrypt/argon2)
secret_store.py       two-tier reversible credential store
audit_log.py          HMAC-chained tamper-evident privileged-action log
metrics.py            optional Prometheus exporter (off by default)
process_lock.py       PID-based single-instance guard

modules/
  base.py             BotModule base class + fetch_json, strip_ctrl, cred, resolve_public
  _netsafe.py         SSRF-safe fetch with DNS-TOCTOU pinning
  geocode.py          location resolution (Nominatim / Zippopotam)
  units.py            dual-unit formatting for weather output
  example.py          copy-and-fill module skeleton
  <60+ command modules>

weather_providers/
  __init__.py          provider registry + 14 public get_* functions
  base.py              frozen normalized dataclasses + WeatherProvider protocol
  _dispatch.py         capability discovery + accuracy/health ranking + fallback
  _health.py           per-provider EMA health score + circuit breaker
  _http.py             capped async HTTP client (aiohttp / requests fallback)
  <32 provider packages>

tests/
  run_tests.py         standalone pre-pytest test runner (regression tests)
  conftest.py          sys.path setup
  test_*.py            41 pytest modules

scripts/
  build-docs.sh        Sphinx HTML + PDF build
  regen-lockfile.sh    lockfile regeneration
  remap-doc-citations.py  doc citation updater
  sbom.sh              software bill of materials
  verify_install.sh    supply-chain install smoke test
```

The import order matters: `config.py` (imports `secret_store`) -> `botlog.py`
(imports from `config`) -> everything else. `config.py` and `botlog.py` both
run validation at import time and can `sys.exit(1)` before the event loop
starts.

---

## What is dangerous

### Code execution surfaces

1. **`.load <module>`** (`admin_cmds.py:432`, `internets.py:462`).
   `exec_module` runs arbitrary Python from any `.py` file in `MODULES_DIR`
   with the bot's full process privileges. The name is regex-constrained and
   path-traversal-checked, but anything already sitting in `modules/` is
   trusted. A compromised file in that directory is full code execution.

2. **`.raw <line>`** (`admin_cmds.py:553`). Injects an unvalidated IRC
   protocol line onto the wire. Only CR/LF/NUL and the 510-byte cap are
   enforced; the command itself (KILL, OPER, SAMODE) is whatever the admin
   types. The audit log records it (with credential redaction), but the
   blast radius is the full privilege of the bot's IRC connection.

3. **The interactive console** (`console.py`). Admin-equivalent capability
   (debug, shutdown) with no authentication. The security boundary is physical
   access to stdin. Run daemonized deployments with `--no-console`.

### State corruption risks

4. **Two instances on the same state files.** The process lock
   (`process_lock.py`) prevents this, but a manual deletion of
   `internets.pid` while the bot is running opens the race. The JSON state
   files use tmp-and-rename, so two writers' renames interleave.

5. **Editing `config.ini` while the bot is running.** The `configparser`
   object (`cfg`) is live in memory. `.rehash` / SIGHUP re-reads it.
   Concurrent edits are not locked; a partial write visible to a rehash
   produces a partial config. Edit, save, then `.rehash`.

### Security invariants that look like simplifications

6. **`is_admin()` hostmask re-check.** (`internets.py:357`). Looks like it
   could be simplified to a set membership test. It cannot. The hostmask
   re-derivation on every call prevents nick-grab session inheritance. The
   `"unknown"` sentinel denial prevents a TOCTOU where the admin quits during
   `verify_password`. Both bugs were observed in production. See
   `docs/security-model.md` section 1.

7. **Auth session drop on NICK/QUIT.** Sessions are dropped, not migrated.
   Migrating would let a malicious server or nick-takeover launder an authed
   session onto an attacker-chosen nick. See `internets.py:1079-1098`.

8. **DNS pinning in `_netsafe.py`.** The global `getaddrinfo` wrapper looks
   invasive. It is the SSRF defense. See ADR-003 in `docs/design-decisions.md`.

9. **The derived-field exclusion in weather gap-fill.** `feels_like_c` and
   `dewpoint_c` look like they should be gap-filled. They must not be. See
   ADR-010.

---

## What should never be touched

See `docs/executive.md`, "What should never change" for the full list. The
short version:

- The `is_admin()` fail-closed hostmask binding
- The `_tls_or_refuse` credential gate
- The netsafe DNS pinning mechanism
- The store's quarantine-on-bad-read behavior
- The audit log HMAC chain (do not downgrade to plain SHA-256)
- The outbound HTTP size-cap discipline (stream + cap before parse)
- The process lock `O_CREAT | O_EXCL` mechanism
- The derived-field invariant in weather gap-fill

---

## Where are the sharp edges

### Hot-reload only refreshes command modules

`.reload weather` re-executes `modules/weather.py` from disk. It does NOT
refresh helper modules (`geocode.py`, `units.py`, `_netsafe.py`, `base.py`)
or `weather_providers/*` because those are cached in `sys.modules`. An edit
to `geocode.py` is invisible until `.restart` (full `os.execv`). This is the
single most common gotcha for a new maintainer.

### Config constants are frozen at import

`config.py` parses `SERVER`, `PORT`, `NICKNAME`, `NS_PW`, `OPER_PW`,
`SERVER_PW`, and other constants at import time. `.rehash` re-reads `cfg` but
does not re-bind these constants. Changing a server address or NickServ
password requires a full restart. The `_cmd_prefix()` function reads the
prefix live (not from the frozen `CMD_PREFIX`) specifically to support rehash;
this is the exception, not the rule.

### The `config.ini` / `config.local.ini` overlay

`config.py`'s `reload_config()` reads both files in order. Re-reading
`config.ini` alone would clobber values that exist only in
`config.local.ini` (e.g., `password_hash`) with the template's empty
placeholders. Every reload path must go through `reload_config()`.

### Lockfile and restart interaction

`os.execv` preserves the PID. The restart path releases the process lock
before `execv`; otherwise the new process image would see its own old PID as
a live holder and refuse to start. See `internets.py:1458-1468`.

### Password length is bytes, not characters

`hashpw.py`'s `check_password` enforces a 128-byte UTF-8 limit, not
128 characters. A non-ASCII passphrase of 128 characters can be 384 bytes
and will be rejected. The bcrypt 72-byte limit is a hard algorithm
constraint, not a tunable. Both limits are enforced at hash time and at
verify time via the same function so the two ends cannot drift.

### scrypt cost is host-dependent

`_best_scrypt_params()` probes descending `(N, r, p)` sets and uses the
first one OpenSSL accepts. A host with a tighter memory cap silently gets a
weaker hash. The CLI prints which parameters were used but does not warn
about degradation.

### `_VALID_HASH_PREFIXES` in `botlog.py` is a separate enumeration

Adding a fourth hash algorithm to `hashpw.py` without updating
`_VALID_HASH_PREFIXES` in `botlog.py:177` makes the bot refuse to start on
a hash that `verify_password` can verify perfectly well.

### Shadow-ban persistence

`.shadow-ban` flushes to `shadow_bans.json` on disk. Bans survive a restart.
The file is not in the config template (`config.ini.example`), and one admin
can shadow-ban another admin (silently locking them out of every command
including `.deauth` and `.shutdown`).

### Module `forget()` coverage is incomplete

`.forgetme` calls `forget()` on every loaded module, but `steam.py` has no
`forget()` override. Its persisted nick-to-SteamID mapping survives
`.forgetme`. Saved locations are erased through `privacy.py` ->
`bot.loc_del()`, not through a module `forget()` hook.

### `sasl_password` secret is defined but never consumed

`sasl_password` is in `KNOWN_SECRETS` and in the config template, but the
SASL PLAIN auth path hardcodes `NS_PW` (the `nickserv_password` value). A
distinct `sasl_password` is silently ignored. See `docs/configuration.md`,
audit finding 2.

### `[weather] units` is a dead key

The config template ships `units = us` but no code reads it. Unit selection
is driven by per-provider API params, not this key. See
`docs/configuration.md`, audit finding 1.

### Metrics endpoint wiring

`metrics.py` defines a full Prometheus exporter. It IS wired into
`internets.py:1377-1384`, gated by `[metrics] enable = true`. The TODO
comment in `metrics.py:9` is stale. The counters/gauges are incremented from
various callsites regardless of whether the HTTP exporter is running.

---

## What technical debt exists

1. **`fetch_json` vs inline streaming.** Many modules use raw `requests` with
   hand-rolled streaming/size-cap logic instead of the shared
   `modules/base.fetch_json` helper. Functionally equivalent (all cap body
   size) but inconsistent with the single-helper convention.

2. **`dice.py` and `bofh.py` have no rate-limit gate**, unlike every other
   command module. Low severity (both are local, no network), but
   inconsistent.

3. **`KEY_ROTATION.md` reference in `hashpw.py:285`.** No such file exists.
   Stale documentation debt.

4. **bcrypt 4.3.0 installed vs 5.0.0 pinned.** The lockfile may lag behind
   `pyproject.toml`'s declared floor. Run `pip install -r requirements.txt`
   to reconcile.

5. **`idlerpg.py` default endpoint is plain HTTP.** The configured default
   `http://idlerpg.rizon.net/xml.php` leaks query params in cleartext.

6. **`dictionary.py` interpolates the raw query word into the URL path**
   without encoding. Host is fixed (low risk), but inconsistent with the
   validation/`quote()` discipline in `pkginfo.py`, `translate.py`, etc.

7. **Console `.debug WEATHER` vs IRC `.debug WEATHER` case handling.**
   The console preserves case but subsystem names are lowercase, so
   uppercase at the console matches no real logger. IRC lowercases the
   entire arg. Use lowercase at the console.

---

## What should be refactored first

Priority order, highest value first:

1. **Consolidate HTTP helpers.** Migrate modules still using raw `requests`
   to `fetch_json`, or factor out the streaming/cap pattern so there is
   genuinely one path.

2. **Add `forget()` to `steam.py`.** Privacy gap: the nick-to-SteamID
   mapping survives `.forgetme`.

3. **Wire `sasl_password` or remove it.** Either implement the documented
   "differs from nickserv" behavior or remove the secret from
   `KNOWN_SECRETS` and the template.

4. **Remove the dead `[weather] units` key** from the template and its
   comment.

---

## What should be monitored

1. **`*.corrupt.*` files in the working directory.** Any quarantine event
   means a state file was corrupt on load. Investigate the cause (power loss,
   disk full, racing instance).

2. **`audit.log` chain integrity.** Run `.audit verify` periodically or
   after any suspect event. A break at a specific index indicates that
   record was tampered with or the log was not cleanly closed.

3. **Provider health via `.providers`.** An admin-only command that dumps
   the per-provider health score, success rate, latency, circuit breaker
   state, and capability matrix. A provider in `open` breaker state for an
   extended period may indicate a revoked API key.

4. **Log for `event=isupport_malformed`.** Indicates the IRC server sent
   a malformed CHANMODES or PREFIX token. MODE-based chanop tracking may
   be degraded until the next clean 005.

5. **Log for `dispatch_budget_exhausted`.** The 45s chain budget was
   consumed before all providers were tried. Either upstream providers are
   slow or the chain is too deep for the budget.

6. **`dropped_messages` in `.stats` or Prometheus.** Non-zero means the
   outbound queue hit its 200-message cap and started discarding. Either
   the bot is sending too much or the network is slow.

---

## Operational procedures

### First-time setup

```bash
git clone <repo>
cd Internets
pip install -r requirements.txt
python -m secret_store init          # creates config.ini from template, 0600
python hashpw.py --algo argon2       # generates admin password hash
# paste the output into config.ini [admin] password_hash
# set [irc] server, port, nickname, realname
# set [secrets] nickserv_password (if needed)
# set API keys in [secrets] as desired
python internets.py
```

### Upgrading

See `docs/deployment.md`, "Upgrade procedure". Read the CHANGELOG first.
Stop gracefully, pull, reinstall deps, merge new template keys, run
`python tests/run_tests.py`, start.

### Backup

Back up together: `config.ini`, `audit.log`, `audit.log.key`, all `*.json`
state files, the `*.bak` and `*.corrupt.*` recovery files, and the rotated
`audit.log.*` segments. `config.ini` contains secrets; handle accordingly.

### Disaster recovery

If a state file is corrupt on startup, the bot quarantines it and starts
with empty state. Check for `<name>.corrupt.<timestamp>` and
`<name>.bak` files. The `.bak` is a one-deep backup from the last
successful write. Manual inspection and restoration is the recovery path.

If the audit log key is corrupt, it is backed up to `audit.log.key.bad`
and a new key is generated. The old log segments are still verifiable with
the old key (if recovered from `.key.bad`); new records start a fresh
chain.

If the process lock file is stale (the bot was `kill -9`'d), the next
start auto-clears it if the PID is dead. If the PID was reused by an
unrelated process, or the lockfile names a different host, delete
`internets.pid` manually after confirming no instance is running.
