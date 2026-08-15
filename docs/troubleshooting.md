# Troubleshooting

Scenario-driven diagnosis. Each entry follows the same shape:

**Symptom** - what you observe. **Probable causes** - ranked, most common first.
**Diagnostic procedure** - what to run or read, in order. **Expected evidence** - the
exact log event, reply text, or file state that distinguishes one cause from another.
**Corrective action** - what to do per cause.

Log event names quoted below are the literal `event=` tokens emitted by the source.
Grep for them. Procedures assume the bot's working directory unless stated; every
path the bot uses resolves against it.

Companion docs: [operations.md](operations.md) for the procedures themselves,
[administration.md](administration.md) for the admin surface,
[configuration.md](configuration.md) for what each key means, and
[internals/](internals/internets.md) for line-level mechanism.

## Before anything else

Three commands answer most first questions:

```text
.health      per-subsystem snapshot (admin, replies privately)
.stats       counters, sender queue depth, RSS, audit record count
.audit 20    recent privileged actions - did someone change something
```

From the shell, the corresponding move is to tail the log and grep for `event=`. If
the process is not running at all, start with
[Config rejected at startup](#config-rejected-at-startup) and
[Process-lock conflict](#process-lock-conflict), which are the two failures that
happen before the loop exists.

Raise verbosity while diagnosing: `.debug on` for everything,
`.debug <subsystem>` for one (`sender`, `store`, `conn`, `sasl`, `modules`,
`dispatch`, `secrets`). Remember that `.loglevel LEVEL` does **not** clear
per-subsystem debug afterwards; only `.rehash` clears both.

(cannot-connect)=
## Cannot connect

**Symptom.** The bot never reaches the network. The log shows repeated
`event=connect_failed`, or it stops after `event=connect_begin` and nothing follows.

**Probable causes.**

1. Wrong `[irc] server` or `port`, or DNS not resolving.
2. Network path blocked (firewall, egress policy, the server refusing the source IP).
3. TLS negotiation failing - see [TLS validation failure](#tls-validation-failure).
4. The server K-lined or G-lined the host, or requires a `server_password` that is
   missing or wrong.
5. SASL failing permanently and the reconnect loop giving up - see
   [SASL failure](#sasl-failure).

**Diagnostic procedure.**

1. Read the connect events in order:
   `grep -E 'event=(connect_begin|connect_ok|connect_failed|reconnect)' <logfile>`.
2. Note the `err=` field on `event=connect_failed`. It carries the exception repr.
3. Reproduce the transport independently:
   `openssl s_client -connect <server>:<port>` (or `nc` for a plaintext port).
4. Confirm the config the process actually read - `config.py` resolves `config.ini`
   against the **working directory**, so a service started from the wrong directory
   reads a different file or none.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| DNS / host wrong | `event=connect_failed ... err=` naming `gaierror` or `Name or service not known` |
| Port closed or filtered | `err=` naming `ConnectionRefusedError` or a timeout |
| TLS problem | `err=` naming `SSLError` or `SSLCertVerificationError` |
| Permanent SASL abort | `event=reconnect_aborted reason=auth_failed` and the loop stops retrying |
| Wrong directory | `SystemExit` at import naming a `config.ini` path you do not expect |

**Corrective action.** Fix the value or the path and restart - `[irc]` constants are
frozen at import and a `.rehash` will not move them. Initial connect failures retry
forever with jittered backoff (15 s doubling to a 300 s cap, plus or minus 25%), so a
transient outage needs no intervention. `event=reconnect_aborted` is the one case
that will not self-heal: it fires only when SASL has failed permanently at least
three times with no NickServ fallback configured, and it exists so the bot stops
hammering the server with bad credentials.

(tls-validation-failure)=
## TLS validation failure

**Symptom.** Connection attempts fail with an SSL error, or the bot connects but the
log carries a loud warning about the TLS posture.

**Probable causes.**

1. The server presents a certificate that does not validate (self-signed, expired,
   wrong hostname, missing intermediate).
2. The server does not support TLS 1.3 and the bot's floor is TLS 1.3.
3. `ssl_verify = false` is set, so validation is off entirely and the warning is the
   symptom rather than the failure.

**Diagnostic procedure.**

1. `grep -E 'event=(tls_unverified|tls_minimum_downgraded|connect_failed)' <logfile>`.
2. Inspect the chain yourself:
   `openssl s_client -connect <server>:<port> -servername <server>` and read the
   verify return code plus the offered protocol version.
3. Check `[irc] ssl` and `ssl_verify` in the config the process actually read.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Chain does not validate | `event=connect_failed` with `SSLCertVerificationError` in `err=` |
| TLS 1.3 floor too high | `openssl` negotiates TLS 1.2 only; connect fails with a protocol error |
| Floor deliberately lowered | `event=tls_minimum_downgraded value=TLSv1.2` at startup |
| Verification disabled | `event=tls_unverified host=... - ssl_verify=false`, logged on **every** connect |

**Corrective action.** Fix the server certificate if you control it, or install the
missing intermediate in the trust store. If the network genuinely cannot do TLS 1.3,
set `INTERNETS_ALLOW_TLS12=1` and accept the logged downgrade. Do not reach for
`ssl_verify = false`: with verification off, every credential the bot sends is
exposed to an on-path attacker, and the bot logs that fact on each connect precisely
so it cannot become invisible. Note also that credential sends are gated
independently - see [SASL failure](#sasl-failure) and
`event=plaintext_cred_refused`.

(sasl-failure)=
## SASL failure

**Symptom.** The bot connects but is not identified to services, or the log shows
`event=sasl_failure`, or the reconnect loop aborts.

**Probable causes.**

1. Wrong `nickserv_password`, or the account does not match the nick.
2. The server does not offer the `sasl` capability.
3. The link is not TLS, so the credential send was refused before it happened.
4. The server sends a multiline `CAP LS 302` reply - see the defect note below.

**Diagnostic procedure.**

1. `grep -E 'event=(sasl_authenticate|sasl_success|sasl_failure|caps_requested)'
   <logfile>`.
2. Check for `event=plaintext_cred_refused cred=...` immediately before, which means
   the credential was never sent.
3. Enable protocol tracing: `.debug sasl` and `.debug conn`, or start with
   `--debug-file /path/to/debug.log`, which captures everything at DEBUG regardless of
   the running base level. Read the `<<` and `>>` lines around CAP negotiation.
4. Verify the password resolves at all:
   `python -m secret_store get nickserv_password` (prints presence and length, never
   the value).

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Bad credential | `event=sasl_failure ... permanent=True` after a 904 or 905 numeric |
| Transient services failure | `event=sasl_failure ... permanent=False` (902) |
| No SASL on this server | no `sasl` in the `caps_requested` set; CAP END sent immediately |
| Not TLS | `event=plaintext_cred_refused cred=sasl` and no AUTHENTICATE on the wire |
| Repeated permanent failure | `event=reconnect_aborted reason=auth_failed` |

**Corrective action.** Correct the credential and **restart** - `NS_PW` is an
import-time constant and a `.rehash` will not refresh it. SASL failure is not fatal by
itself: the bot completes CAP END and falls back to NickServ IDENTIFY after the MOTD,
so a working `nickserv_password` still identifies the bot. It is only the combination
of permanent SASL failure and no NickServ fallback that aborts the reconnect loop.

:::{warning}
**Known defect (multiline CAP LS).** The bot requests `CAP LS 302` but mishandles the
multiline reply. On a continuation line the `*` marker is parsed as a capability
token, the first real capability keeps a leading colon and can never match the desired
set, and each line is answered independently - so a `CAP REQ`, or a premature
`CAP END`, can fire before the server has finished listing. Servers whose capability
list fits one line behave correctly. If SASL is offered but never negotiated on a
particular network, this is the first thing to suspect. Recorded in
[internals/internets.md](internals/internets.md#findings) and the reconstruction
findings ledger.
:::

(nickserv-auth-failure)=
## NickServ authentication failure

**Symptom.** The bot is connected but unidentified: no vhost or cloak, channels with
`+r` reject it, and the rejoin is late or incomplete.

**Probable causes.**

1. Wrong or unset `nickserv_password`.
2. The link is not TLS, so IDENTIFY was refused.
3. Services were slow or down, and the 10-second identify wait expired.
4. The services nick is not the configured one on this network.

**Diagnostic procedure.**

1. `grep -E 'NickServ|event=rejoin|event=plaintext_cred_refused' <logfile>`.
2. Check the identify detection path: the bot marks itself identified on either a
   `900` numeric or a NickServ NOTICE containing `identified` or `recognized`. A
   network whose NOTICE wording differs will never flip the flag even after a
   successful identify.
3. Confirm from the network side: `/msg NickServ STATUS <botnick>` from your own
   client.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Identified normally | `NickServ: identified (900)` or `NickServ: identified (NOTICE)` |
| Never sent | `event=plaintext_cred_refused cred=nickserv` |
| Services slow or wording mismatch | `event=rejoin nickserv=timeout wait=10.0s` |
| Healthy but late | `event=rejoin nickserv=confirmed` |

**Corrective action.** Fix the credential and restart. `event=rejoin nickserv=timeout`
is a warning, not a failure - the bot proceeds to join anyway, so channels requiring
identification may reject that first attempt. If the timeout is chronic on a slow
network, expect the first rejoin after each reconnect to be lossy and check
[Not joining channels](#not-joining-channels).

(not-joining-channels)=
## Not joining channels

**Symptom.** The bot is connected and identified but is absent from one or more
channels, and the channel does not come back after a restart.

**Probable causes.**

1. The channel was **dropped from saved state** by a join error and is now gone from
   `channels.json`.
2. The channel is invite-only (`+i`) and the INVITE request to services was not
   honored.
3. The bot is banned (`+b`), key-protected (`+k`), or the channel is full (`+l`).
4. Rejoin ran before services granted the cloak (see
   [NickServ failure](#nickserv-authentication-failure)).
5. The channel name in saved state does not match the channel-shape regex.

**Diagnostic procedure.**

1. Read `channels.json` - it is the rejoin list, and it is authoritative.
2. `grep -E 'Cannot join|event=rejoin' <logfile>`.
3. Try the join by hand from an admin session: `.raw JOIN #channel` and read the
   server's numeric reply from the `<<` debug lines.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Invite-only | `Cannot join #chan (invite-only) - asking <services> for INVITE` |
| Any other join error | **no log line at all**; the channel simply vanishes from `channels.json` |
| Rejoin ran unidentified | `event=rejoin nickserv=timeout` immediately before |

**Corrective action.** Re-join from an admin session (`.raw JOIN #chan`, or invite the
bot); a successful join persists the channel back into `channels.json` immediately.

Be aware of the asymmetry: numerics 403, 405, 471, 474, 475, and 476 - no such
channel, too many channels, channel full, banned, bad key, bad channel mask - all
**silently** discard the channel from saved state with no log message. The only
evidence is the file. If channels are disappearing across restarts and the log is
clean, this is why: diff `channels.json` before and after a reconnect.

(command-missing)=
## Command missing or not responding

**Symptom.** A command that should exist produces `no command '<x>' loaded - try
.help`, or produces nothing at all.

**Probable causes.**

1. The owning module is not loaded.
2. The module is loaded but **unconfigured** (missing API key), so it is hidden from
   non-admins.
3. The command word collides with a core command, which always wins dispatch.
4. The command prefix changed under you.
5. You are shadow-banned.
6. The command was dispatched but timed out or raised.
7. The bot is at its concurrent-task cap.

**Diagnostic procedure.**

1. `.modules` - is the module in the loaded list? It is public, so this works
   unauthenticated.
2. `.help <module>` as an **admin** - unconfigured modules are visible to admins
   everywhere, and the listing carries a `(hidden, no key: ...)` note.
3. Check the live prefix. `.rehash` can change it, and the core reads it per dispatch.
4. `grep -E 'event=(dispatch_rejected|command_timeout|channel_throttled)' <logfile>`.
5. `.shadow-list` from an admin session.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Not loaded | absent from `.modules`; `.help <name>` says no command loaded |
| Unconfigured | visible only to admins, badged `unconfigured` in `.health` |
| Core collision | the command exists in `_CORE`; `.help` may show the module's version but dispatch runs core's |
| Shadow-banned | **nothing at all** - no reply, no log, indistinguishable from the bot being offline |
| Task cap reached | `bot is busy - try again shortly.` and `event=dispatch_rejected reason=at_capacity` |
| Handler timeout | `'<cmd>' timed out.` and `event=command_timeout` |
| Handler raised | `internal error processing '<cmd>' - see log for details.` plus a traceback in the log |

A concrete instance of the core-collision row, verified in this documentation pass:
`modules/health.py` registers `uptime`, but `_CORE` already maps `uptime` to the
admin-gated core handler and `_dispatch()` checks `_CORE` first. The module's intended
**public** `.uptime` is therefore unreachable, and a non-admin asking for it gets the
auth prompt. The loader's collision check only compares module against module, so
nothing warns at load time. See
[operations.md](operations.md#health-checking).

**Corrective action.** Load the module (`.load <name>`), supply the missing key (see
[API-backed command unavailable](#api-backed-command-unavailable)), or rename the
colliding module command. A shadow-ban is silent by design - check `.shadow-list`
before assuming a defect. Persistent `event=dispatch_rejected reason=at_capacity`
means 50 command tasks are live simultaneously; look for a wedged handler rather than
raising the cap.

(module-failed-to-load)=
## Module failed to load

**Symptom.** `.load <name>` replies `Error loading '<name>' - see log for details.`,
or a module in `AUTO_LOAD` is missing after startup.

**Probable causes.**

1. Python syntax or import error inside the module.
2. Name rejected by the loader's grammar.
3. Path escapes `MODULES_DIR`.
4. The module does not expose `setup(bot)`.
5. A command-name collision with an already-loaded module.
6. `on_load()` raised.

**Diagnostic procedure.**

1. `grep 'event=module_load_failed' <logfile>` - the `err=` field carries the real
   reason. The IRC reply is deliberately generic and tells you nothing.
2. Compile it standalone: `python -m py_compile modules/<name>.py`.
3. Check the name against `^[a-z][a-z0-9_]*$` - no dots, no slashes, no uppercase.
4. `.modules` - is another module already registering the same command word?

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Syntax or import error | `event=module_load_failed name=... err=<exception>`; `py_compile` fails identically |
| Bad name or path | load fails before any import; nothing executed |
| No `setup(bot)` | `err=` naming the missing contract |
| Command collision | `err=` naming the conflicting command and its owning module |
| `on_load()` raised | nothing is registered, but `setup()` side effects already happened |

**Corrective action.** Fix the module and `.load` again. Note two loader properties
that shape recovery: a failed load registers nothing, so there is no half-loaded state
to clean up on the command side, but `setup()` side effects that already ran are not
undone. And a failed `unload` leaves the module **fully** loaded with its commands
intact, deliberately, rather than stranding orphaned commands - so a `.reload` that
reports failure may have changed nothing at all.

If the edit was to a helper (`modules/base.py`, `geocode.py`, `units.py`) or anything
under `weather_providers/`, `.reload` will not pick it up at all: those are cached in
`sys.modules`. Restart. See
[administration.md](administration.md#admin-live-vs-restart).

(api-backed-command-unavailable)=
## API-backed command unavailable

**Symptom.** A command that needs an API key is hidden, replies that it is not
configured, or consistently fails upstream.

**Probable causes.**

1. The key is not set in any tier.
2. The key is set but `config.ini` permissions block the file tier.
3. The value is a template placeholder, which is filtered as unset.
4. The key is set but wrong or expired, so every upstream call 401s.
5. The upstream is rate-limiting or down.

**Diagnostic procedure.**

1. `python -m secret_store list` - shows `env`, `file`, or `(unset)` per known secret.
   It never prints values.
2. `python -m secret_store status` - shows the secrets file path, existence, and the
   permission verdict with a remediation hint.
3. `.health` as admin - the module list badges each module `ok`, `unconfigured`, or
   `?`.
4. `grep -i 'REFUSING to read' <logfile>`.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Unset | `(unset)` in `list`; module badged `unconfigured` in `.health` |
| Bad permissions | `REFUSING to read <path> - mode is 0oNNN, expected 0o600 - run chmod 600 <path>` |
| Placeholder | reported unset despite a value being present in the file |
| Wrong key | upstream 401 or 403 in the module's log lines; for weather, the provider's breaker trips immediately |

**Corrective action.** Set the key with `python -m secret_store set <name>` (it prompts
via `getpass` so the value stays out of shell history), or export
`INTERNETS_<NAME_UPPER>`. Fix permissions with `chmod 600 config.ini`. Most module keys
resolve through an **uncached** lookup, so a rotated key is picked up by the next call
with no restart; environment-tier values always need a restart, and a module that
captured the key at load time needs a `.reload`.

:::{warning}
**Known behavior (stricter is also refused).** The permission check is an equality
test for 0600, not a "no group or world bits" test. A read-only 0400 `config.ini` is
**refused** exactly like a world-readable one, and the bot silently runs keyless.
Recorded in [internals/secret_store.md](internals/secret_store.md#findings).
:::

(weather-provider-benched)=
## Weather provider failing or benched

**Symptom.** Weather commands fall through to a different provider than expected, or
report that all providers failed. `.providers` shows a provider with a low score or an
open circuit.

**Probable causes.**

1. Upstream outage or timeouts, tripping the circuit breaker.
2. Bad or expired API key, which trips the breaker immediately on 401 or 403.
3. Rate limiting (429), which decays the provider's score.
4. The provider is not configured, so it never registered.
5. The provider does not support the requested capability.

**Diagnostic procedure.**

1. `.providers` (admin) - per-provider health summary plus the capability chains.
2. `.debug weather` and re-run the command; the dispatcher logs its per-provider
   decisions at DEBUG.
3. Read the dispatcher's own messages in the log.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Breaker open | `<pid>: <capability> skipped - circuit open` at DEBUG; score pinned to 0.00 |
| Auth failure | an ERROR the first time a 401 or 403 is seen; the breaker opens immediately, bypassing the threshold |
| No usable data | `<pid>: <capability> no usable data - trying next` |
| Whole chain exhausted | `All providers failed for '<capability>'` |
| Forced provider unusable | `force_provider ... circuit is open (cooling down)` or `... does not support '<capability>'` |
| Not registered | absent from `.providers` entirely |

**Corrective action.** No provider is benched permanently. The breaker opens after 5
consecutive failures inside a 60-second window, stays open for a 60-second cooldown,
then admits exactly one probe: a successful probe closes it, a failed probe re-opens it
for another full cooldown. Fixing an API key therefore recovers the provider with no
restart. Rate-limit penalties decay with a 300-second half-life, and each success
additionally steps the counter down, so a 429 storm clears itself once the upstream
recovers.

A merely degraded provider (sagging score, breaker closed) is **not** benched: static
accuracy dominates the sort and health only breaks ties. If a provider you expect to
be first is consistently second, check its accuracy rank before assuming a health
problem. Detail: [internals/weather-providers/health.md](internals/weather-providers/health.md).

:::{warning}
**Known defect (key leak in the failure reply).** This class of failure has a verified
counterpart in `modules/stocks.py`, which appends `str(exception)` to the "all
providers failed" reply. urllib3 transport errors embed the full request query,
including `token=` and `apikey=` parameters, so a network outage while finance keys are
configured publishes those keys to the channel. `sender.redact_secrets()` is log-only
and does not scrub PRIVMSG. Until it is fixed, treat any stocks outage as a key
exposure and rotate. Verified and reproduced by the orchestrator; recorded in the
reconstruction findings ledger.
:::

(rate-limited)=
## Rate limited

**Symptom.** The bot answers `slow down`, or drops commands silently, or an
API-backed command refuses before making any request.

**Probable causes.**

1. Per-nick flood gate (default 3 s between commands).
2. Per-channel burst gate (20 commands per 10 s across all nicks).
3. Per-nick API window, consumed by expensive geocode and weather paths.
4. Outbound token bucket - not a rejection, a delay. See
   [No outbound replies](#no-outbound-replies).

**Diagnostic procedure.**

1. Note whether you got a **reply** or **silence**. That is the discriminator.
2. `grep 'event=channel_throttled' <logfile>`.
3. Check `[bot] flood_cooldown` and `api_cooldown` in the config.
4. `.stats` for sender queue depth, to separate rejection from delay.

**Expected evidence.**

| Gate | Evidence |
|---|---|
| Per-nick flood | notice `<nick>: slow down (Ns cooldown)`; admins bypass entirely |
| Per-channel burst | **silence**, plus `event=channel_throttled channel=... nick=... cmd=...` |
| Argument too long | `input too long (max 400 chars).` |
| Task cap | `bot is busy - try again shortly.` |
| API window | module-specific refusal before any upstream request |

**Corrective action.** The channel gate is deliberately silent - a throttle notice
would add to the flood it exists to contain - so check the log, not the channel. Both
per-nick cooldowns are floored at 1 second in `config.py`, so a config value of 0
cannot disable them. Raising a cooldown requires a restart; the values are
import-frozen. Note also that an over-budget channel does **not** record the refused
attempt, so an attacker cannot keep the window pinned full by continuing to spam.

(config-rejected-at-startup)=
## Config rejected at startup

**Symptom.** The process exits immediately. Nothing connects, no PID file appears.

**Probable causes.**

1. `config.ini` missing or unreadable in the working directory.
2. Empty `command_prefix`.
3. `password_hash` with an unrecognized prefix.
4. A mode string containing characters outside the allowlist.
5. A missing section or key, or a non-integer numeric.
6. Unknown CLI arguments.

**Diagnostic procedure.**

1. Read the exit output. `config.py` and `botlog.py` both fail at **import**, before
   the loop, so the message is on stderr and not in the log file.
2. `pwd` - confirm the working directory holds the `config.ini` you meant.
3. `python -c "import config"` from the deployment directory reproduces the import-time
   failure in isolation.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Missing config | `SystemExit` naming the resolved path and pointing at `python -m secret_store init` |
| Empty prefix | `SystemExit` explaining that an empty prefix makes every message a command |
| Bad hash prefix | CRITICAL then exit 1; the invalid value is never echoed |
| Bad mode string | CRITICAL then exit 1 |
| Missing section or key | raw `KeyError: 'irc'` traceback, no curated message |
| Bad numeric | raw `ValueError` from `int()` |
| Unknown CLI arg | argparse usage and `SystemExit(2)`, during import |

**Corrective action.** Fix the file and start again. Note that valid prefixes for
`password_hash` are exactly `scrypt`, `bcrypt`, and `argon2`; an empty hash is
**allowed** and only warns, because that is the documented first-run state before you
have run `hashpw.py` - the bot runs with admin auth disabled. The world-readable
`config.ini` check is also advisory only, warning and suggesting `chmod 640`, never
fatal.

:::{warning}
**Known defect (rehash bypasses the prefix guard).** The empty-`command_prefix` guard
runs at import only and is **not** re-applied by `reload_config()`. A `.rehash` can
load an empty prefix into the live config, after which every channel message parses as
a command. Recorded in [internals/config.md](internals/config.md#findings).
:::

(secret-unavailable)=
## Secret unavailable or wrong permissions

**Symptom.** A credential the config clearly contains is treated as unset: the bot
runs keyless, or refuses to send a password.

**Probable causes.**

1. `config.ini` permissions are not exactly 0600.
2. The value is a placeholder from the template.
3. The value lives in the wrong tier for the consumer (import-frozen versus live).
4. An environment variable is shadowing the file value.
5. The link is not TLS, so the credential was refused at send time rather than lookup
   time.

**Diagnostic procedure.**

1. `python -m secret_store status` then `python -m secret_store list`.
2. `ls -l config.ini` - the check is equality with 0600, both directions.
3. `grep -E 'REFUSING to read|event=plaintext_cred_refused' <logfile>`.
4. `env | grep INTERNETS_` - the environment tier wins over the file tier.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Permissions | `REFUSING to read <path> - mode is 0oNNN, expected 0o600 - run chmod 600 <path>` |
| Placeholder | `list` reports the name unset despite a value on disk |
| Env shadowing | `list` reports backend `env` for a name you expected to come from the file |
| Not TLS | `event=plaintext_cred_refused cred=<name>` and no credential on the wire |

**Corrective action.** `chmod 600 config.ini`. Replace placeholders
(`changeme`, `your-key-here`, `set-via-secret-store`, `todo`, `xxx`, and similar) with
real values; they are filtered as unset in both tiers, deliberately, so a template
value can never reach an outbound request. Unset the shadowing environment variable, or
accept it as the source of truth. And remember which values are frozen: `NS_PW`,
`SERVER_PW`, and `OPER_PW` are resolved once at import, so rotating one needs a
restart even though `secret_store.get()` itself is uncached.

Never create, restore, or hand-edit `config.ini` or `config.local.ini` to work around
this; they are not in the repository and must not be.

(process-lock-conflict)=
## Process-lock conflict

**Symptom.** Startup exits 1 with
`Another bot instance is already running: <reason>`.

**Probable causes.**

1. Another instance genuinely is running.
2. The previous instance was killed and its PID has been reused by an unrelated live
   process.
3. The lockfile was written by a **different host** on shared storage.
4. A `.restart` failed to release the lock before exec'ing.
5. The lockfile directory is unwritable.

**Diagnostic procedure.**

1. `cat internets.pid` - the format is `pid|start_time|hostname`.
2. `ps -p <pid> -o pid,user,cmd` - is it the bot, or something else?
3. Compare the hostname field with `hostname`.
4. `grep 'event=restart_lock_release_failed' <logfile>`.

**Expected evidence.**

| Cause | Reason string / evidence |
|---|---|
| Genuine live holder | reason names the pid, host, and start time |
| Lost the create race | `lockfile <path> appeared during acquire` |
| Cannot create | `could not create lockfile <path>: <OSError>` |
| Foreign host | the hostname field differs; no liveness probe is even attempted |
| Failed restart release | `event=restart_lock_release_failed` in the previous run's log |

**Corrective action.** If a bot is genuinely running, stop it first. A **dead** PID on
the same host needs no action - stale detection unlinks the file and proceeds
automatically, including after `kill -9`. The two cases that cannot self-clear are PID
reuse and a foreign hostname: verify no instance is running, then delete
`internets.pid` by hand. Note that a PID owned by another user probes as *alive*
(`PermissionError` is read conservatively), so a recycled PID under a different account
looks exactly like a live holder.

(state-restoration-failure)=
## State restoration failure or corrupt state file

**Symptom.** Saved locations, the channel list, user tracking, or shadow-bans come
back empty after a restart. A `*.corrupt.*` file appears next to the state files.

**Probable causes.**

1. The file failed integrity validation and was quarantined.
2. The file exceeded the 10 MiB read cap.
3. A crash or power loss truncated the newest write.
4. `shadow_bans.json` failed to parse (handled differently from the store files).
5. The bot is running from a different working directory than before.

**Diagnostic procedure.**

1. `ls -l *.json *.corrupt.* *.bak`.
2. `grep 'Store:' <logfile>` - the quarantine message names the reason and the
   destination.
3. `.health` for the dirty-flag section, and `.shadow-list` for shadow-bans.
4. `pwd` - state paths default to the working directory.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Quarantined | `Store: <path> unusable (<reason>) - quarantined to <path>.corrupt.<ts>` |
| Quarantine itself failed | `Store: <path> unusable (<reason>); quarantine failed: <err>` |
| Shadow-bans lost | a warning only; the list degrades to empty and the bot continues |
| Flush failing | `event=store_flush_failed err=...`, dirty flag stays set, retried every 30 s |

**Corrective action.** Stop the bot, then recover by hand: inspect the
`.corrupt.<timestamp>` file, or restore `<name>.bak`, which holds the previous good
version, and rename it into place. Quarantine exists precisely so the next flush cannot
overwrite the only copy - never delete a `.corrupt.*` file until you have confirmed the
live file loads.

A legacy v1 file (a bare payload with no `schema` key) is accepted silently and
rewritten as v2 on the next flush; that is an upgrade, not corruption. Worst-case loss
on a hard crash is about 30 seconds of unflushed mutations, since the flush thread runs
on a 30-second interval and there is no `fsync` before the atomic rename.

:::{warning}
**Known defect (backup permissions).** `<name>.bak` is written without a chmod, so on
first creation it takes umask-default permissions (typically 0644) while the live file
is 0600. The PII in `users.json` is world-readable in `users.json.bak`. Verified;
recorded in the findings ledger and [internals/store.md](internals/store.md#findings).
`chmod 600 *.bak` after any recovery.
:::

(no-outbound-replies)=
## No outbound replies

**Symptom.** The bot clearly receives commands - the log shows them dispatched - but
nothing reaches the channel, or replies arrive very late.

**Probable causes.**

1. Token-bucket throttling under sustained load (delay, not loss).
2. Send queue full, dropping priority-1 messages (loss).
3. The transport is closing or dead, so lines are silently discarded.
4. A reconnect replaced the sender and orphaned queued messages.
5. The target was rejected before enqueue (empty, or containing a space).

**Diagnostic procedure.**

1. `.stats` - sender queue depth and the dropped-message counter.
2. `grep -E 'Send queue full|Send error|Drain error' <logfile>`.
3. `.debug sender` and watch the `>>` lines: they are written at the moment of the
   buffered write, so their absence means the message never reached `_write_line()`.
4. Correlate with `event=connection_lost` and the reconnect sequence.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Throttling | queue depth non-zero and draining; roughly one line per 1.5 s after a 5-line burst |
| Queue overflow | `Send queue full - dropping message` and a climbing dropped counter |
| Priority-0 pressure | `Send queue full - UNABLE to enqueue priority-0 message` (loud, rare) |
| Transport closing | `>>` lines present but nothing arrives; `Send error:` or `Drain error:` warnings |

**Corrective action.** Understand the policy before changing anything: priority-1
traffic gets a 5-line burst then one line per 1.5 seconds (about 40 messages a minute
sustained), while priority-0 protocol traffic (PONG, CAP, NICK, PASS, AUTHENTICATE,
QUIT, keepalive PING) bypasses the bucket entirely and is never dropped in favor of
chat. That is deliberate: dropping a PONG causes a ping timeout, a disconnect, and a
reconnect storm, which is strictly worse than losing one chat line. A module that
produces long multi-line output will be throttled, and that is working as intended.

:::{warning}
**Known gap (unaccounted drops).** While the writer is closing, the drain loop keeps
consuming and silently discarding messages - and still spends priority-1 tokens -
with no drop accounting, so the counters under-report. `Sender.start()` likewise
discards anything enqueued before the swap. Recorded in
[internals/sender.md](internals/sender.md#findings). Do not treat a zero dropped
counter as proof that nothing was lost across a reconnect.
:::

(metrics-endpoint-unreachable)=
## Metrics endpoint unreachable

**Symptom.** A scrape of `/metrics` times out or is refused.

**Probable causes.**

1. Metrics are not enabled - the default.
2. The exporter failed to start.
3. The bind host was rejected by the unspecified-address guard.
4. The port is bound but not reachable from the scraper's network position.
5. A previous scrape stalled and is blocking the single-threaded server.

**Diagnostic procedure.**

1. `grep -E 'event=(metrics_enabled|metrics_start_failed)' <logfile>`.
2. `curl -sv http://127.0.0.1:9779/metrics` **from the bot host**.
3. `ss -lntp | grep 9779`.
4. Check `[metrics] enable`, `host`, and `port` in the config the process read.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Not enabled | neither event present; no listener |
| Guard rejected the host | `event=metrics_start_failed` with a `ValueError` about an all-interfaces bind |
| Bind failed | `event=metrics_start_failed` with an `OSError` (port in use, permission) |
| Running normally | `event=metrics_enabled`; `curl` returns `text/plain; version=0.0.4` |
| Wrong path | any path other than `/metrics` returns 404 with body `not found` |

**Corrective action.** Enable it in config and **restart** - `[metrics]` is read once
in `_main()`. Bind `127.0.0.1` and front it with an authenticating reverse proxy for
off-host scraping; the endpoint has no authentication of its own. Note that the guard
rejects only *unspecified* addresses (`0.0.0.0`, `::`, `::0`, IPv4-mapped equivalents,
empty or whitespace host); a specific routable IP or a hostname **passes and binds**
despite an error message that claims loopback-only. Metrics failures are non-fatal by
design and never affect the bot.

If exactly one scrape hangs and subsequent ones queue behind it, that is the
single-threaded `HTTPServer` with no socket timeout. Kill the stalled client.

:::{warning}
**Known defect (constant zeros).** Six of the ten registered metrics have no update
call site: `internets_provider_calls_total`, `internets_provider_quota_used`,
`internets_module_loaded`, `internets_provider_active`,
`internets_sender_queue_depth`, and `internets_authed_admins_count`. They render as a
constant `0`, which reads as healthy-but-idle rather than not-instrumented. An alert
on any of them will never fire. Recorded in
[internals/metrics.md](internals/metrics.md#findings).
:::

(audit-verify-broken-chain)=
## Audit verify reports a broken chain

**Symptom.** `.audit verify` answers `audit chain BROKEN at record index N.`, or
`.health` reports `BROKEN at record index N`.

**Probable causes.**

1. A corrupt or truncated line at index N (crash during append, disk error).
2. The HMAC key was regenerated, so all prior v2 records fail under the new key.
3. `audit.log` was edited by hand.
4. Two processes wrote the same log concurrently.
5. Deliberate tampering.

**Diagnostic procedure.**

1. Find the record: `sed -n "$((N+1))p" audit.log` (the index is zero-based over
   non-blank lines).
2. Validate it as JSON, and compare its `prev_hash` with the previous record's
   `this_hash`.
3. `ls -l audit.log.key audit.log.key.bad` - a `.bad` file means the key was
   regenerated at some point.
4. `grep -i 'audit' <logfile>` for key-load errors and for the warnings `_audit()`
   emits when a write fails.
5. Check whether a second process could have been running: see
   [Process-lock conflict](#process-lock-conflict).

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Corrupt line | the line at index N is not valid JSON, or is truncated |
| Key regenerated | `audit.log.key.bad` exists; **every** v2 record fails, so N is early |
| Hand edit | the line parses but its recomputed digest does not match |
| Concurrent writers | interleaved records; no cross-process lock exists in the audit log itself |

**Corrective action.** There is no repair function, and there should not be one: the
chain's value is that a break is visible. Preserve the file as evidence, move it aside,
and let a fresh chain start. If the key was regenerated, the prior records can be
verified again only by restoring `audit.log.key.bad` as the key. An unreadable existing
key makes `record()` fail closed rather than regenerate, exactly so this does not happen
silently.

:::{warning}
**Known defect (downgrade forgery).** `verify()` selects the hash algorithm from each
record's own `v` field, and a record lacking `v == 2` is verified with **unkeyed**
SHA-256 at any chain position. Anyone with write access to `audit.log` alone - no key
required - can truncate at any record and append forged legacy-format records that
chain cleanly, and `verify()` will report the chain intact. An `intact` verdict is
therefore evidence against accidental corruption, not against a motivated local
writer. Rotated segments are never verified or displayed at all. Verified by the
orchestrator; recorded in the findings ledger and
[internals/audit_log.md](internals/audit_log.md#findings).
:::

(ci-red-after-dependency-change)=
## CI red after a dependency change

**Symptom.** The Tests workflow fails after a dependency bump, most often in the
install step, and most often on the older Python legs.

**Probable causes.**

1. `requirements.lock` was regenerated on the wrong Python version, so marker-gated
   transitives are missing.
2. `requirements.txt` changed without regenerating the lock, or only one of the two
   was committed.
3. A floor in `pyproject.toml` extras drifted below the `requirements.txt` policy.
4. A genuine upstream breaking release.

**Diagnostic procedure.**

1. Read the failing job's **install** step, not the test output. On Windows legs the
   real failure is usually several steps earlier.
2. `head -3 requirements.lock` - the header states which Python generated it.
3. Diff `requirements.txt` against `requirements.lock` for anything present in one and
   not the other.
4. Compare `pyproject.toml` extras floors against `requirements.txt`.

**Expected evidence.**

| Cause | Evidence |
|---|---|
| Wrong-Python lock | `pip install -r requirements.lock --require-hashes` fails with "all requirements must have their versions pinned", naming the missing transitive |
| Windows masking | the install step reports success and the job fails later with `ModuleNotFoundError: No module named 'bcrypt'` or `'argon2'` |
| Extras floor drift | no CI failure at all; caught only by review |
| Upstream break | the failure is in test execution, not install, and is version-specific |

**Corrective action.** Regenerate with `scripts/regen-lockfile.sh`, which **must**
resolve on Python 3.10 - the lowest supported version - because conditional transitive
dependencies gated `python_version < "3.11"` are otherwise silently omitted and break
the 3.10 legs. Commit `requirements.txt` and `requirements.lock` together, always.

:::{warning}
**Known defects (both live).** The committed `requirements.lock` was generated on
Python 3.14, violating the resolve-on-3.10 contract, and omits `typing_extensions>=4.4`
pulled by `aiohttp`. Every Python leg below 3.13 fails the `--require-hashes` install,
and the Tests workflow has been red on `main` since 2026-08-13. Separately, the
tests.yml install step runs three pip commands in one block; on Windows the default
pwsh shell does not stop on a failing command, so the install failure is not the
reported failure and the job dies confusingly later. Both verified; recorded in the
findings ledger and
[internals/ci-and-packaging.md](internals/ci-and-packaging.md#findings).
:::

Two CI scopes worth knowing before you chase a false lead: the coverage gate is
**core-only** (it omits `modules/`, `weather_providers/`, `internets.py`, and
`console.py`), and `pip-audit` runs against `requirements.lock` only, never the extras
floors. A green security job says nothing about `pip install internets-irc[weatherkit]`.
