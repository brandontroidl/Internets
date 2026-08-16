# Incident response

Runbooks for security incidents on a running Internets deployment. Each one is
written to be executed under pressure: a detection section with the literal log
strings and command output this bot actually produces, then containment,
evidence preservation, eradication, recovery, and follow-up in the order you
should do them.

Every runbook ends with a section titled **What this bot cannot tell you**.
Read it. The bot's evidence surface is narrow in specific, knowable ways, and a
runbook that lets you believe otherwise is worse than no runbook: it converts an
unanswered question into a false negative.

Scope: this document is for a deployment you own and operate. It assumes shell
access as the bot user on the host, and the deployment directory as the working
directory for every command shown. Reporting a vulnerability in the software
itself is [SECURITY.md](security-policy.md); the design-level control inventory
is [security-model.md](security-model.md); routine procedures are
[operations.md](operations.md).

| Subsystem a runbook touches | Reference |
|---|---|
| Admin auth, sessions, `.raw` | [internals/admin_cmds.md](internals/admin_cmds.md) |
| Audit chain and its key | [internals/audit_log.md](internals/audit_log.md) |
| Secret resolution tiers | [internals/secret_store.md](internals/secret_store.md) |
| Module loading | [internals/internets.md](internals/internets.md) |
| State files and quarantine | [internals/store.md](internals/store.md) |
| Metrics exporter | [internals/metrics.md](internals/metrics.md) |

(ir-first)=
## Before you touch anything

Containment usually destroys evidence. A restart clears the in-memory admin
session table and the nick-to-hostmask cache; a rotation overwrites the value
you would want to search history for; a `.rehash` clears every session including
the one you would have wanted to identify.

Two minutes of copying costs nothing and is not recoverable later:

```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ~/ir-$ts && chmod 700 ~/ir-$ts
cp -a audit.log audit.log.key audit.log.* internets.log* ~/ir-$ts/ 2>/dev/null
cp -a config.ini config.local.ini ~/ir-$ts/ 2>/dev/null
cp -a *.json *.json.bak *.json.corrupt.* ~/ir-$ts/ 2>/dev/null
ps -eo pid,lstart,user,args | grep -F internets.py > ~/ir-$ts/ps.txt
ls -la > ~/ir-$ts/dir-listing.txt
sha256sum ~/ir-$ts/* > ~/ir-$ts/SHA256SUMS
```

The copy contains live credentials and PII. Keep it at 0700, off the host if the
host itself is suspect, and destroy it on a schedule you decide.

Two per-incident captures worth taking while the process is still alive, because
they do not exist anywhere on disk:

```bash
pid=$(cut -d'|' -f1 internets.pid)
tr '\0' '\n' < /proc/$pid/environ | grep '^INTERNETS_' > ~/ir-$ts/bot-env-names.txt
ls -l /proc/$pid/cwd /proc/$pid/exe > ~/ir-$ts/proc.txt
```

The first is the only authoritative answer to "which secrets is the running bot
taking from the environment rather than the file", and it prints values as well
as names - treat the output as a secret. The second answers "which directory and
which interpreter is this process actually using", which matters because every
path the bot uses resolves against its working directory
([deployment.md](deployment.md#the-deployment-directory-and-why-it-matters)).

(ir-keyleak)=
## 1. A finance API key was published to a channel

**This is a live defect, not a hypothetical.**
`modules/stocks.py - _try_providers()` builds its failure reply from
`str(exception)` for every provider that failed, and all three finance providers
carry their credential in the query string. See
[known-issues.md](known-issues.md) item 1.

### Detection signals

The channel reply is the signal. It looks like this, in the channel where
somebody typed `.stock` or `.crypto`:

```text
all providers failed for 'AAPL' (finnhub: 401 Client Error: Unauthorized for
url: https://finnhub.io/api/v1/quote?symbol=AAPL&token=<KEY>; alphavantage:
HTTPSConnectionPool(host='www.alphavantage.co', port=443): Max retries exceeded
with url: /query?function=GLOBAL_QUOTE&symbol=AAPL&apikey=<KEY>)
```

Verified live: `requests`' `raise_for_status()` renders the full prepared URL,
query string included, into the exception message.

The trigger most operators expect is a network outage. The more likely trigger,
and the more dangerous one, is an **HTTP 401 or 403 from an expired, revoked, or
quota-exhausted key**: the moment a key stops working the bot starts publishing
it. That makes the failure self-amplifying, because a key you rotated
incorrectly is a key that will now be printed.

Same event, in the bot log at DEBUG under the `internets.stocks` logger, only if
debug is enabled for that subsystem or globally:

```text
[DEBUG] internets.stocks: finnhub failed for AAPL: 401 Client Error: ...
```

`sender.redact_secrets()` does not help here. It masks the argument after a
credential verb (`IDENTIFY`, `OPER`, `PASS`, ...) on log lines only, and it is
never applied to `PRIVMSG` bodies.

Retroactive search of your own copy of the log:

```bash
grep -nE 'token=|apikey=' internets.log internets.log.* | head
```

### Immediate containment

Order matters. Stop the emission before you rotate, or a rotation performed
while `.stock` is still reachable can publish the replacement.

1. Unload the module. This is instant, needs no restart, and is audited:

   ```text
   .unload stocks
   ```

   Confirm in the log: `event=module_unloaded name=stocks commands=3`.

2. If you cannot reach the bot as an admin, remove `stocks` from `[bot] autoload`
   in `config.ini` and restart. A `.rehash` will not do it: `AUTO_LOAD` is an
   import-time constant in `config.py`.

3. Treat the key as public from the moment it hit the channel. It was delivered
   to every user in the channel, to their clients' scrollback, to any channel
   logger or bouncer present, and to the IRC server's own logging if it keeps
   any. **None of that is recallable.** Do not spend time trying.

### Evidence preservation

Take the [Before you touch anything](#ir-first) copy first. Then, specifically:

- Note the channel, the timestamp, and the nick who ran the command. The bot
  does not audit-log module command usage, so the only in-bot record is the
  `internets.stocks` DEBUG line, and only if debug was on.
- `.stock` and `.crypto` are not privileged, so there is **no audit record at
  all** for the invocation.
- Preserve the rotated bot logs too (`internets.log.1` through `.3`, per
  `[logging] backup_count`, default 3). At 5 MiB each on a busy bot these roll
  fast.

### Eradication: rotate the keys

Three keys are in scope. Which ones leaked depends on which providers were
configured and which failed; assume all configured ones leaked unless you can
read the exact reply text.

| Secret name | Query parameter | Free-tier rate (source comment) |
|---|---|---|
| `finnhub_key` | `token=` | 60 calls/min |
| `alphavantage_key` | `apikey=` | 25 calls/day |
| `twelvedata_key` | `apikey=` | 800 calls/day |

The signup and account URLs recorded in `modules/stocks.py`:

- Finnhub: <https://finnhub.io/register>
- Alpha Vantage: <https://www.alphavantage.co/support/#api-key>
- Twelve Data: <https://twelvedata.com/account>

The provider consoles differ in shape and change over time; confirm the current
flow at the console rather than following remembered steps. What matters
operationally, and what you must establish for each provider before you call the
rotation done, is:

- Whether issuing a new key **revokes** the old one or leaves both live. If both
  stay live, you must explicitly delete or disable the old key. A rotation that
  only adds a key has not contained anything.
- Whether the provider offers usage logs for the compromised key, and over what
  window. That is your only evidence of whether it was abused.
- The rate ceiling, above, is the abuse budget an attacker inherits. Alpha
  Vantage's 25 calls/day is a small prize; Finnhub's 60 calls/min is not.

### Recovery: install the new key and confirm the bot uses it

The bot resolves each secret through `secret_store.get()`, which is a two-tier
lookup: `INTERNETS_<NAME>` in the process environment first, then
`config.ini [secrets]`. **The tier decides what you have to do**, and getting it
wrong is the classic silent failure here.

Establish which tier is live first:

```bash
python -m secret_store list | grep -E 'finnhub|alphavantage|twelvedata'
```

Output is name plus backend label, never the value:

```text
finnhub_key                file
alphavantage_key           (unset)
twelvedata_key             env
```

:::{warning}
`secret_store list` reads **your shell's** environment, not the bot's. If the
service unit exports `INTERNETS_FINNHUB_KEY` and your interactive shell does
not, `list` reports `file` while the running bot is using `env`. The
authoritative check is the running process:

```bash
tr '\0' '\n' < /proc/$(cut -d'|' -f1 internets.pid)/environ | grep '^INTERNETS_'
```

That output contains the secret values. Verified against
`secret_store.py - list_stored()`, which calls `os.environ.get()` in the CLI
process.
:::

**File tier** (`config.ini [secrets]`):

```bash
python -m secret_store set finnhub_key      # prompts; keeps it out of history
```

Never pass `--value` on an interactive shell: it lands in shell history and in
the process table. `set_value()` writes the `[secrets]` section in place,
preserving comments, through an atomic 0600 write.

Then reload the module so it re-reads the key. `StocksModule.on_load()` caches
the keys into `self._keys`, so nothing short of a module reload or a process
restart picks up a change:

```text
.reload stocks
```

Confirm, at INFO under `internets.stocks`:

```text
stocks: active providers: ['finnhub', 'twelvedata']
```

A key that failed the placeholder filter or was written to the wrong section
shows as `stocks: active providers: ['none']`.

**Environment tier** (`INTERNETS_FINNHUB_KEY`): a running process's environment
cannot be changed from outside it. `.reload stocks` re-reads `os.environ` of the
**same** process and will happily keep using the old value. Update the unit's
`Environment=` or `EnvironmentFile=`, then `systemctl daemon-reload` and restart
the process. There is no reload path.

Confirm the value changed without printing it:

```bash
python -m secret_store get finnhub_key
```

```text
(set, 40 chars, backend=file)
```

Finally, a functional check, in a channel you control rather than the one where
the leak happened:

```text
.stock AAPL
```

A successful reply ends with the provider tag, for example `[finnhub]`. A reply
beginning `all providers failed` means you are still emitting keys - go back to
containment.

### Follow-up

- Reload `stocks` only after the rotation is confirmed. Leaving it unloaded is a
  legitimate end state: the two commands it provides are `.stock`, `.s`, and
  `.crypto`.
- The same URL-bearing-exception habit exists in `log.warning` calls in `imdb`,
  `lastfm`, `youtube`, `steam`, and `twitch`. Those leak to the log rather than
  the channel, which is lower severity but the same defect, and they are worth a
  `grep -nE 'apikey=|api_key=|key=' internets.log` while you are here.
- The fix shape is recorded in [known-issues.md](known-issues.md) item 1:
  aggregate `type(e).__name__` and the provider name, never `str(e)`.
  `weather_providers/pirateweather/_codes.py - safe_get_json()` is the in-repo
  model.

### What this bot cannot tell you

- **Who saw it.** The bot has no record of channel membership at the moment of
  the reply beyond `users.json`, which is periodically pruned and only tracks
  users the bot has observed speaking or joining.
- **Whether the key was used by anyone else.** Only the provider's console can
  answer that, and only for as long as it retains usage data.
- **How many times it leaked.** There is no counter and no audit record. A
  provider outage lasting an hour publishes the key on every `.stock` call in
  that hour. Reconstruct from channel logs, not from the bot.
- **Whether the key is in a backup.** `config.ini` is inside the deployment
  directory, which is the backup unit. Every backup taken while the key was live
  contains it. See [disaster-recovery.md](disaster-recovery.md).

(ir-admin)=
## 2. Admin credential compromise

### What an attacker with the admin password can do

Authentication is a single shared password, hashed in `[admin] password_hash`,
verified by `hashpw.py - verify_password()`. There is one admin identity; there
are no per-user accounts and no per-command authorization. Everything in
`admin_cmds.py - _CORE` outside `_CORE_PUBLIC` is theirs.

| Capability | Commands | Consequence |
|---|---|---|
| Arbitrary IRC protocol | `.raw` | Anything the bot's IRC identity can do |
| Module control | `.load`, `.unload`, `.reload`, `.reloadall` | Disable privacy and logging modules |
| Process control | `.shutdown`, `.die`, `.restart`, `.rehash` | Availability; clears sessions |
| Data exfiltration | `.audit`, `.fingerprint`, `.stats`, `.shadow-list` | Hostmasks, seen data, note counts |
| Impersonation | `.say`, `.act`, `.nick`, `.mode`, `.snomask` | Speak as the bot anywhere |
| Evidence control | `.loglevel`, `.debug` | Turn the log down before acting |

`.raw` is the one that sets the ceiling. It sends any 510-byte line that
contains no CR, LF, or NUL directly to the IRC server as the bot. If `[irc]
oper_name` and `oper_password` are configured and the bot has opered up, `.raw`
inherits **IRC operator privilege**: KILL, network-wide bans, SAJOIN, and
whatever else your IRCd exposes to opers. Check that first:

```bash
grep -n 'oper_name' config.ini
```

What the admin password does **not** directly give: arbitrary code execution.
`IRCBot.load_module()` accepts only names matching `^[a-z][a-z0-9_]*$`, resolves
them under `MODULES_DIR`, and refuses a path that escapes it. An attacker who
also has filesystem write access to `modules/` does get code execution, via
`.load` or `.reload` - see [runbook 5](#ir-module).

### Detection signals

Successful authentication, at INFO on the root `internets` logger:

```text
Auth granted: mallory (mallory!~m@203.0.113.9)
```

Failures, at WARNING, with a running count:

```text
Failed auth: mallory (mallory!~m@203.0.113.9) 3/5
Auth lockout: mallory (mallory!~m@203.0.113.9) 5 failures
```

Session revocation, when a bound hostmask changes underneath a live session:

```text
Auth revoked for mallory: hostmask unverifiable or changed
  (stored=mallory!~m@203.0.113.9 current=mallory!~m@198.51.100.4)
```

Privileged actions in the log:

```text
Raw line sent by mallory: 'WHOIS alice'
Rehash by mallory
Restart by mallory
event=module_unloaded name=privacy commands=4
```

And in the audit chain, which is the durable record:

```text
.audit 50
.audit grep auth
.audit grep raw
```

A cleaner offline read, on the file rather than through IRC:

```bash
grep -c '"action":"auth_failed"' audit.log
python -c "
import json
for l in open('audit.log'):
    r = json.loads(l)
    if r['action'] in ('auth','auth_failed','auth_lockout','raw'):
        print(r['ts'], r['action'], r['actor'], r['host'], r.get('args'))
"
```

Brute-force shape: `_AUTH_MAX_FAILS = 5` within `_AUTH_LOCKOUT = 300` seconds,
keyed on the **lowercased nick**, not the hostmask. An attacker who changes nick
gets a fresh five-attempt budget each time, so a burst of `auth_failed` records
under many different actors is the signature to look for, not a rising counter
under one.

### Immediate containment

:::{warning}
**`.deauth` will not do what you want.** `AdminCommandsMixin.cmd_deauth()` takes
no target: it ignores its argument and ends **the calling nick's own** session.
There is no command that ends someone else's session. Verified against source.
:::

The three levers that actually work, in increasing order of disruption:

1. **Shadow-ban the attacker's nick.** `IRCBot._dispatch()` checks
   `is_shadow_banned()` as its very first action, before the PM check, before
   rate limiting, before auth. Every command from that nick is dropped silently,
   including `.auth`.

   ```text
   .shadow-ban mallory incident 2026-08-16
   ```

   This does **not** revoke an existing authenticated session for a different
   nick, and the attacker can defeat it by changing nick. It buys minutes, not
   containment.

2. **`.rehash` - clears every admin session.** This is the real lever.
   `cmd_rehash()` calls `self._authed.clear()` and replies
   `Cleared N admin session(s) - re-authenticate.` It also re-reads both config
   layers, so it is the same command that installs a rotated password hash.

3. **Restart or stop.** `.restart`, or from the shell:

   ```bash
   kill -INT "$(cut -d'|' -f1 internets.pid)"
   ```

   A restart clears sessions as a side effect of process replacement. If you
   believe the attacker also has host access, stop rather than restart, and go
   to [runbook 4](#ir-host).

Do not use `.rehash` as your first action if you have not yet captured evidence:
it clears the session table, which is the only place the attacker's bound
hostmask lives.

### Evidence preservation

The session table is in memory only. Before any rehash or restart, capture it:

```text
.stats
```

`.stats` reports the authenticated admin count but not the identities. The
identities are recoverable only from the log lines above (`Auth granted:` with
the bound hostmask). Grep them out before the log rotates:

```bash
grep -nE 'Auth granted|Auth revoked|Auth lockout|Failed auth' \
     internets.log internets.log.* > ~/ir-$ts/auth-timeline.txt
```

Then copy `audit.log`, `audit.log.key`, and every `audit.log.*` segment
together, per [Before you touch anything](#ir-first).

### Eradication: rotate the password

```bash
python hashpw.py --algo argon2
```

It prompts twice, hashes, prints timing, self-tests the result both ways, and
prints the config line:

```text
Add to config.ini under [admin]:

    password_hash = argon2$...

Self-test passed
```

Constraints the tool enforces, worth knowing before you pick a passphrase:
`MAX_PASSWORD_BYTES = 128` (UTF-8 bytes, not characters) for every algorithm,
and `BCRYPT_MAX_PASSWORD_BYTES = 72` additionally for bcrypt. `cmd_auth()`
enforces the same 128-byte ceiling at login, so a longer password would hash
cleanly and then never authenticate.

Install the new hash under `[admin] password_hash`, in `config.ini` or in the
`config.local.ini` overlay if that is where yours lives, then:

```text
.rehash
```

Expected reply:

```text
Config reloaded - argon2 hash active.
Cleared 2 admin session(s) - re-authenticate.
```

A restart is **not** required. `botlog.py - get_hash()` calls
`config.reload_config()` on every single `.auth` attempt, so the new hash is
live from the next authentication regardless. The `.rehash` is what kills the
existing sessions.

Verify the new password works and the old one does not, from a PM:

```text
.auth <new>          ->  mallory: authenticated.
.auth <old>          ->  mallory: wrong password.
```

If `.auth` answers `no password_hash configured - run hashpw.py`, the hash
landed in a file or section the bot is not reading - check that you edited the
`config.ini` in the bot's working directory, not another copy.

### Recovery

- Re-authenticate and confirm the module set is what you expect: `.modules`,
  compared against `[bot] autoload`. An attacker who unloaded `privacy` leaves
  no trace beyond one log line and one audit record.
- `.audit verify` - the chain should be intact. If it is not, go to
  [runbook 7](#ir-audit).
- `.health` and `.stats` for a general state read.
- Review `.shadow-list` for bans you did not place.
- Check `[irc] oper_name` is still what you set, and if the bot is opered,
  review the IRCd's own oper log for the incident window. That log is outside
  this bot entirely and is usually the better evidence.

### Follow-up

- Rotate the IRC-side credentials too if `.raw` was used at all. An attacker
  with `.raw` could have made the bot send `PRIVMSG NickServ :IDENTIFY ...` only
  if they already knew the password, but they could have registered nicks,
  changed channel modes, or issued oper commands. Go to
  [runbook 3](#ir-ircauth).
- The admin password is a single shared secret with no per-actor attribution.
  The audit log records the **nick** that acted, which is an IRC-layer identity
  an attacker controls. Treat actor attribution as a lead, not proof.

### What this bot cannot tell you

- **Which human was behind a nick.** The audit record's `actor` and `host` are
  the IRC nick and hostmask, both attacker-influenceable.
- **What a `.raw` line did.** The audit record stores the line after
  `sender.redact_secrets()` has masked everything following a credential verb,
  so `.raw OPER admin hunter2` is recorded as `OPER [REDACTED]`. The bot has no
  record of the server's response to any raw line.
- **What was read.** `.audit`, `.fingerprint`, `.stats`, and `.shadow-list` are
  not themselves audited. An attacker who authenticated and only read data
  leaves an `auth` record and nothing else.
- **Whether a rotated audit segment was altered.** `verify()` reads only the
  live `audit.log`. See [runbook 7](#ir-audit).
- **Anything about console access.** The stdin console
  (`console.py`) has **no authentication at all** and is not audited; its
  `shutdown`, `debug`, `loglevel`, and `status` commands leave only the ordinary
  log. The single signal is `event=console_active stdin=tty pid=...` at WARNING
  on startup. An unexplained one of those in a service log is itself an
  incident.

(ir-ircauth)=
## 3. IRC account or NickServ credential compromise

### Scope

Four separate reversible credentials reach the IRC network, all resolved through
`secret_store.get()` and all held as **import-time constants** in `config.py`:

| Secret | Read by | Sent as |
|---|---|---|
| `nickserv_password` | `config.py - NS_PW` | `PRIVMSG NickServ :IDENTIFY`, SASL PLAIN |
| `sasl_password` | `secret_store` only | falls back to `nickserv_password` |
| `server_password` | `config.py - SERVER_PW` | `PASS` during registration |
| `oper_password` | `config.py - OPER_PW` | `OPER <name> <pw>` |

Because they are import-time constants, **none of them can be changed without a
process restart**. `.rehash` explicitly does not re-read them; the SIGHUP path
logs `note=defensive_no_cred_reload` to say so.

### Detection signals

From the bot's side:

```text
event=sasl_failure nick=internets permanent=True line='...'
event=sasl_success nick=internets
event=plaintext_cred_refused cred=nickserv_password - refusing to send ...
event=tls_minimum_downgraded value=TLSv1.2
```

`_tls_or_refuse()` is a real control worth knowing: every credential send is
gated on the live connection being TLS. On a plaintext connection the bot logs
CRITICAL and suppresses the send rather than leaking the password.

A SASL failure that is not a config change you made is the strongest signal that
the account password was changed by someone else. `_RE_SASL_FAIL` matches
numerics 902, 904, and 905, and a permanent failure sets
`_sasl_failed_permanently`, which the reconnect loop surfaces rather than
retrying blindly.

The stronger evidence is **not in this bot**. Services (NickServ, and your
IRCd's oper log) hold the authoritative record of who identified, from where,
and when. Check `/msg NickServ INFO <account>` for a last-seen and last-address,
and your network's services log for `IDENTIFY` and `OPER` events.

### Immediate containment

1. **Change the password at services, from a trusted client, not through the
   bot.** Do not use `.raw PRIVMSG NickServ :SET PASSWORD ...`: it puts the new
   password on the wire from a process you may be in the middle of
   investigating, and the audit record will hold `PRIVMSG [REDACTED]` which
   tells you nothing later.

2. If the bot is opered and you suspect the oper credential, ask your network to
   **deoper and suspend the oper block** first. That is a services/IRCd action,
   not a bot action.

3. Stop the bot if it is reconnecting into a broken auth state. A permanent SASL
   failure already halts the retry, but a NickServ-IDENTIFY deployment will
   reconnect and re-send a now-wrong password indefinitely.

### Evidence preservation

- Capture the connection timeline before the log rotates:

  ```bash
  grep -nE 'event=(connect_begin|sasl_|rejoin|reconnect|plaintext_cred)' \
       internets.log internets.log.* > ~/ir-$ts/conn-timeline.txt
  ```

- Ask the network for the services log covering the window. This bot does not
  retain server responses.

### Eradication

Rotate at services, then install the new value at whichever tier is live
(see [runbook 1](#ir-keyleak) for how to determine that):

```bash
python -m secret_store set nickserv_password        # prompts
```

Set `sasl_password` explicitly only if it differs from `nickserv_password`; it
falls back otherwise. Note that `sasl_password` is the one entry in
`KNOWN_SECRETS` with no `CONFIG_LOCATIONS` mapping, so `migrate` will not
relocate a plaintext copy of it from another section.

Then **restart**:

```bash
kill -INT "$(cut -d'|' -f1 internets.pid)"
# confirm internets.pid is gone, then start under your service manager
```

### Recovery

Confirm the new credential took effect:

```text
event=sasl_success nick=internets
event=rejoin nickserv=confirmed
```

A `event=rejoin nickserv=timeout wait=...` line means the bot proceeded without
a confirmed identification. Also confirm the connection is TLS: if you see
`event=plaintext_cred_refused`, the credential was never sent at all and the
`ssl` setting is wrong.

### Follow-up

- Confirm `[irc] ssl = true` and that `INTERNETS_ALLOW_TLS12` is **not** set.
  The default floor is TLS 1.3.
- Review channel access lists, autoop entries, and any services flags granted to
  the bot's account. A compromised account may have been granted access
  somewhere you do not routinely check.
- If the bot's nick was used to send messages, those are in channel logs, not in
  this bot. `.say` and `.act` are audited with target and text; anything sent
  via `.raw` is audited only in redacted form.

### What this bot cannot tell you

- **Whether the account was used elsewhere.** The bot only knows its own
  session. Services holds the record.
- **What the server replied to any credential.** Numerics are matched, not
  stored; only `sasl_failure` and `sasl_success` events survive.
- **Whether the credential leaked from this host or somewhere else.** The
  credential exists in `config.ini`, in every backup of the deployment
  directory, and possibly in the service unit's environment. See
  [runbook 4](#ir-host).

(ir-host)=
## 4. Host compromise

If an attacker had code execution or file read as the bot user, **everything in
the deployment directory is exposed**. Treat this as the superset incident: it
implies runbooks 1 through 3 and 5 through 7 simultaneously.

### What is exposed, concretely

| Artifact | Mode | What it gives up |
|---|---|---|
| `config.ini` | 0600 | Every secret in `[secrets]`, plus `password_hash` |
| `config.local.ini` | 0600 by convention | Usually `password_hash` |
| `audit.log.key` | 0600 | Ability to forge the audit chain undetectably |
| `audit.log`, `audit.log.*` | 0600 | Admin action history, hostmasks (PII) |
| `users.json` | 0600 | Nick, hostmask, first/last seen, opt-out flags |
| `users.json.bak` | umask default | The same PII, typically world-readable |
| `locations.json` | 0600 | Per-nick saved locations |
| `internets.log`, `.1`-`.3` | umask default | Nick-to-location pairs, announced URLs |
| Module stores | varies | `seen.json`, `tells.json`, `notes.json`, `steamids.json`, `reminders.json` |
| Process environment | n/a | Any `INTERNETS_*` secret set by the unit |

Two of those rows are known defects rather than design:
`store.py - Store._write()` writes `<name>.bak` with `Path.write_bytes` and no
`chmod`, and `botlog.py` creates the log with the default umask and no check.
Both are [known-issues.md](known-issues.md) items 13 and 15. Verified live: a
`.bak` created under a 0022 umask lands at 0644 while the live file is 0600.

### Detection signals

There is no host intrusion detection in this bot. What it can contribute:

```bash
# Files newer than the last known-good deploy, inside the deployment dir
find . -newermt '2026-08-15 00:00' -type f -ls

# Modules that are not tracked by git
git status --porcelain modules/

# Whether the installed package tree still matches its wheel manifest
./scripts/verify_install.sh
```

Plus the log and audit signals from runbooks 2 and 5. Real detection comes from
the host: auditd, the package manager's own verification, SSH logs, and your
network's flow records. None of that is in scope here.

### Immediate containment

1. **Stop the bot and leave it stopped.** A restart on a compromised host
   re-reads a config the attacker may have edited.

   ```bash
   kill -INT "$(cut -d'|' -f1 internets.pid)"
   ```

2. **Isolate the host** at the network layer if you can. Do not reboot: a reboot
   destroys process memory, open file descriptors, and the network state that a
   responder would want, and it does not remove persistence.

3. **Assume every secret listed below is compromised.** Rotation is not
   optional and not deferrable, because the values are stored reversibly by
   design - the bot has to send them on the wire.

### Evidence preservation

Do this from a **different** host if the compromise is credible. Copy, do not
move; hash before and after.

```bash
tar --numeric-owner -cf - /srv/internets | ssh responder@evidence 'cat > bot.tar'
```

Preserve, in this priority order: `audit.log` plus `audit.log.key` plus every
rotated segment, the bot log and its rotations, `config.ini`, the JSON state
files with their `.bak` and `.corrupt.*` siblings, and the service unit.

### Eradication: the rotation matrix

`secret_store.py - KNOWN_SECRETS` registers **41** names.
`CONFIG_LOCATIONS` maps **40** of them to a config section. `nasa_api_key` is
read by `modules/apod.py` and `modules/astro2.py` but is in **neither**
registry, so it is invisible to `secret_store list` and `migrate`, which brings
the real total to **42**.

Enumerate what is actually set on this deployment before you start, so you
rotate what exists rather than the whole catalogue:

```bash
python -m secret_store list | grep -v '(unset)'
```

Add the unregistered one by hand:

```bash
grep -n 'nasa_api_key' config.ini
```

**Verify-only versus recoverable.** Exactly one credential in this system is
verify-only, and it is not in the secret store:

| Credential | Storage | Class |
|---|---|---|
| `[admin] password_hash` | scrypt / bcrypt / argon2 hash | Verify-only |
| All 42 secret-store names | Plaintext, 0600 or environment | Recoverable |
| `audit.log.key` | 32-byte hex, 0600 sidecar | Local, not issued |

Everything in the recoverable class must be **reissued at its provider**.
Changing the stored copy is not rotation; the attacker holds the value, and it
remains valid until the issuer invalidates it.

Rotation, grouped by where you go to do it:

| Group | Names | Where |
|---|---|---|
| Admin auth | `password_hash` | `python hashpw.py`, then `.rehash` |
| IRC | `nickserv_password`, `sasl_password`, `server_password`, `oper_password` | Services / IRCd operator |
| Weather (keyed) | `weatherapi_key`, `tomorrowio_key`, `openweathermap_key`, `visualcrossing_key`, `pirateweather_key`, `weatherstack_key`, `accuweather_key`, `worldweatheronline_key`, `weatherbit_key`, `stormglass_key` | Each provider console |
| Meteomatics | `meteomatics_username`, `meteomatics_password` | Meteomatics account |
| Apple WeatherKit | `weatherkit_team_id`, `weatherkit_service_id`, `weatherkit_key_id`, `weatherkit_key_file` | Apple Developer |
| Air / marine / fire | `airnow_key`, `purpleair_key`, `waqi_token`, `openaq_key`, `iqair_key`, `tidecheck_key`, `firms_key`, `google_pollen_key` | Each provider console |
| Satellites | `n2yo_api_key`, `nasa_api_key` | N2YO, NASA (unregistered) |
| Media / social | `omdb_key`, `lastfm_key`, `youtube_key`, `steam_key`, `twitch_client_id`, `twitch_client_secret` | Each provider console |
| Finance | `finnhub_key`, `alphavantage_key`, `twelvedata_key` | See [runbook 1](#ir-keyleak) |
| Search / reputation | `brave_key`, `abuseipdb_key` | Brave, AbuseIPDB |
| Contact identifier | `weather_user_agent` | Not a credential; PII, change if it is your address |

Two entries need their own handling:

- **`weatherkit_key_file`** stores a *path*, not a value. The secret is the
  contents of the `.p8` private key at that path. Revoke the key at Apple,
  issue a new one, replace the file, and confirm the old file is destroyed.
  Rotating the config value alone does nothing.
- **`weather_user_agent`** is a contact identifier (typically an email) sent in
  every outbound HTTP `User-Agent`. It is PII, not a credential. It cannot be
  "rotated"; decide whether the exposure matters.

**`audit.log.key`.** An attacker holding this key can rewrite the entire chain
and `verify()` will pass. That is not repairable after the fact - you can only
draw a line. To start a fresh chain:

1. Move the old `audit.log` and `audit.log.key` into evidence, together.
2. Start the bot. `AuditLog._load_key()` generates a fresh 32-byte key at 0600
   on first `record()`, and the chain restarts from genesis.

Do not delete the old key. Without it the old log cannot be verified even as a
historical artifact.

### Recovery

Rebuild rather than clean. A compromised host is not made trustworthy by
removing what you found.

1. Fresh host, fresh OS, fresh dedicated unprivileged user.
2. Fresh checkout at a known-good tag, dependencies installed from
   `requirements.txt` (see the lockfile defect in
   [deployment.md](deployment.md#deploy-defect-lockfile)).
3. Fresh `config.ini` from `config.ini.example` via
   `python -m secret_store init`, then set every rotated secret with
   `python -m secret_store set`. Do **not** restore the old `config.ini`.
4. Restore only **data** from backup: `locations.json`, `channels.json`,
   `users.json`, `shadow_bans.json`, and the module stores. Full procedure in
   [disaster-recovery.md](disaster-recovery.md).
5. Do not restore `internets.pid`, `audit.log`, or `audit.log.key`.
6. Deployment directory 0700; `config.ini` exactly 0600; `UMask=0077` in the
   unit.

### Follow-up

- Notify users whose PII was in `users.json`, `internets.log`, and the module
  stores, per whatever obligation applies to you. Note that `.forgetme` cannot
  reach the bot log ([known-issues.md](known-issues.md) item 4), so the log's
  contents are part of the exposure and were never erasable.
- Rotate anything the bot host shared with other systems: SSH keys, deploy keys,
  the backup destination's credentials.
- Add `privacy` and `health` to `autoload` if they were not there; the shipped
  template omits `privacy` while enabling six data-collecting modules.

### What this bot cannot tell you

- **Anything about the host.** There is no file integrity monitoring, no
  process monitoring, and no outbound connection logging beyond what the
  provider modules log at DEBUG.
- **Whether files were read.** All the read paths are ordinary file reads by the
  bot user. No access is recorded.
- **Whether the audit log is honest.** With `audit.log.key` in hand, an attacker
  can rewrite it cleanly. Even without the key, the downgrade path in
  [runbook 7](#ir-audit) makes forgery possible.
- **When the compromise started.** The bot log's earliest evidence is bounded by
  `[logging] backup_count`, default 3 files of 5 MiB each.

(ir-module)=
## 5. A malicious or trojaned module file

### How module loading works

`IRCBot.load_module()` is **arbitrary code execution by design**. It builds a
module from a file path with `spec_from_file_location` +
`module_from_spec` + `exec_module`, and the file's top level runs at that
moment. There is no signature, no checksum, and no allowlist beyond three
structural checks:

- The name must match `^[a-z][a-z0-9_]*$`.
- `MODULES_DIR / f"{name}.py"` must exist.
- `path.resolve()` must be under `MODULES_DIR.resolve()`, which blocks
  traversal and symlinks that escape.

So the trust boundary is the **filesystem**: anyone who can write a `.py` file
into `modules/` can run code as the bot user, by asking an admin to `.load` it,
by waiting for a restart if it is in `autoload`, or by editing an existing
module and triggering `.reload`.

No `sys.modules` entry is created for a loaded command module, so a `.reload`
genuinely re-reads the file. Anything the module *imports* is cached normally,
which matters for the next section: a trojan planted in `modules/base.py`,
`modules/geocode.py`, or under `weather_providers/` survives `.reloadall` and
requires a restart to change - and equally, requires a restart to remove.

### Detection signals

Every load and unload is logged under `internets.modules` at INFO:

```text
event=module_loaded name=stocks commands=3 cmds=crypto,s,stock
event=module_unloaded name=privacy commands=4
event=module_load_failed name=evil err=<exception>
```

Admin-triggered loads are also audited:

```bash
python -c "
import json
for l in open('audit.log'):
    r = json.loads(l)
    if r['action'] in ('load','unload','reload','reloadall'):
        print(r['ts'], r['action'], r['actor'], r['host'], r.get('args'))
"
```

:::{warning}
`.reloadall` records `args: null`. The audit record tells you a mass reload
happened and who did it, **not which modules were reloaded**. Autoloaded modules
at startup are not audited at all - they appear only as `event=module_loaded`
log lines.
:::

Filesystem checks, which are the ones that actually answer the question:

```bash
git status --porcelain modules/ weather_providers/
git diff --stat modules/ weather_providers/
find modules weather_providers -name '*.py' -newermt '2026-08-01' -ls
ls -la modules/__pycache__/ 2>/dev/null
```

A file present in `modules/` that git does not know about, or a tracked file
with a diff you did not make, is the finding. Compare `.modules` output against
`[bot] autoload` for a module that is loaded but not in the template.

Cross-check what is loaded right now against what is on disk:

```text
.modules
```

### Immediate containment

1. **Unload it** if the bot is otherwise trustworthy:

   ```text
   .unload <name>
   ```

   Note what this does *not* do: the code already ran at load time. Unloading
   deregisters commands and calls `on_unload()`; it does not undo anything the
   module did to the process, the filesystem, or the network. If `on_unload()`
   raises, the module stays fully loaded and the unload aborts.

2. **If the module ran at all, stop the process.** Unload is command-surface
   containment, not code containment. Anything the module started - a thread, a
   timer, a socket, a monkeypatch of another module - persists for the life of
   the process.

   ```bash
   kill -INT "$(cut -d'|' -f1 internets.pid)"
   ```

3. Remove it from `[bot] autoload` before the next start, or it loads again.
   `AUTO_LOAD` is import-time; a `.rehash` will not change it.

### Evidence preservation

Preserve the file itself before you delete it, and preserve the bytecode cache,
which can prove a version that no longer exists on disk:

```bash
cp -a modules/<name>.py modules/__pycache__/ ~/ir-$ts/
sha256sum modules/*.py weather_providers/**/*.py > ~/ir-$ts/module-hashes.txt
git log --oneline -20 -- modules/ > ~/ir-$ts/module-git-log.txt
```

Then the load timeline:

```bash
grep -n 'event=module_' internets.log internets.log.* > ~/ir-$ts/module-events.txt
```

### Eradication

1. Restore the module tree from a trusted source, not by editing in place:

   ```bash
   git status --porcelain modules/ weather_providers/
   git checkout -- modules/ weather_providers/
   git clean -n modules/ weather_providers/     # review first
   ```

2. Delete `__pycache__` directories so nothing stale is importable.
3. Treat this as a host compromise unless you can positively account for how the
   file got there. Go to [runbook 4](#ir-host).

### Recovery

Start with a reduced `autoload`, confirm each module you re-enable, and check
`.modules` against your intended list. Watch for `event=module_load_failed`,
which after a partial cleanup usually means a module lost an import.

A failed `.reload` leaves the module **unloaded**: `reload_module()` unloads
then loads, so a syntax error deregisters its commands with nothing to restore
them ([known-issues.md](known-issues.md) item 13).

### Poisoned dependency

Same class of problem, different tree. Third-party packages execute at import,
and `requirements.txt` / `requirements.lock` are the control surface.

What the repository provides:

| Control | Where | What it does |
|---|---|---|
| Hash pinning | `requirements.lock` | `pip install --require-hashes` refuses a substituted artifact |
| Lock regeneration | `scripts/regen-lockfile.sh` | Resolves on Python 3.10, the lowest supported |
| CVE scan | `.github/workflows/security.yml` | `pip-audit -r requirements.lock --strict` |
| SAST | same workflow | bandit, failing on HIGH severity |
| Secret scan | same workflow | gitleaks |
| SBOM | `scripts/sbom.sh` | CycloneDX from the **installed** environment |
| Install verification | `scripts/verify_install.sh` | Hash-checks installed files against the wheel `RECORD` |

What to check, in order:

```bash
# 1. What is actually installed, versus what the lock says
pip freeze > ~/ir-$ts/pip-freeze.txt
git diff -- requirements.txt requirements.lock

# 2. Known CVEs against the lock
pip-audit -r requirements.lock --strict --progress-spinner off

# 3. An SBOM of the live environment, not of the declared deps
OUT=~/ir-$ts/sbom.cdx.json ./scripts/sbom.sh

# 4. Does the installed tree still match its manifest
./scripts/verify_install.sh
```

In the lockfile diff, the things that matter are: a version that moved without a
corresponding `requirements.txt` change, a **new transitive** package nobody
added, a changed hash for an unchanged version (which should be impossible on
PyPI and is a strong signal), and a package pulled from a non-PyPI index.

:::{warning}
**Known defect.** `requirements.lock` was generated on Python 3.14 rather than
the 3.10 the script mandates, so it omits marker-gated transitives such as
`typing_extensions>=4.4`, and a `--require-hashes` install fails on Python 3.10
through 3.12. The practical consequence during an incident is that the hash
pinning you would want to lean on **does not currently install cleanly** on
those versions. Regenerate the lock per the script before relying on it. See
[known-issues.md](known-issues.md) item 6.
:::

Eradication for a poisoned dependency is a rebuilt virtualenv from a regenerated
lock, on a host you trust, plus the [runbook 4](#ir-host) treatment - the package
ran as the bot user with the bot user's file access.

### What this bot cannot tell you

- **What a module did.** There is no sandbox, no syscall record, no per-module
  network log. `event=module_loaded` proves it ran; nothing describes what it
  did.
- **Which modules `.reloadall` touched.** The audit record carries no argument.
- **Whether an autoloaded module was the malicious one.** Startup autoload is
  logged but not audited.
- **When a file was modified.** `mtime` is attacker-writable. Use git and your
  backups, not timestamps.
- **Whether a dependency was tampered with at install time.** `pip-audit`
  answers "known CVE", not "modified artifact". `verify_install.sh` answers the
  integrity question only for a wheel install of this project itself, not for
  its dependencies.

(ir-audit)=
## 6. Audit log tampering suspected

### How to verify

Over IRC:

```text
.audit verify
```

Two possible replies:

```text
audit chain intact (1284 records).
audit chain BROKEN at record index 417.
```

The index is zero-based and identifies the **first** record that failed, which
is where the chain diverges, not necessarily where the attacker started.

Offline, on a copy, which is what you should actually do:

```bash
python -c "
from audit_log import AuditLog
a = AuditLog('audit.log')
print(a.verify(), a.count())
"
```

`.health` reports the same verification as a single line.

### What verification actually checks

Each record carries `prev_hash` and `this_hash`. `verify()` walks from a genesis
of 64 zeros, recomputes each digest over a NUL-separated canonical form of
`(prev_hash, ts, actor, host, action, args)`, and compares. Records declaring
`v: 2` are verified with HMAC-SHA-256 under `audit.log.key`; records with no
`v` field fall back to unkeyed SHA-256.

Verified live, in a scratch directory:

- Editing one field of one record: `verify()` returns `(False, 1)`. Detected.
- **Rewriting that same record as an unversioned legacy record with a
  recomputed plain SHA-256 digest: `verify()` returns `(True, -1)`.** The forged
  `"action": "tampered"` sits in the chain and verification reports it intact.

### What verification does not cover

:::{warning}
**Known defect (verify downgrade).** Because each record chooses the algorithm
that validates it, anyone who can write `audit.log` can rewrite the chain from
any position as unversioned records, with **no key required** - the algorithm is
in `audit_log.py`. Reproduced above. Treat an intact verdict as evidence that
accidental corruption is absent, not as proof against a deliberate local writer.
[known-issues.md](known-issues.md) item 5.
:::

Three further limits, all real:

- **Rotated segments are never verified.** `verify()`, `count()`, `.audit`, and
  `.health` open only the live `audit.log`. `audit.log.<UTC stamp>` files are
  invisible to all of them. Deleting an entire rotated segment is undetectable
  by the bot's own tooling.
- **Tail truncation is undetectable from the file alone.** Removing the last N
  records leaves a chain that verifies perfectly. The module docstring says so.
- **No `fsync` anywhere in the codebase.** Verified: the only occurrence of the
  string is a stale comment in `admin_cmds.py` claiming `record()` fsyncs. A
  power loss can drop the most recent records, including the shutdown record
  written moments before exit. A gap at the tail after an unclean stop is
  expected, not evidence of tampering.
- **Rotation is not atomic against itself.** The stamp has one-second
  granularity and `Path.rename()` overwrites silently, so two rotations inside
  one second destroy the earlier segment.

### Immediate containment

If you suspect the log is being written by someone other than the bot:

1. Stop the bot. Every `record()` appends, and a running bot keeps extending a
   chain you are trying to freeze.
2. Copy `audit.log`, `audit.log.key`, and every `audit.log.*` segment to
   evidence, **together**. The log is worthless without the key and the key is
   worthless without the log.
3. Check who can write them:

   ```bash
   ls -la audit.log* && ls -ld .
   ```

   Both should be 0600 owned by the bot user, in a 0700 directory. Anything
   looser is the finding.

### Evidence preservation

```bash
sha256sum audit.log audit.log.key audit.log.* > ~/ir-$ts/audit-hashes.txt
cp -a audit.log audit.log.key audit.log.* ~/ir-$ts/
```

Verify each rotated segment by hand, since the bot will not:

```bash
python -c "
import sys
from pathlib import Path
from audit_log import AuditLog
for p in sorted(Path('.').glob('audit.log.2*')):
    a = AuditLog(p)
    print(p.name, a.verify(), a.count())
"
```

Note the key sidecar for a rotated segment is still `audit.log.key`: rotation
renames the log and starts a fresh chain but does not rotate the key, so one key
covers every segment written since the key was created.

### Independent evidence that does exist

The audit log is not your only record. When it is in doubt, corroborate against
sources the bot does not control:

| Source | Covers | Why it is independent |
|---|---|---|
| `internets.log` and rotations | `Auth granted`, `Raw line sent by`, `Rehash by`, `event=module_*` | Separate file, separate writer |
| IRCd / services logs | Connections, IDENTIFY, OPER, KILL | Different host entirely |
| Channel logs (yours or users') | Everything the bot said | Third parties hold copies |
| Backups of the deployment directory | Prior states of every file | Off-host, if you followed [disaster-recovery.md](disaster-recovery.md) |
| Host auditd / filesystem timestamps | Who wrote the file | Outside the bot's control |

An attacker who edits `audit.log` but forgets `internets.log` is the common
case, because the two are written by different subsystems and only one of them
is advertised as tamper-evident.

### Eradication and recovery

There is no repair. A chain that verified false is a chain whose contents you
cannot trust from the divergence point onward, and a chain that verified true
may still have been rewritten by the downgrade path.

The honest recovery is to draw a line:

1. Move `audit.log` and `audit.log.key` to evidence together.
2. Start the bot; a fresh key and a fresh chain are created on the first
   privileged action.
3. Record in your own incident notes the timestamp of the cut, so a future
   reader knows the chain before it is a separate artifact.

### Follow-up

- If tamper evidence matters to you, ship `audit.log` off-host as it is written.
  An external append-only sink is the only thing that closes the tail-truncation
  and downgrade gaps, and it is explicitly out of scope for this bot.
- Rotated segments accumulate at 5 MiB each. Archive them off-host with the key,
  per the quarterly item in
  [operations.md](operations.md#routine-maintenance-checklist).

### What this bot cannot tell you

- **Whether records were deleted from the tail.** By construction.
- **Whether a rotated segment was altered or deleted.** Nothing in the bot reads
  them.
- **Whether the chain was downgraded.** `verify()` reports intact either way.
  You would have to check every record for a missing `v` field yourself:

  ```bash
  python -c "
  import json
  for i, l in enumerate(open('audit.log')):
      if json.loads(l).get('v') != 2:
          print('legacy record at index', i)
  "
  ```

  On a deployment that has only ever run 3.0.0 or later, **any** hit is a
  finding.
- **What an admin read.** Read-only commands are not audited.

(ir-metrics)=
## 7. The metrics endpoint is exposed beyond loopback

### What the endpoint is

`metrics.py` runs a `http.server.HTTPServer` on a background daemon thread,
serving Prometheus text exposition on `GET /metrics` and 404 for every other
path. It is **unauthenticated**, has **no TLS**, and is **single-threaded with
no socket timeout**.

The only bind guard is a refusal of *unspecified* addresses. `expose()` parses
the host with `ipaddress`, unwraps IPv4-mapped IPv6, and raises only when the
host is empty or `is_unspecified` - so `0.0.0.0`, `::`, `::0`, and
`::ffff:0.0.0.0` are refused, while **a specific routable address, or a hostname
resolving to one, binds successfully** despite the error message saying the
endpoint must remain loopback-only.

### Detection signals

```bash
ss -ltnp | grep -E ':9779|python'
```

A listener on anything other than `127.0.0.1` or `::1` is the finding. Confirm
what the bot thinks it did:

```bash
grep -n 'event=metrics_enabled' internets.log
```

```text
event=metrics_enabled host=10.0.0.5 port=9779
```

That line records the host it actually bound. `event=metrics_start_failed
err=...` means it did not start. And from off-host, the definitive test:

```bash
curl -sS -m 5 http://<bot-host>:9779/metrics | head
```

A `200` with `# HELP internets_commands_total ...` means it is reachable.

### Immediate containment

1. Firewall the port at the host, which is the fastest and least disruptive
   action.
2. Fix the config:

   ```ini
   [metrics]
   enable = true
   host = 127.0.0.1
   port = 9779
   ```

   Or `enable = false` to turn it off entirely.
3. Restart. `.rehash` will not move the listener: `expose()` is called once from
   `_main()` at startup, and the running server is not reconfigured.

### Evidence preservation

Web-server access is logged at DEBUG only (`log_message` is overridden to route
into `log.debug("metrics http: " + format, ...)`), so unless debug was enabled
for that subsystem there is **no access record at all**. What you can capture:

```bash
ss -tnp | grep 9779 > ~/ir-$ts/metrics-conns.txt
grep -n 'metrics http' internets.log internets.log.* > ~/ir-$ts/metrics-access.txt
```

Your firewall or reverse-proxy logs are the real evidence.

### What an unauthenticated scraper learned

Not much, and it is worth being precise rather than alarmed. The registry holds
ten metrics, of which four have live update call sites:

| Metric | Labels | Discloses |
|---|---|---|
| `internets_commands_total` | `module`, `command` | Which commands exist and their usage volume |
| `internets_reconnects_total` | none | Connection stability |
| `internets_dropped_messages_total` | none | Send-queue saturation |
| `internets_audit_records_total` | none | Privileged action volume |

**No nick, hostmask, channel, or user data appears in any label.** Verified: the
only labelled call site is `commands_total.inc(labels={"module": ..., "command":
...})`. So the disclosure is a command inventory, a usage profile, and a
liveness oracle - reconnaissance, not a data breach.

The other six metrics have no update call site anywhere and render as a constant
zero ([known-issues.md](known-issues.md) item 13): `provider_calls_total`,
`provider_quota_used`, `module_loaded`, `provider_active`, `sender_queue_depth`,
`authed_admins_count`. Do not read meaning into them, and do not build alerts on
them.

The more serious exposure is availability, not confidentiality. `HTTPServer` is
single-threaded with no socket timeout, so **one client holding a connection
open blocks every subsequent scrape**, and the port is an unauthenticated
network surface on a host whose whole design otherwise has none.

### Recovery and follow-up

- Bind loopback, and front it with an authenticating reverse proxy if you need
  off-host scraping. The bot has no authentication to add.
- Counters do not persist across a restart, so a restart resets your totals.
  Rate queries are unaffected.
- Add the bind address to your deployment checklist
  ([deployment.md](deployment.md#deployment-checklist)).

### What this bot cannot tell you

- **Who scraped it.** No access log above DEBUG, and no record of source
  addresses.
- **For how long it was exposed.** Only `event=metrics_enabled` at each start,
  bounded by log retention.
- **Whether the scrapes were yours.** There is no client identity of any kind.

## Cross-references

- [security-model.md](security-model.md) - the control inventory these runbooks
  respond to, and section 12's verified weaknesses.
- [known-issues.md](known-issues.md) - every defect cited above, with its
  verification and fix shape.
- [operations.md](operations.md) - routine procedures, including the maintenance
  checklist these runbooks assume you are running.
- [disaster-recovery.md](disaster-recovery.md) - restoring after any of the
  above.
- [administration.md](administration.md) - the admin command surface in full.
- [SECURITY.md](security-policy.md) - reporting a vulnerability in the software.
