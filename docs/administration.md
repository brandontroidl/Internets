# Administration

The privileged surface: how an operator authenticates to the bot over IRC, what each
admin command actually does, how far each one reaches, what lands in the tamper-evident
audit trail, and which changes take effect live versus needing a restart.

Handler-level mechanism lives in [internals/admin_cmds.md](internals/admin_cmds.md);
session and identity semantics live in
[internals/internets.md](internals/internets.md); the audit store itself is
[internals/audit_log.md](internals/audit_log.md). Day-to-day procedures are in
[operations.md](operations.md); symptom-driven diagnosis is in
[troubleshooting.md](troubleshooting.md).

There are two other admin-equivalent surfaces this document does not cover, both by
design outside the IRC authentication model:

- The **interactive console** on stdin, which is unauthenticated and grants shutdown,
  log control, and status to anyone with terminal access
  ([internals/console.md](internals/console.md)).
- Direct **filesystem access** to `config.ini`, the state files, and `audit.log`.
  Anyone with that has already won; the controls here assume they do not have it.

## Authentication model

### `.auth` is PM-only

```text
/MSG <botnick> AUTH <password>
```

The PM requirement is enforced in the dispatcher (`IRCBot._dispatch()`), not in the
handler, and it applies to `.deauth` as well. Typing `.auth` in a channel produces
`must be used in PM.` and the password is never evaluated. The command word and its
argument are masked wholesale in the command log before anything else happens, so an
accidental channel `.auth` does not put the password in the log either - but it does
put it in the channel, so rotate the password if that happens.

Preconditions, in order:

1. No configured `password_hash` - reply points at `hashpw.py`. Admin auth is
   *disabled*, not broken; this is the documented first-run state.
2. No argument - usage.
3. Argument longer than 128 UTF-8 **bytes** (`hashpw.MAX_PASSWORD_BYTES`) - explicit
   too-long reply. The limit is imported from `hashpw` so creation and verification
   cannot disagree; they previously did, by a factor of eight, which let an operator
   create a password that hashed cleanly and could then never authenticate.

Verification runs in a worker thread (`asyncio.to_thread`), because scrypt, bcrypt,
and argon2 are deliberately slow and running one on the event loop would freeze the
bot for every user on every attempt.

### Lockout

| Constant | Value | Meaning |
|---|---|---|
| `_AUTH_MAX_FAILS` | 5 | failures before the gate closes |
| `_AUTH_LOCKOUT` | 300 s | sliding window |
| `_AUTH_CLEANUP_THRESHOLD` | 50 | entries before stale pruning |

The window **slides**: any attempt while locked out refreshes the timestamp, so an
attacker trickling one attempt per window does not earn a free verification every
300 s. While locked out, `verify_password` is never invoked at all.

One emergent consequence, pinned by test as intended: window expiry resets the count
only for the gate check, never in the stored dict, and the failure increment re-reads
the stored value. So after a lockout expires, one more wrong password takes the
counter from 5 to 6 and the nick locks again immediately. Post-lockout, an attacker
gets exactly one verification per window until a success, which pops the entry
entirely.

Operator consequence: if you fat-finger your own password six times, wait out the
full 300 s before trying again, and expect exactly one attempt per window until one
succeeds.

### Session binding: nick **and** hostmask

A session is not "this nick is an admin". `IRCBot.is_admin()` grants only when all
three hold:

1. the lowercased nick is present in `_authed`,
2. the caller's **current** hostmask is known and is not the `"unknown"` sentinel,
3. that hostmask equals the one bound at auth time.

Anything else denies, and the check actively **revokes** the stored binding on a
mismatch rather than merely refusing once. The check is fail-closed and runs on every
inbound command, not once at auth time.

Binding happens at success under two time-of-check-to-time-of-use guards, because the
verify ran in another thread: the current hostmask is re-read, a missing or `unknown`
value refuses to bind (`can't confirm your hostmask right now`), and a hostmask that
changed across the await refuses too (`identity changed during auth`). Both fail
closed rather than create a nick-only session a nick-grabber could inherit.

Sessions are revoked on:

| Event | Scope | Why |
|---|---|---|
| `.deauth` | the caller | explicit |
| QUIT | that nick | identity gone |
| NICK change | that nick | identity change, never migrated |
| Hostmask change observed on any command | that nick | binding broken |
| Disconnect / reconnect | all | sessions never survive a link drop |
| SIGHUP / `.rehash` | all | the password may have been rotated |

A NICK change **drops** the session rather than following the nick. That is
deliberate: migrating it would let a nick change launder an authenticated session onto
a different identity.

`.deauth` takes no admin gate - it is only meaningful to an authenticated session, and
a non-authed caller simply gets `not authenticated`. It audits only when a session
actually ended.

## The admin command surface

`_CORE` is the single dispatch table, and the `.help` listings derive from it, so a
new core command cannot silently skip help. Four commands are public
(`_CORE_PUBLIC`): `.help`, `.modules`, `.version`, `.auth`. Everything else in the
table is admin-gated, including `.deauth`, which stays on the admin side of the help
split.

Command names that are admin-only are invisible to non-admins in `.help`: a named
lookup falls through to the same generic `no command '<x>' loaded` a nonexistent
command produces, so the help surface does not leak the admin roster.

### Blast radius

Ordered roughly by reach. "Audited" means a durable record lands in `audit.log`.

| Command | Reach | Audited |
|---|---|---|
| `.raw <line>` | arbitrary IRC protocol as the bot | yes, redacted |
| `.shutdown` / `.die [reason]` | stops the process | yes, before shutdown |
| `.restart` | replaces the process image | yes, before shutdown |
| `.load` / `.unload` / `.reload` / `.reloadall` | arbitrary code execution in-process | yes, module name |
| `.rehash` | re-reads both config layers, clears all sessions | yes |
| `.nick <new>` | changes the bot's identity on the network | yes, from/to |
| `.mode <+/-modes>` | user modes on the bot itself only | yes |
| `.snomask <mask>` | server-notice mask on the bot | yes |
| `.say [target] <text>` | speaks as the bot anywhere | yes, target and text |
| `.act [target] <text>` | CTCP ACTION as the bot | yes, target and text |
| `.shadow-ban <nick> [reason]` | silences a user bot-wide, persistently | yes |
| `.shadow-unban <nick>` | reverses the above | yes |
| `.debug` / `.loglevel` | log verbosity, process-wide | yes when arguments given |
| `.fingerprint <nick>` | cross-store profile of one user (PII read) | no |
| `.audit [...]` | reads the privileged action trail | no |
| `.stats` / `.uptime` / `.shadow-list` | read-only diagnostics | no |
| `.deauth` | ends the caller's own session | only if a session ended |
| `.auth` | creates a session | yes on success; failures audited separately |

Read-only commands do not audit, consistently. That is a deliberate policy, not an
oversight: it keeps the trail to state changes and keeps a diagnostic loop from
churning the log through its 5 MiB rotation.

### Reply channel

Admin output uses `preply()`, which sends a NOTICE to the invoking nick when the
command came from a channel, and stays in PM when it started in PM. Quotas, counters,
audit lines, and auth prompts therefore do not spill into the channel even when the
command was typed there. `.health` and `.providers` follow the same convention.

## Audit trail of admin actions

Every state-changing handler calls `AdminCommandsMixin._audit()`, which resolves the
actor's hostmask, sanitizes both actor and hostmask, and appends one HMAC-chained JSON
line. Action names as they appear in `.audit` output:

```text
auth  auth_failed  auth_lockout  deauth
load  unload  reload  reloadall  rehash  restart  shutdown
mode  snomask  raw  say  act  nick
shadow-ban  shadow-unban  loglevel  debug
```

Four properties worth knowing as an administrator:

- **No password material, ever.** A successful `.auth` records `args=None`. A failed
  `.auth` records only `{"fails": n, "max": 5}` - two wrong passwords of length 8 and
  100 produce records identical apart from the counter, so the trail is not a length
  oracle. Backend exceptions are logged by exception *type* only, because some hash
  backends echo fragments of their input in exception text.
- **`.raw` is redacted in the record.** The wire gets the full line; the echo, the bot
  log, and the audit record all get `sender.redact_secrets()` applied, which masks
  everything after the first credential verb (`AUTHENTICATE`, `IDENTIFY`, `REGISTER`,
  `IDENT`, `OPER`, `PASS`, `AUTH`).
- **Actor strings are control-stripped.** C0 and C1 bytes are removed and the field
  truncated to 64 characters before the record is written. Failed-auth records made
  the write path reachable by unauthenticated users, and a nick carrying `\x02` could
  otherwise terminate the bold span in `.audit` output early and fabricate what reads
  as an extra column - attributing an action to the wrong nick. The chain is
  tamper-*evident*, not tamper-proof against garbage fed in at write time; stripping
  at the producer is what keeps the rendered columns honest.
- **Failed-auth auditing is capped.** Recording stops once a nick is locked out,
  because the lockout branch audits nothing. A sustained flood therefore yields at
  most five `auth_failed` records plus one `auth_lockout` record per nick per window,
  and cannot churn the audit log through rotation to destroy older history.

Auditing is deliberately **fail-open at the caller**: `_audit()` catches every
exception and degrades to a log warning, so an audit-store failure never blocks the
admin command it was recording. Availability of admin control was chosen over
guaranteed audit coverage. If you see `audit` warnings in the bot log, treat the trail
as having holes from that point.

For what `.audit verify` does and does not prove - including the verified downgrade
defect and the fact that rotated segments are never verified - see
[operations.md](operations.md#audit-log).

:::{warning}
**Known defect (partial rehash unaudited).** `.rehash` validates the reloaded
`password_hash` prefix and aborts on a bad one, but that abort returns **after** the
new log level has been applied and **before** sessions are cleared and before the
`rehash` audit record is written. A partially applied rehash - log level changed,
sessions kept, a bad hash now live for the next `.auth` - leaves no audit record at
all. Recorded in [internals/admin_cmds.md](internals/admin_cmds.md#findings).
:::

## Shadow-ban system

A shadow-ban silently drops **all** command traffic from a nick at the very top of
`IRCBot._dispatch()`, before the flood gate, before any handler, before any reply.
There is no notice, no rate-limit consumption, and no per-command audit entry. The
banned nick cannot distinguish being ignored from the bot being offline. That
indistinguishability is the point.

| Command | Behavior |
|---|---|
| `.shadow-ban <nick> [reason]` | refuses to ban the bot itself or the invoking admin; dedupes; persists |
| `.shadow-unban <nick>` | removes the nick and its reason; persists |
| `.shadow-list` | read-only listing with reasons; not audited |

Matching is case-insensitive. State lives in `_shadow_bans` and
`_shadow_ban_reasons` in memory and in `shadow_bans.json` on disk, written atomically
at 0600 via mkstemp plus `os.replace`. A load failure degrades to an empty list with
a warning rather than a crash, which means **a corrupt or unreadable
`shadow_bans.json` silently un-bans everyone at the next start**. Check
`.shadow-list` after any restart that followed disk trouble.

:::{warning}
**Known defect (persist race).** `cmd_shadow_ban` / `cmd_shadow_unban` mutate the set
on the event loop, then hand the save to a worker thread that iterates the same set
with no lock. A concurrently scheduled shadow-ban task mutating mid-iteration raises
inside the save, which the host's broad exception handler degrades to a warning - the
persist is silently skipped for that call. The in-memory ban stays effective until
restart; it just may not survive one. Recorded in
[internals/admin_cmds.md](internals/admin_cmds.md#findings).
:::

## Module management

| Command | Effect | Failure behavior |
|---|---|---|
| `.load <name>` | executes `modules/<name>.py` fresh and registers its commands | nothing registered; generic reply, detail in the log |
| `.unload <name>` | runs `on_unload()` then removes commands | on raise, the module stays **fully** loaded |
| `.reload <name>` | strictly unload then load | an unload failure aborts the reload |
| `.reloadall` | reloads every loaded module serially | reports `OK:` / `FAILED:` partitions, no abort on first failure |
| `.modules` | lists loaded modules with command counts, plus available ones | public |

Loading a module is arbitrary code execution inside the bot process, by design. The
controls around it are: only admins can invoke `.load`, `AUTO_LOAD` comes from the
operator's config file, the module name must match `^[a-z][a-z0-9_]*$` (no dots,
slashes, or uppercase), and the resolved path must stay inside `MODULES_DIR` - a
containment check that also defeats a symlink pointing out of the tree. Error text
never reaches IRC; the reply is `Error loading '<name>' - see log for details.` and
the detail lands in the log as `event=module_load_failed`.

Unload deliberately keeps a module whole when its teardown fails, rather than
half-removing it and stranding its commands.

Two module-loader properties that shape what a reload can and cannot fix:

- The loader never creates a `sys.modules` entry for the module, so every `.load`
  executes the file fresh from disk. Nothing module-internal survives - globals,
  caches, and class objects are all new. What does survive is everything bot-owned the
  module reads through `self.bot`: the store, `cfg`, rate-limiter windows, channel
  state.
- Anything the module *imports* is cached normally. `.reload` will not pick up an edit
  to `modules/base.py`, `modules/geocode.py`, `modules/units.py`, or anything under
  `weather_providers/`. Those need `.restart`.

Command-name collisions **between modules** are rejected at load time, and the second
module loses. Collisions with a **core** command are not checked at all:
`_dispatch()` resolves `_CORE` before the module registry, so a module command sharing
a name with a core command is silently unreachable regardless of what `.help` shows.

:::{warning}
**Known defect (live instance).** `modules/health.py` registers `uptime` intending a
public uptime figure, but `_CORE` maps `uptime` to the admin-gated core handler, which
wins. The module's handler never runs and a non-admin gets the auth prompt instead of
the intended public reply. Nothing warns at load time because the loader compares only
module against module. Discovered during this documentation pass; not previously in the
findings ledger.
:::

## Raw protocol injection

`.raw <line>` sends an arbitrary line to the IRC server as the bot. It is the widest
command in the table and the only one whose argument is legitimately a credential.

Guards, in order:

1. Admin gate.
2. CR, LF, and NUL are rejected outright - no smuggling a second command
   (`line contains CR/LF/NUL - rejected.`).
3. The line is capped at 510 UTF-8 bytes (`line exceeds 510 bytes - rejected.`).
4. The full line goes to the wire; a `redact_secrets()` copy goes to the echo, the bot
   log, and the audit record.

What `.raw` does **not** have: any allowlist of verbs, any awareness of what the line
means, or any rollback. `.raw KILL`, `.raw GLINE`, `.raw MODE #chan +b *!*@*`, and
`.raw OPER` are all available to any authenticated admin, and the bot's own state
tracking will not learn about their effects except through whatever the server echoes
back. It is the escape hatch, and it should be treated as one.

The narrower commands exist so `.raw` is rarely necessary, and they validate what
`.raw` cannot:

| Command | Validation |
|---|---|
| `.mode` | `^[a-zA-Z+\- ]+$`, and always targets the bot itself |
| `.snomask` | `^[a-zA-Z+\-]+$`, sent as `MODE <botnick> +s <mask>` |
| `.nick` | RFC 2812 nick grammar; `_nick` updates only on the server's echo |
| `.say` / `.act` | target rejected if it contains a comma or space (a comma would multicast the message to several targets) |

Prefer them. Reach for `.raw` when nothing else expresses the operation, and expect
the line to be in the audit trail permanently.

(admin-live-vs-restart)=
## Decision table: live versus restart

What actually takes effect when. "Live" means the running process picks it up with no
link drop.

| You changed | Do this | Takes effect |
|---|---|---|
| A command module under `modules/` | `.reload <name>` | live |
| Several command modules | `.reloadall` | live |
| `modules/base.py`, `geocode.py`, `units.py` | `.restart` | restart only |
| Anything under `weather_providers/` | `.restart` | restart only |
| `internets.py`, `sender.py`, `store.py`, any core file | `.restart` | restart only |
| `[bot] command_prefix` | `.rehash` | live (read per dispatch) |
| `[logging] level` | `.rehash`, or `.loglevel LEVEL` | live |
| `[admin] password_hash` | `.rehash` | live; all sessions cleared |
| `[metrics]` enable / host / port | `.restart` | restart only |
| `[bot] autoload` | `.restart` | restart only |
| `[bot] modules_dir` | `.restart` | restart only |
| `[bot] api_cooldown` / `flood_cooldown` | `.restart` | restart only |
| NickServ / server / oper password | `.restart` | restart only |
| `[irc] server`, `port`, `nickname`, `realname` | `.restart` | restart only |
| A provider or module API key in `[secrets]` | depends, see below | mostly live |
| An `INTERNETS_*` environment variable | `.restart` | restart only |
| Python dependencies | `.restart` | restart only |

Notes on the rows that are not obvious:

- **Credentials are frozen at import.** `NS_PW`, `SERVER_PW`, and `OPER_PW` are
  computed once by `config.py` and a rehash does not recompute them. The SIGHUP log
  line says so explicitly.
- **Module API keys are usually live** because `modules/base.py - cred()` resolves
  through `secret_store.get()`, which is uncached and re-reads the file on every call.
  A key rotated with `python -m secret_store set` is picked up by the next lookup with
  no restart. Modules that capture a key into an instance attribute at load time are
  the exception; `.reload <module>` covers those.
- **An environment variable cannot change under a running process**, so the env tier
  of secret resolution always needs a restart.
- **`.rehash` clears every admin session** as a side effect. You will need to
  re-authenticate. That is intentional, not a bug: a rehash may have rotated the
  password out from under a live session.

## Admin safety guidance

Ordered by how much grief each one has the potential to cause.

**Authenticate in PM, always.** The dispatcher enforces it, but the enforcement is
after the message has already reached the channel. If you do type it publicly, rotate
the password.

**Keep `.raw` for last.** Use the validated command if one exists. If you must use
`.raw`, read the line back from the `>> ...` echo before assuming it did what you
meant, and remember the echo is the redacted copy.

**Assume no rollback.** Nothing in the admin surface is transactional. `.shadow-ban`
persists immediately, `.nick` changes network identity, `.raw` is gone the moment it
is enqueued, and `.restart` replaces the process.

**Prefer restart over a partial reload.** A `.reload` that silently used a cached
helper module is worse than a five-second outage, because it looks like it worked. See
the table above.

**Deauthenticate when you are done**, especially from a shared or bouncer-backed
connection. The session is bound to your hostmask, not to your client, so it survives
you walking away.

**Watch what you put in `.say`.** The text is recorded in the audit trail verbatim,
deliberately - impersonation by bot is exactly what the trail should attribute.

**Treat `.fingerprint` as a privacy operation.** It cross-references one nick across
every store the bot holds: current hostmask, per-channel membership, shadow-ban
status, last-seen, pending tells, note **count** (never note content), and audit
mentions. It is not audited, so there is no record of who looked. Use it for
moderation, not curiosity.

**Do not hand-edit `config.ini`, `config.local.ini`, `audit.log`, or the state files
while the bot is running.** The bot owns those writes. Hand-editing `audit.log`
specifically will break the chain and `.audit verify` will report it.

**Check `.shadow-list` after any unclean restart.** A corrupt `shadow_bans.json`
degrades to an empty list with only a log warning.
