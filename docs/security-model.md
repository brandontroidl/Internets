# Security model

Internets 5.0.0. This page describes the security-relevant mechanisms of the bot,
the boundaries they defend, and where they stop working. It is written to be read
by someone conducting a security review, so it states limits as plainly as it
states controls. Line-level mechanism lives in [the implementation reference](internals/index.md);
this page explains what is defended, by what, and against whom.

Nothing here claims the system is secure. Each control is described together with
the conditions under which it fails.

## 1. Threat assumptions

The bot is a single-process, single-host Python daemon that connects outbound to
one IRC server and makes outbound HTTP requests to third-party APIs. It listens on
no network port by default. The Prometheus exporter is the only listener and is
off unless enabled.

Assumed capable of hostile behavior:

| Actor | Capability assumed | Primary controls |
|---|---|---|
| Any IRC user | Send arbitrary text to a channel the bot is in, or to the bot in PM; change nick; supply arbitrary command arguments | Dispatch caps, rate limiting, `strip_ctrl`, SSRF guards |
| A malicious or compromised upstream API | Return arbitrary bytes, oversized bodies, hostile redirects | Response byte caps, per-hop SSRF revalidation, `strip_ctrl` |
| A hostile DNS answer | Resolve a public name to an internal address | Thread-local DNS pinning in `modules/_netsafe.py` |
| The IRC server operator | See everything the bot sends, including credentials | TLS-only credential sends, TLS 1.3 floor |

Assumed trusted, and therefore outside what any control here defends against:

- The local host's kernel, filesystem, and any account able to read the bot's
  working directory. File modes are the only protection, and root defeats them.
- The Python interpreter and the pinned dependency set at install time.
- The operator, who holds the admin password hash and every configured secret.
- Anyone with write access to `config.ini`, `audit.log`, or `audit.log.key`. Such
  an actor can rewrite the audit chain undetected (see [section 9](#9-audit-integrity)).

Explicitly out of scope, with no mitigation in the codebase:

- Cross-process concurrency on any state file. `audit_log.AuditLog` uses a
  `threading.Lock` and no `flock`; two bot processes sharing a directory will
  interleave writes. `process_lock.ProcessLock` is the intended prevention, and
  its stale-PID reclaim is itself not atomic (see [process_lock internals](internals/process_lock.md)).
- Tail truncation of the audit log by an actor holding both the log and its key.
  Detecting that requires an external append-only sink, which does not exist here.
- Traffic analysis, IRC server compromise, and anything requiring a second host.

## 2. Trust boundaries

```{graphviz}
digraph boundaries {
  rankdir=LR;
  node [shape=box, fontname="Helvetica", fontsize=10];
  edge [fontname="Helvetica", fontsize=9];

  subgraph cluster_host {
    label="local host (trusted)";
    style=dashed;
    proc  [label="bot process\n(non-root enforced)"];
    cfgf  [label="config.ini 0600\n[secrets]"];
    state [label="state JSON\nlocations/channels/users"];
    audit [label="audit.log 0600\n+ audit.log.key 0600"];
    blog  [label="internets.log\numask default"];
  }

  ircd  [label="IRC server\nTLS 1.3", shape=ellipse];
  users [label="IRC users\nuntrusted input", shape=ellipse];
  apis  [label="third-party HTTP APIs\nuntrusted responses", shape=ellipse];
  prom  [label="Prometheus scraper\nloopback only", shape=ellipse, style=dashed];

  users -> ircd [label="PRIVMSG"];
  ircd  -> proc [label="inbound lines\n8192-byte read cap"];
  proc  -> ircd [label="outbound\n512-byte frames"];
  proc  -> apis [label="SSRF-guarded\nsize-capped"];
  apis  -> proc [label="strip_ctrl on\neverything echoed"];
  proc  -> cfgf;
  proc  -> state;
  proc  -> audit;
  proc  -> blog;
  prom  -> proc [style=dashed, label="/metrics\nunauthenticated"];
}
```

| Boundary | Direction | What crosses | Enforcement point |
|---|---|---|---|
| IRC server | both | Every command, every reply, all credentials | TLS context in `internets.py - IRCBot._connect()`; credential gate `_tls_or_refuse()` |
| IRC users | inbound | Command words and arguments | `IRCBot._dispatch()` caps and gates |
| Admins | inbound | Privileged commands | `IRCBot.is_admin()` re-checked per call |
| Channel operators | inbound | Nothing. Channel op status grants no bot privilege | See [section 4](#4-authorization) |
| Filesystem | both | Config, secrets, state, audit, logs | POSIX modes; `secret_store.perms_ok()` is the only fail-closed check |
| External HTTP APIs | both | Credentials outbound, arbitrary bytes inbound | `modules/_netsafe.py`, `modules/base.py - fetch_json()` |
| Local operator shell | inbound | `python -m secret_store`, `hashpw.py`, the stdin console | No authentication; local shell access is full control |
| Prometheus | inbound | Counter values | `metrics.MetricRegistry.expose()` refuses unspecified-address binds |

The stdin console (`console.py`) is unauthenticated by design: it accepts
`debug`, `loglevel`, `status`, and `shutdown` from whoever controls the
terminal. `--no-console` disables it for daemonized runs. Anyone who can write
to the process's stdin can shut the bot down.

## 3. Authentication

There is exactly one authenticated principal: the bot admin. There is no user
account system, no per-user identity, and no delegation.

### The password

A single password hash is stored in `config.ini` (or the `config.local.ini`
overlay) at `[admin] password_hash`. It is generated out-of-band with
`hashpw.py` and never transits IRC in either direction except as the plaintext
the admin sends in `.auth`.

- Supported schemes: `scrypt$` (stdlib, always available), `bcrypt$`
  (requires `bcrypt`), `argon2$` (requires `argon2-cffi`).
- `botlog._validate_hash()` runs at import and calls `sys.exit(1)` if the stored
  value has any prefix other than those three. An empty hash is not fatal; it
  logs a warning and leaves admin auth disabled. A malformed one aborts startup
  rather than silently disabling every admin command.
- `hashpw.MAX_PASSWORD_BYTES` is 128, denominated in UTF-8 bytes, and is pinned
  to stay at or below `IRCBot._MAX_ARG_LEN` (400) so the dispatcher-side cap
  cannot make the auth-side check unreachable.
- bcrypt input is refused above `BCRYPT_MAX_PASSWORD_BYTES` (72) at hash time,
  because bcrypt below 5.0 silently truncates there. Truncation would mean any
  password sharing the stored one's first 72 bytes authenticates.

Cost parameters are environment-tunable within hard bounds:
`INTERNETS_ARGON2_MEM_MIB` (default 128, clamped to 19..4096),
`INTERNETS_ARGON2_TIME` (default 3, clamped 1..20), `INTERNETS_BCRYPT_ROUNDS`
(default 13, clamped 10..16). Out-of-range values are clamped with a warning,
not rejected. Detail in [hashpw internals](internals/hashpw.md).

### The `.auth` flow

`admin_cmds.AdminCommandsMixin.cmd_auth()`:

1. Refuses outside PM. `IRCBot._dispatch()` blocks `auth` and `deauth` in a
   channel before the handler runs, so the password cannot reach a channel via
   the normal path.
2. Rejects an argument over `MAX_PASSWORD_BYTES` before hashing.
3. Applies the lockout check (below).
4. Snapshots the caller's current hostmask, then verifies the password in a
   worker thread so a slow argon2 verify does not block the event loop.
5. On success, requires a currently-known hostmask that is not the `"unknown"`
   sentinel, and requires it to equal the pre-verify snapshot. A hostmask that
   changed during the verify window is refused.
6. Binds the session as `_authed[nick.lower()] = hostmask`.

The password is never logged, never audited, and never included in an exception
message: the generic exception arm logs `type(e).__name__` only, because hashing
backends have been observed to echo input fragments in error text.

### Brute-force lockout

`_AUTH_MAX_FAILS = 5` failures within `_AUTH_LOCKOUT = 300` seconds locks the
nick out. The window is sliding: an attempt made while locked out refreshes the
timer, so trickling one attempt per window does not bypass the limit. The
failure map is pruned once it exceeds `_AUTH_CLEANUP_THRESHOLD = 50` entries.

Failed attempts are audited (`auth_failed`), and the transition into lockout is
audited once (`auth_lockout`). Recording stops at lockout, deliberately: an
unbounded flood would otherwise churn the 5 MB audit log through rotation and
destroy older forensic history. The counter is the only thing recorded; not the
password, not its length.

The lockout is keyed on the lowercased nick. It is not keyed on hostmask, so an
attacker who can change nick freely gets a fresh five-attempt budget per nick.
Against a 128-byte-max password behind argon2 at 128 MiB this is not the
limiting factor, but it is the shape of the control.

### Session binding

`IRCBot.is_admin()` is called on every privileged command and re-reads the live
binding each time. It is fail-closed:

- No session for the nick: deny.
- Stored hostmask is the `"unknown"` sentinel: deny and revoke.
- Current hostmask unknown: deny, without revoking.
- Current hostmask differs from the bound one: deny and revoke.

Only an exact match of a known current hostmask against the bound one grants.
The hostmask cache is refreshed on PRIVMSG, JOIN, NICK, and account events, and
dropped on QUIT. `.rehash` and SIGHUP both clear every session.

A session is therefore a routing handle over a live hostmask check, not an
authorization token in its own right. There is no session expiry by time: a
session persists until deauth, a hostmask change, a rehash, or a restart.

## 4. Authorization

### Admin gating

Every privileged handler begins with `_require_admin()`, which calls
`is_admin()`. The core command table `AdminCommandsMixin._CORE` holds 28 entries
mapping to 27 handlers (`.die` aliases `.shutdown`); `_CORE_PUBLIC` marks
`help`, `modules`, `version`, and `auth` as usable without auth. Everything else
is admin-only, including `deauth` - 23 admin commands once aliases collapse.

Admin authority is total within the process. `.raw` injects an arbitrary IRC
protocol line, `.load` imports an arbitrary module from `modules_dir`,
`.restart` re-execs the process, and `.shutdown` stops it. There is no
privilege tiering below admin and no confirmation step on any of them. The
compensating control is the audit log, which is detective, not preventive.

Three module commands gate on `is_admin()` directly rather than through the
core table:

- `channels.cmd_users` - admin only, because the output is per-nick hostmask PII.
- `health.cmd_health` - admin only; its refusal message points non-admins at
  `.uptime`, which is itself admin-gated. `health` also registers a public
  `uptime`, but `_dispatch()` resolves `_CORE` first, so that registration is
  permanently unreachable while `health.help_lines()` still advertises it. It
  is the only shadowed command name in the whole command set.
- `weather.cmd_providers` - admin only; exposes which provider keys are live.

`channels.cmd_users` is admin-gated in code but advertised as public in its help
entry: `help_lines()` tags `join` and `part` with
`[channel founder / admin]` and leaves `users` unmarked. The gate holds; only
the advertisement is wrong.

### PM-only commands

Three separate mechanisms enforce PM-only, and they are not unified:

| Command | Enforced by | Behavior in channel |
|---|---|---|
| `.auth`, `.deauth` | `IRCBot._dispatch()` | Refused before the handler runs |
| `.pwn` | `secinfo.cmd_pwn` comparing `reply_to != nick` | Refused with a notice |
| `.forgetme`, `.privacy` | `privacy._require_pm()` | Refused |
| `.optout`, `.optin` | nothing | Accepted in channel |

The `privacy` module docstring claims the module is PM-only. `optout` and
`optin` do not call `_require_pm()`, so that claim is wrong. No privacy command
is rate-limited.

### What a channel operator can do

Nothing, by virtue of being a channel operator. `IRCBot.is_chanop()` exists and
tracks per-channel op state from NAMES and MODE, but no command authorizes on
it. Channel op status is used for display and bookkeeping only.

The one non-admin privileged path is `channels.cmd_join` / `cmd_part`, which
accepts a request from a **registered channel founder**. Verification is
asynchronous: the module sends `WHOIS <nick>` and `PRIVMSG <services> :INFO
<channel>`, then compares the WHOIS 330 account name against the founder name
parsed from the services reply. A match approves the join or part.

This inherits the network's services as an identity provider. It is only as
strong as the IRC network's NickServ, and it depends on parsing a human-readable
services NOTICE with a regular expression. Overlapping verifications inside the
15-second window can misattribute a founder (recorded in the ledger). A `.join`
by a founder is not audit-logged; only admin actions are.

## 5. Secrets

### Two-tier resolution

`secret_store.get(name)` resolves in this order, first non-placeholder hit wins:

1. Environment variable `INTERNETS_<NAME_UPPER>`.
2. `config.ini` `[secrets]` section, only if the file is exactly mode 0600.
3. The caller's default, normally the empty string.

Both tiers apply the same `_PLACEHOLDERS` filter, so `changeme`, `your-key-here`,
`todo`, `test`, and about thirty other template markers read as unset rather than
being sent upstream as a credential. `config.py._secret_or_cfg()` and
`modules/base.py - cred()` layer a legacy fallback under this: if the store has
nothing, they read the old plaintext `(section, key)` location in `config.ini`.

`KNOWN_SECRETS` holds 41 canonical names. Membership is what makes a name
visible to `secret_store list`, `status`, and `migrate`. See
[the configuration reference](configuration.md#secret-inventory) for the full
inventory and [secret_store internals](internals/secret_store.md) for the file
backend.

### Permission enforcement

`perms_ok()` is the one fail-closed filesystem check in the codebase. If
`config.ini` exists with any mode other than 0600:

- `get()` logs `REFUSING to read` and returns the default, so every secret reads
  as unset and every keyed module silently stops working.
- `set_value()` and `delete()` raise `PermissionError` rather than reporting
  "not found", so an operator removing a leaked credential cannot mistake a
  blocked delete for a completed one.

The check is exact equality against 0600, not a "no broader than" test. A file
at 0400 - stricter, not looser - fails the check and falls through to defaults.
On Windows the check returns OK unconditionally and relies on ACLs.

`_atomic_write_text()` creates the temp file with 0600 from `os.open`, writes,
`os.replace`s, then re-chmods. There is no window in which the file is
world-readable. `set` and `delete` are targeted line edits on the `[secrets]`
block rather than a `configparser` round-trip, because `configparser.write()`
would strip every comment from the file.

### What is never logged

- Secret values. The `get` CLI prints `(set, N chars, backend=file)`; `list` and
  `status` print backend labels only. No CLI flag prints a value.
- Exception text from configparser, argon2, or bcrypt. `_safe_exc()` reduces
  every logged exception in `secret_store` to its type name, because those
  libraries echo fragments of the offending value.
- Outbound credentials, in the log only. `sender.redact_secrets()` masks
  everything after `AUTHENTICATE`, `IDENTIFY`, `REGISTER`, `IDENT`, `OPER`,
  `PASS`, or `AUTH` on any line before `log.debug` sees it, and
  `internets._redact_inbound()` applies the same regex to the trailing portion
  of inbound PRIVMSG and NOTICE lines so a credential cannot be redacted in one
  direction and leak in the other.

Redaction scope is the important limit here: it operates on the **log path
only**. The bytes on the wire are unchanged, and no PRIVMSG the bot composes is
passed through it. See [section 12](#12-known-limitations) for the case where
that matters.

### Verify-only versus recoverable

The two credential classes are handled by different modules on purpose:

| Class | Example | Storage | Module |
|---|---|---|---|
| Verify-only | Admin password | One-way hash (scrypt/bcrypt/argon2) | `hashpw.py` |
| Recoverable | NickServ, SASL, oper, server passwords, every API key | Plaintext at rest, 0600 | `secret_store.py` |

A recoverable credential must be replayed on the wire, so hashing it would break
the authentication it exists to perform. That is why `secret_store` does not
hash. The protection is file mode plus the environment-variable tier, nothing
more. The module docstring's phrase "encryption-at-rest" is stale from the
removed keyring era; the implementation is plaintext plus 0600.

`weatherkit_key_file` stores a **path** to an Apple `.p8` private key, not the
key material. The file's own permissions are the operator's responsibility.

## 6. Network security

### TLS to the IRC server

`IRCBot._connect()` builds `ssl.create_default_context()` and sets
`minimum_version = TLSv1_3`. Setting `INTERNETS_ALLOW_TLS12=1` lowers the floor
to TLS 1.2 and logs a warning naming the downgrade. Certificate and hostname
verification are on unless `[irc] ssl_verify = false`, which logs a warning on
every reconnect so the state cannot silently persist unnoticed.

`_tls_or_refuse(cred_name)` gates every outbound credential on `_tls_active`. On
a plaintext connection it logs CRITICAL and the caller suppresses the send, so
`ssl = false` costs the bot its NickServ, SASL, server, and oper authentication
rather than leaking them. The flag reflects the configured `ssl` setting, not a
handshake result, so it is a configuration guard rather than a live channel-state
check.

### SSRF defenses

Two independent guards exist, and they do not agree.

`modules/base.py - resolve_public(host)` is used by the network probers
(`.headers`, `.ssl`, `.tcp`, `.down`). It resolves the host and raises
`ValueError` if **any** answer is private, loopback, link-local, multicast,
reserved, or unspecified. It returns the `getaddrinfo` list so callers can
connect to a validated address rather than re-resolving.

`modules/_netsafe.py` is the fuller guard, used wherever a user-influenceable
URL is fetched (`probe`, `scinews`):

- `ip_is_blocked()` rejects the same set plus IPv6 site-local (`fec0::/10`), and
  unwraps IPv4-mapped IPv6 addresses before testing.
- `METADATA_HOSTS` blocks `169.254.169.254`, `fd00:ec2::254`, and
  `metadata.google.internal` by name as well as by address.
- `resolve_safe_ip()` requires **every** answer to pass, then returns one, so a
  multi-answer rebinding attempt is rejected rather than partially accepted.
- `safe_open()` pins `socket.getaddrinfo` thread-locally to the validated IP for
  the duration of the request, closing the resolve-to-connect TOCTOU that a
  plain check-then-fetch leaves open. The real hostname is kept, so SNI, `Host`,
  and certificate verification work normally.
- Every redirect hop is re-parsed, re-validated, and re-pinned. The hop limit is
  5; `Location`-less redirects and non-`http(s)` schemes raise `SSRFBlocked`.

The divergence is real and is a known defect: `resolve_public()` does **not**
test `is_site_local`, so `fec0::/10` passes the prober guard and is blocked by
the netsafe guard. Verified by direct call. See
[section 12](#12-known-limitations).

The DNS pin is a module-level monkeypatch of `socket.getaddrinfo`, installed
once at import and a no-op unless the calling thread has set a pin. It affects
any synchronous resolution in a pinned thread, including code that did not ask
for it. `aiohttp` uses the loop resolver and is unaffected.

### Plaintext HTTP integrations

Not every outbound call is TLS. Verified cleartext endpoints:

| Module | Endpoint | Why |
|---|---|---|
| `reflookup` | arXiv over `http://` | The only cleartext URL in the module |
| `ipinfo` | `ip-api.com` over `http://` | Free tier does not offer TLS |
| `iss` | ISS position endpoints over `http://` | Upstream offers no TLS |
| `idlerpg` | `http://idlerpg.rizon.net/xml.php` by default | Operator-overridable via `[idlerpg] api_url` |

None of these carry a credential, but all of them are on-path forgeable: an
attacker between the bot and the upstream can dictate the reply text the bot
prints to the channel. `strip_ctrl` bounds what that text can do to an IRC
client; it does not make the content trustworthy.

## 7. Input validation

### Inbound IRC

| Limit | Value | Enforced at |
|---|---|---|
| Read buffer | 8192 bytes | `asyncio.open_connection(limit=_READ_LIMIT)` |
| Over-limit line | Counted and drained, connection kept | `IRCBot.run()` read loop |
| Command argument | 400 chars | `IRCBot._dispatch()` - `_MAX_ARG_LEN` |
| Concurrent command tasks | 50 | `_MAX_TASKS`, checked as an O(1) counter |
| Per-command wall time | 60 s | `asyncio.wait_for` in `_run_cmd()` |
| Outbound body chunk | 400 bytes, UTF-8 boundary aware | `IRCBot._split_msg()` |
| Outbound frame | 512 bytes including CRLF | `Sender._write_line()` |

An over-limit inbound line does not kill the connection: `readline()` raises,
the remainder is drained to the next newline, and the loop continues. A command
argument over 400 characters is refused with a notice rather than truncated.

Dispatch order matters for a review: shadow-ban check, PM-only check, per-nick
flood limit, per-channel burst limit, argument length, task capacity, then
handler lookup. A shadow-banned nick consumes none of the later budgets and
receives no reply at all, which is the point of the mechanism.

Rate limiting has three layers, all in `store.RateLimiter`: per-nick flood
(`flood_cooldown`, floored at 1 s), per-nick API cooldown (`api_cooldown`,
floored at 1 s), and a cross-user per-channel burst gate that catches
coordinated floods the per-nick limiters cannot see. Admins bypass the flood
limiter.

Individual modules gate on `rate_limited()` inconsistently. Several gate after
emitting their usage line, so empty-argument spam bypasses the limiter;
`bofh`, `dice`, and every `privacy` command skip the gate entirely. This is
recorded across several ledger entries and is a real availability weakness, not
a style nit.

### External data echoed to IRC

`modules/base.py - strip_ctrl(s, max_len=400)` is the single sanitizer for any
third-party string spliced into an IRC line. It removes the entire C0 range
(`\x00`-`\x1f`) plus DEL, which covers bold (`\x02`), color (`\x03`), reverse
(`\x16`), ESC (`\x1b`), and BEL (`\x07`), then truncates. The sender only strips
CR, LF, and NUL as a transport backstop, so `strip_ctrl` is the actual defense
against a hostile API title spoofing bot-attributed formatting or emitting ANSI
escapes into an operator's terminal.

Response bodies are byte-capped before decoding. `fetch_json` streams and reads
`max_bytes + 1`, raising `ResponseTooLarge` before any JSON parse, with a
256 KiB default and explicit larger caps where a payload legitimately needs one.
The weather provider tree carries a second, independent stream-and-cap
implementation with a different default (see
[weather provider internals](internals/weather-providers/http.md)).

## 8. Logging defenses

`botlog._SafeFormatter` strips C0 controls except TAB, DEL, and the C1 range
(`\x80`-`\x9f`) from `record.msg` and every element of `record.args` before
formatting. It works on a copy of the record so other handlers see the original,
and it deliberately leaves `record.exc_text` alone so tracebacks stay readable.
This is the log-injection control: an attacker cannot inject a newline through
`log.info("cmd: %s", attacker_input)` to forge a log line.

`redact_secrets` is applied to outbound lines before `log.debug` and, via
`_redact_inbound`, to the trailing text of inbound PRIVMSG and NOTICE. Matching
is word-boundaried so `password` and `compass` are not false positives, and a
bare verb with no argument is left alone. A standalone verb in relayed `.say`
text is over-redacted in the debug log, which is the safe direction.

The scope limit is worth stating twice because it has already caused a real
leak: **`redact_secrets` is log-only**. It is not applied to PRIVMSG
construction, so any module that interpolates an exception string into a channel
reply bypasses it entirely.

The main log (`internets.log`) is created by `logging.handlers.RotatingFileHandler`
with no mode enforcement, so it lands at the process umask, typically 0644,
while `config.ini` and `audit.log` are held at 0600. That log contains PII (see
[section 12](#12-known-limitations)).

## 9. Audit integrity

`audit_log.AuditLog` maintains a hash chain over privileged actions. Every
handler in `admin_cmds.py` calls `_audit()`; no other file writes audit records.

Each record is a JSON line carrying `v`, `ts`, `actor`, `host`, `action`,
`args`, `prev_hash`, and `this_hash`. `this_hash` is HMAC-SHA-256 over the
NUL-separated canonical form of `(prev_hash, ts, actor, host, action, args_str)`
under a 32-byte key held in a 0600 sidecar at `<path>.key` - for the default
`./audit.log` that is `audit.log.key`, not `audit.key` as the module docstring
says. NUL separation means a field containing the delimiter cannot collide with
a different field layout. `args` are canonicalized with `sort_keys=True` so dict
ordering cannot break verification.

HMAC rather than plain SHA-256 is the point of the design: an attacker holding
only a copy of `audit.log` - a backup, an accidental commit - cannot recompute
the chain, because the algorithm is public and in this file.

Key handling is fail-closed in one direction: an existing key file that is
present but unreadable raises `RuntimeError` rather than being regenerated,
because regenerating would silently void every prior record's HMAC. A key that
is present but malformed or short is moved to `<name>.bad` before a fresh key is
written, so the old chain stays recoverable.

`verify()` re-walks the file and returns `(True, -1)` or `(False, index)` at the
first broken record. `.audit verify` surfaces this to an admin.

### Verified weaknesses

These are properties of the current implementation, confirmed against source.

**Legacy downgrade.** `verify()` chooses the hash scheme from the record's own
`v` field. A record written without `v` is verified with keyless SHA-256, which
anyone can compute. An attacker with write access to `audit.log` can therefore
rewrite the chain from any position by emitting legacy-form records, and
`verify()` reports the chain intact. The self-declared version field is the
authorization for its own verification scheme. Severity depends on where the
HMAC key sits relative to the log; both are 0600 in the same directory by
default, so an actor who can write one can generally read the other anyway - but
the downgrade removes even the requirement to read the key.

**Rotated segments are never verified.** `_rotate_if_oversize()` renames the log
to `audit.log.<UTC timestamp>` once it exceeds 5 MB and starts a fresh chain
from genesis. Neither `verify()` nor `count()` nor the `.audit` command ever
opens a rotated segment. The rotation stamp has one-second granularity and the
rename overwrites silently, so two rotations inside the same second destroy a
segment.

**No durability.** `record()` writes with a buffered text-mode append and does
not call `os.fsync`. A comment at `admin_cmds.py:270` states that it fsyncs;
that comment is stale. The module docstring's "append-binary mode" is also
wrong - `record()` opens text mode `"a"` for the non-creation path.

**Tail truncation is undetectable** from the file alone, as the module docstring
correctly states. Editing, reordering, or deleting any non-tail record is
caught.

**No cross-process locking.** One `threading.Lock`, no `flock`. Two processes
sharing the directory will corrupt the chain.

Detail in [audit_log internals](internals/audit_log.md).

## 10. Filesystem protections

| Artifact | Mode | Enforced how | Fail behavior |
|---|---|---|---|
| `config.ini` | 0600 | `secret_store.perms_ok()` on every read | Fail-closed: all secrets read as unset |
| `audit.log` | 0600 | `os.open` at creation, `chmod` after every write | Best-effort, warning only |
| `audit.log.key` | 0600 | `os.open` at creation | Best-effort |
| `locations.json`, `channels.json`, `users.json` | 0600 | `chmod` on the temp file before `os.replace` | Warning only |
| `*.json.bak` | umask | Not enforced | PII may be world-readable |
| `internets.log` | umask | Not enforced | Contains PII, typically 0644 |
| Module state JSON (`seen`, `tells`, `notes`, `reminders`, `steamids`, `shadow_bans`) | umask | Not enforced | No checksum, no quarantine |

`store.Store` is the only persistence layer with an integrity envelope. Its v2
format wraps the payload as `{schema, checksum, data}` with a SHA-256 over the
canonical JSON. On read, a checksum mismatch or an unknown schema raises
`_StoreRejected` and the file is **quarantined** to a `.corrupt.*` name rather
than being loaded empty and overwritten by the next flush - which would destroy
the only copy of saved locations, channel-rejoin state, and privacy opt-out
flags. Legacy v1 bare payloads are accepted and rewritten as v2 on the next
flush.

Writes are atomic: `mkstemp` in the same directory, chmod 0600 on the temp file
before the rename, one-deep `.bak` copy of the previous good file, then
`os.replace`. The `.bak` is written with `write_bytes` and never chmod'd, so its
first creation takes the umask.

The six module-owned JSON stores have none of this. A corrupt file is loaded as
empty, logged as a warning, and overwritten on the next save. For
`shadow_bans.json` that means an unclean restart silently un-bans everyone.

Startup drops no privileges but refuses to run as root: `internets._entry()`
exits if `geteuid() == 0` unless `INTERNETS_ALLOW_ROOT=1` is set, which logs a
warning naming the override. `ProcessLock` on `./internets.pid` prevents two
instances from sharing the state files.

## 11. Dependency security

- `requirements.lock` is a `pip-compile --generate-hashes` lockfile; CI installs
  with `--require-hashes`, so a tampered artifact fails the install rather than
  being executed.
- `pip-audit` runs against the lockfile on push, on pull request, and weekly on
  a schedule, with `--strict`. It covers the lockfile only - the optional-extra
  floors in `pyproject.toml` are not audited.
- `bandit` runs twice: an informational MEDIUM-and-above pass, and a gating pass
  that fails CI on HIGH severity at HIGH confidence. SARIF is uploaded to code
  scanning.
- CodeQL runs in `security-extended` mode. Four `py/overly-permissive-file`
  alerts are open, triaged as benign, and deliberately left.
- Every GitHub Action is pinned to a commit SHA, not a tag.
- Dependabot watches pip and GitHub Actions daily, with security updates grouped
  separately from routine bumps so they can merge independently.
- Workflow permissions default to `contents: read`; jobs elevate only where a
  SARIF upload requires `security-events: write`.

Known drift at the time of writing: `requirements.lock` carries a header saying
it was compiled with Python 3.14, which violates the resolve-on-3.10 contract in
`scripts/regen-lockfile.sh`, and the CI Tests workflow has been red on `main`
since 2026-08-13 as a result. Separately, this machine's installed bcrypt is a
major version behind the one `requirements.lock` pins, which matters because the
two differ in how they treat an over-long password: the pinned major raises,
the installed one truncated. Verify with `pip show bcrypt` against the lockfile
before trusting a local bcrypt test result. Neither is a documentation fix; both
are recorded in [known issues](known-issues.md).

## 12. Known limitations

These are verified defects and accepted gaps, stated without qualification. None
of them is theoretical; each was confirmed against source or reproduced.

### API keys can be published to a channel

`modules/stocks.py - _try_providers()` builds its failure reply by appending
`str(exception)` for each provider that failed:

```python
errors.append(f"{name}: {e}")
...
return f"all providers failed for '{symbol}' ({'; '.join(errors)})"
```

`urllib3` transport exceptions embed the full request URL, including the
`token=` or `apikey=` query parameter. A network outage while finance keys are
configured therefore publishes every configured finance API key to the channel
that ran `.stock`. Reproduced empirically. `redact_secrets` does not help,
because it is applied to the log path only and never to a composed PRIVMSG.

The same URL-bearing exception pattern appears in `log.warning` calls in
`imdb`, `lastfm`, `youtube`, `steam`, and `twitch`. Those are log-only and
lower severity, but the same class.

A correct pattern already exists in-repo: `weather_providers/pirateweather/_codes.py
- safe_get_json()` redacts the key before the exception escapes.

### `.isprime` is an any-user denial of service

`modules/mathx.py - cmd_isprime()` calls `_isprime()` synchronously on the event
loop. Composites surviving 2^20 trial division fall into an unbounded Pollard
rho. A pasted 100-digit semiprime hangs the entire bot - not just that command,
the whole process, including admin commands. The sibling `cmd_bignum` uses
`asyncio.to_thread` and does not have this problem. The 60-second `_run_cmd`
timeout does not help: it cancels an await, and this handler never yields.

### The audit chain can be downgraded

Described in [section 9](#9-audit-integrity). An actor with write access to
`audit.log` can rewrite history from any point using legacy-form records and
have `verify()` report the chain intact, without needing the HMAC key. Rotated
segments are never verified at all, and no writer in this repository calls
`os.fsync` - verified: `os.fsync` appears nowhere in the source tree, so an
audit record acknowledged to an admin may not have reached disk when the host
loses power.

### Privacy leaks into the unprotected bot log

Two modules write user data at INFO into `internets.log`, which is created at
umask default rather than 0600:

- `modules/linktitle.py` logs every announced and every skipped URL together
  with the channel it appeared in. That is a per-channel record of what the
  channel's users are browsing. The nick is not logged.
- `modules/location.py - cmd_regloc()` logs `regloc <nick> -> <location>`,
  a direct nick-to-location pair.

Neither is reachable by `.forgetme`. `privacy.cmd_forgetme` purges the store's
locations, opt-out flags, and per-channel user tracking, then calls every loaded
module's `forget()` hook - but it has no path into the log file, into rotated
log segments, into `*.json.bak`, into `*.corrupt.*` quarantine files, or into
rotated audit segments. Right-to-erasure is therefore incomplete by
construction, and the residue sits in the one artifact with the weakest
permissions.

`cmd_forgetme` also clears the opt-out flag before calling `user_purge`, so an
untracked user is told "tracking in 1 channel(s) (erased now)" when the counted
row was the opt-out sentinel.

### The two SSRF guards disagree on `fec0::/10`

`modules/_netsafe.py - ip_is_blocked()` rejects IPv6 site-local addresses;
`modules/base.py - resolve_public()` does not test `is_site_local` and lets them
through. The network probers use the permissive one. Verified on a live
interpreter. `fec0::/10` is deprecated (RFC 3879) and rarely routed, which
bounds the practical impact, but the divergence means the two guards cannot be
reasoned about as one control.

### No writer in the repository calls fsync

Stated separately because it is broader than the audit log. `os.fsync` appears
nowhere in the source tree. `store.Store._write()`, `audit_log.record()`,
`secret_store._atomic_write_text()`, and all six module JSON stores rely on
`os.replace` for atomicity but not on any flush for durability. `os.replace` is
atomic with respect to a concurrent reader; it is not a commit. A power loss can
lose acknowledged writes in every one of these paths.

### Concurrency gaps on shared mutable state

`internets._save_shadow_bans()` passes `_shadow_bans` and `_shadow_ban_reasons`
to `json.dump` inside a `to_thread` worker with no lock held, while
`cmd_shadow_ban` and `cmd_shadow_unban` mutate them on the event loop. The same
pattern exists in `modules/notes.py` and `modules/steam.py`. The observable
failure is a dropped write or a `RuntimeError` during iteration, not a
privilege escalation, but the shadow-ban list is a security control.

### Unauthenticated metrics endpoint

`metrics.MetricRegistry.expose()` refuses to bind an unspecified address
(`0.0.0.0`, `::`, and whitespace variants) and defaults to `127.0.0.1:9779`, but
`/metrics` has no authentication of any kind. Anything that can reach the bound
address reads the counters. A specific non-loopback address is accepted; only
the all-interfaces forms are refused. The exporter is single-threaded, so a
stalled scraper blocks it.

Six of the ten default metrics have no update call site and are permanently
zero.

### Other accepted gaps

- The stdin console is unauthenticated and can shut the bot down.
- Admin authority has no tiers; `.raw` is unrestricted arbitrary protocol injection.
- Channel-name validation hard-codes `#&+!` (`channels._CHAN_RE`, and the
  prefix tuple in `IRCBot.reply()`) instead of reading `CHANTYPES` from
  ISUPPORT; only `CHANMODES` and `PREFIX` are consumed from 005.
- Inbound `PING` is matched with `startswith("PING")`, so a prefixed `PING` from
  a server that sends one would go unanswered.
- `internets._handle_cap` mishandles multiline `CAP LS 302` continuations and
  replaces rather than unions `_caps` on a second `ACK`.

## Cross-references

- [Configuration reference](configuration.md) - every key, its default, and its security implications
- [Logging and auditing](logging-and-auditing.md) - operational view of the log and audit surfaces
- [State and persistence](state-and-persistence.md) - the store integrity envelope and what it does not cover
- [Administration](administration.md) - the admin command surface in operational terms
- [Security policy](security-policy.md) - reporting process
- [Implementation reference](internals/index.md) - line-level detail for every file named here
