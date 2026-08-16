# Logging and auditing

The bot writes two independent record streams with different purposes,
different guarantees, and different threat models.

| | Application log | Audit log |
| --- | --- | --- |
| Owner | `botlog.py` | `audit_log.py` |
| Default file | `internets.log` | `audit.log` |
| Purpose | diagnosis | tamper-evident accountability |
| Format | human text | one JSON object per line |
| Integrity | none | HMAC-SHA-256 chain |
| Permissions | umask default | 0600, re-asserted per append |
| Rotation | size, 3 copies kept | size, segments kept forever |
| Content policy | injection-sanitized, credential-redacted | never records a password |

Neither stream can be purged by `.forgetme`. Both are covered by the backup
set in [state-and-persistence](state-and-persistence.md).

## Part 1: the application log

### Initialization

`botlog.py` builds the entire logging stack as an **import side effect**, and
importing it transitively triggers `config.py`'s import-time work, including
`argparse.parse_args()` on the live `sys.argv`. The order of side effects is
fixed:

1. `_setup_logging()` configures the `internets` logger and returns the
   process-global `DebugFilter`.
2. `log.info("Internets v<version> starting")`.
3. CLI `--debug` flags are applied.
4. `_validate_hash()` runs and may `sys.exit(1)`.
5. A world-readable `config.ini` produces a warning (POSIX only).
6. Mode-string validation runs and may `sys.exit(1)`.

After import the module is passive. `internets.py` flushes the handlers
during shutdown; nothing else is torn down.

### The handler tree

Root of the tree is `logging.getLogger("internets")`, set to `DEBUG` with
`handlers.clear()` first so the setup is safe to re-run. Every module obtains
`logging.getLogger("internets.<name>")` and inherits these handlers without
importing `botlog` at all.

| Handler | Sink | Filter | Rotation |
| --- | --- | --- | --- |
| `RotatingFileHandler` | `[logging] log_file` | `DebugFilter` | `max_bytes`, `backup_count` |
| `StreamHandler` | stdout | `DebugFilter` | n/a |
| `RotatingFileHandler` | `[logging] debug_file`, if set | **none** | same caps |

All three handler *levels* are `DEBUG`. The effective severity gate is the
`DebugFilter`, not handler levels. The optional debug-file handler
deliberately omits the filter so it captures everything at DEBUG regardless
of the runtime base level, which is what makes it useful for protocol
diagnostics; it is tagged with a `_debug_file = True` attribute for
introspection.

Format is fixed in code, not configurable:
`%(asctime)s [%(levelname)s] %(name)s: %(message)s`.

### Configuration

```ini
[logging]
level        = INFO
log_file     = internets.log
max_bytes    = 5242880      ; 5 MB per file before rotating
backup_count = 3            ; keep internets.log.1 .. .3
debug_file   =              ; blank disables the unfiltered debug sink
```

CLI flags override config at startup:

| Flag | Effect |
| --- | --- |
| `--loglevel LEVEL` | overrides `[logging] level` for the base gate |
| `--debug` | bare form sets `global_debug` on |
| `--debug SUB [SUB ...]` | namespaces each to `internets.<SUB>` and enables it |
| `--debug-file PATH` | overrides `[logging] debug_file` |
| `--no-console` | unrelated to logging; disables the stdin console |

> **Known issue** (`botlog.py - _setup_logging()`): an unrecognized
> `[logging] level` value degrades silently to INFO via
> `getattr(logging, LOG_LEVEL, logging.INFO)`. Every other config guard in
> the file fails loudly; this one does not even warn, so a typo such as
> `level = WARN` looks applied and is not.

### The severity model and per-subsystem debug

`DebugFilter.filter()` passes a record if **any** of three conditions holds:

1. `record.levelno >= base_level` - the normal severity gate, seeded from
   `[logging] level` or `--loglevel`.
2. `global_debug` is on.
3. The record's logger name equals a registered subsystem, or is a dotted
   child of one. The boundary is an explicit `name.startswith(sub + ".")`,
   so subsystem `internets.weather` does not match logger
   `internets.weatherx`.

Because condition 3 still requires the *logger* to emit DEBUG records at all,
every enable path does two things: register the subsystem with the filter and
call `setLevel(logging.DEBUG)` on that logger.

`VALID_LEVELS` is the operator-facing allowlist: `DEBUG`, `INFO`, `WARNING`,
`ERROR`. `CRITICAL` is not offered, but CRITICAL records pass regardless
since they exceed any base level.

### Runtime control

Three front ends drive the same process-global filter instance:

| Surface | Debug | Level |
| --- | --- | --- |
| IRC (admin) | `.debug [on\|off]`, `.debug <sub> [off]` | `.loglevel [LEVEL]`, `.loglevel <logger> LEVEL` |
| Console | `debug ...` | `loglevel ...` |
| CLI at startup | `--debug [SUB ...]` | `--loglevel LEVEL` |

Both IRC commands are admin-only and both are audit-logged. `cmd_debug`
lowercases its arguments before dispatch; `cmd_loglevel` does not, and
`apply_loglevel` uppercases the level itself.

Semantics of `botlog.py - apply_debug()`:

- no args, or `on` - `global_debug = True`.
- `off` - `global_debug = False` **and** clear every registered subsystem.
- `<sub>` - namespace to `internets.<sub>` unless the argument already starts
  with the literal `internets`, set that logger to DEBUG, register it.
- `<sub> off` - set the logger to `NOTSET` and deregister.

Semantics of `botlog.py - apply_loglevel()`:

- no args - print the status report: base level, global-debug flag, active
  subsystems, and the debug file path if configured.
- `LEVEL` - validate against `VALID_LEVELS`, set `base_level`, and clear
  `global_debug`.
- `<logger> LEVEL` - the target **must** start with `internets`, so
  `.loglevel weather DEBUG` is rejected and `.loglevel internets.weather
  DEBUG` is the working form. `DEBUG` registers the subsystem; `NOTSET` and
  every explicit level deregister it and set the logger level directly.

`.rehash` (and SIGHUP) resets the base level from the reloaded config and
clears both `global_debug` and the subsystem set.

> **Known issue** (`botlog.py - apply_loglevel()`): setting a base level
> clears `global_debug` but leaves per-subsystem debug sets active, whereas
> `.rehash` clears both. An operator who raises the level to WARNING still
> receives DEBUG from any subsystem enabled earlier, with no indication in
> the reply. `.loglevel` with no arguments lists the active subsystems, which
> is the way to notice.

### Logger names

The tree mirrors the source layout: `internets.<module>` for every command
module, plus these core loggers, which are the useful `--debug` targets:

`internets.conn`, `internets.dispatch`, `internets.modules`,
`internets.shutdown`, `internets.signal`, `internets.sasl`,
`internets.sender`, `internets.store`, `internets.secrets`,
`internets.audit`, `internets.process_lock`, `internets.metrics`,
`internets.netsafe`, `internets.hashpw`, `internets.geocode`.

The weather provider tree logs under `internets.weather.*`
(`internets.weather.dispatch`, `.http`, `.providers`, `.health`, and one per
keyed provider), so `--debug weather` enables the module and every provider
under it through the dotted-child rule.

### Log-injection protection

`botlog.py - _SafeFormatter` subclasses `logging.Formatter` and sanitizes
user-controlled data before interpolation:

- Strips ASCII C0 controls except TAB (so CR, LF, NUL, ESC), DEL (0x7f), and
  C1 controls (0x80 to 0x9f, the terminal CSI vectors).
- Applies that cleaning to `record.msg` (after `str()`) and to every element
  of `record.args`, in both tuple and dict form. Non-string args pass
  through untouched.
- Works on a copy built with `logging.makeLogRecord(record.__dict__)`, so
  other handlers see the original record.
- Exception tracebacks survive, because they render via `record.exc_text`
  downstream rather than through `msg` and `args`.

This defeats the classic log-forging vector - `log.info("cmd: %s",
attacker_input)` with an embedded CRLF forging a fake log line - and the
ESC-sequence attack against whoever later views the file in a terminal.

### Credential redaction and its exact scope

Redaction is a **separate** mechanism from injection sanitization, and it
lives in `sender.py`, not `botlog.py`. There is no redaction hook inside the
formatter, so a module that logs a secret ships it to all three sinks
unmodified; the codebase relies on call-site discipline for that.

`sender.py - redact_secrets()` masks everything after the first credential
verb, keeping the verb:

```
_SECRET_VERBS = AUTHENTICATE, IDENTIFY, REGISTER, IDENT, OPER, PASS, AUTH
```

Matching is case-insensitive and word-boundaried, longest verb first, so
`IDENTIFY` wins over `IDENT` and a verb embedded in another word
("password", "compass") is not a false positive. A bare verb with no argument
is left alone.

It is applied in both directions so a credential cannot be redacted on one
side and leak on the other:

| Call site | Covers |
| --- | --- |
| `sender.py - Sender._write_line()` | every outbound line, at DEBUG |
| `internets.py - _redact_inbound()` | inbound PRIVMSG/NOTICE trailing text |
| `internets.py - _handle_privmsg()` | the `arg=` field of the `cmd=` log line |
| `admin_cmds.py - cmd_raw()` | the echo, the log line, and the audit record |

`_redact_inbound()` first scopes to the trailing text after
`PRIVMSG|NOTICE <target> :`, because a sender hostmask like `ident@host`
would otherwise match the `IDENT` verb. `.auth` and `.deauth` arguments are
not redacted but fully replaced with `[REDACTED]`.

> **Scope limit, stated plainly**: redaction is **log-only**. The bytes on the
> wire are never altered, and `redact_secrets()` is not applied to outbound
> PRIVMSG *content*. It is a logging hygiene control, not an output filter.

> **Known defect** (`modules/stocks.py - _try_providers()`, ledger entry
> "SECURITY DEFECT ... stocks key leak", verified empirically): the
> "all providers failed" reply appends `str(exception)` to an IRC message.
> urllib3 transport errors embed the full request query, including
> `token=` and `apikey=` parameters, so a network outage while finance keys
> are configured publishes those keys to the channel. `redact_secrets` does
> not help here because it is not on the PRIVMSG path and the leaked material
> does not follow a credential verb. A lower-severity instance of the same
> pattern (log-only) exists in the URL-bearing `log.warning` calls in
> `imdb`, `lastfm`, `youtube`, `steam`, and `twitch`.

### Rotation

Both file handlers are `logging.handlers.RotatingFileHandler` with
`maxBytes=LOG_MAX` and `backupCount=LOG_BACKUPS`, UTF-8 encoded. There is no
time-based rotation. At the defaults the main log occupies at most four
files (`internets.log` plus `.1` to `.3`), and the optional debug file
rotates on the same caps.

Rotated copies are outside every erasure path, which matters for the privacy
findings below.

### Startup validation that logs or refuses to boot

| Check | Outcome |
| --- | --- |
| `[admin] password_hash` empty | WARNING; bot runs with admin auth disabled |
| hash prefix not `scrypt` / `bcrypt` / `argon2` | `log.critical` + `sys.exit(1)`; value never echoed |
| `config.ini` world-readable (POSIX) | WARNING suggesting `chmod 640` |
| `user_modes` / `oper_modes` / `oper_snomask` fail `^[a-zA-Z+\- ]*$` | `log.critical` + `sys.exit(1)` |

The mode-string allowlist is a security control, not cosmetics: those strings
are sent raw into IRC `MODE` and `OPER` lines, so the pattern blocks CRLF or
separator injection from config into the protocol stream.

One failure mode is unguarded: `_setup_logging()` does not catch `OSError`,
so an unwritable `log_file` or `debug_file` path aborts import with a raw
traceback rather than the actionable message the other guards produce.

### Privacy in the application log

Three paths put user data into `internets.log`, where `.forgetme` cannot reach
it and where rotated copies persist. The first is by far the broadest:

> **Known defect** (privacy): `internets.py - _handle_privmsg()` logs one INFO
> line per accepted command carrying the command name, the **full argument
> text**, the invoking nick, the invoker's hostmask, and the channel or `(PM)`.
> PMs to the bot are logged the same as channel lines, so the bodies of `.tell`,
> `.note`, `.remind`, and `.regloc` all land here. The only masking is the
> credential-verb pass described above, which is keyed on `IDENTIFY`/`OPER`/etc.
> and therefore does not fire on an argument that is itself a secret - notably
> `.pwn <password>`, whose plaintext argument is written to the log despite the
> command being PM-only and sending only a hash prefix upstream.

> **Known defect** (privacy): `modules/location.py - cmd_regloc()` logs
> `regloc <nick> -> '<raw input>' (<resolved place>)` at **INFO**, pairing an
> IRC nick with a self-reported location and its geocoded name.

> **Known defect** (privacy): `modules/linktitle.py - on_raw()` logs every
> announced URL with its target channel, and every skipped URL without one, at
> **INFO**, which accumulates channel browsing activity. Neither line carries
> the speaking nick, so the exposure is per-channel rather than per-user;
> correlating it with the same file's `cmd=` lines closes that gap.

Both were surfaced during the documentation reconstruction (see
[known issues](known-issues.md), batch B and batch H). Lowering them to DEBUG is
the obvious remediation, but that is an owner decision, not a documented
behaviour.

## Part 2: the audit log

### Why it is separate

`audit_log.py` exists apart from the ordinary log stream because the audit
trail has different requirements: durable, bounded, permission-restricted
(0600, because records carry hostmasks), and tamper-evident rather than
merely informational. It records **who did what**, for privileged actions
only.

### Record format

One compact JSON object per line:

```json
{"v":2,"ts":"2026-08-15T12:04:31.442190Z","actor":"alice",
 "host":"alice!user@host","action":"reload","args":"weather",
 "prev_hash":"<64 hex>","this_hash":"<64 hex>"}
```

| Field | Meaning |
| --- | --- |
| `v` | record version; `2` means HMAC-SHA-256, absent means legacy |
| `ts` | UTC, `YYYY-mm-ddTHH:MM:SS.ffffffZ`, 27 characters |
| `actor` / `host` | nick and resolved hostmask, control-bytes stripped |
| `action` | the audited verb (table below) |
| `args` | structured when JSON-serializable, else a deterministic string |
| `prev_hash` / `this_hash` | the chain links |

`args` is kept in its original shape (dict, list, scalar) whenever it
survives `json.dumps` without a `default=` fallback, so `.audit grep` and
`.fingerprint` can match inside it. Otherwise it is stored as the stable
string form.

### The hash chain

```
this_hash = HMAC-SHA-256(key, prev_hash | ts | actor | host | action | args_str)
```

The six fields are joined with NUL separators (`_canonical()`), so a value
containing a delimiter cannot shift field boundaries in the hashed form.
`args_str` comes from `_stable_args_str()`: `None` becomes the empty string,
a `str` passes verbatim, anything else becomes compact JSON with sorted keys
and `default=str`, with a `repr()` fallback for unserializable values. The
first record chains from the genesis hash, 64 zeros.

The key is 32 bytes from `secrets.token_bytes`, hex-encoded in a 0600 sidecar
`audit.log.key` (the module docstring calls it `audit.key`; the class
docstring and the code agree on `<path>.key`). The threat model is explicit:
an attacker who obtains only a *copy* of `audit.log` - a backup, an
accidental commit - cannot forge entries, because the key is not in the copy.

Key lifecycle is fail-closed in the direction that matters. An existing but
unreadable key raises `RuntimeError` rather than regenerating, because
regeneration would silently void every prior record's HMAC. A malformed or
short key is renamed to `audit.log.key.bad` and a fresh one generated; prior
v2 records then fail verification under the new key, and recovery is manual
from the `.bad` file. A key that cannot be persisted is still used in memory,
with an error logged saying the chain will not survive restart.

### What is audited

`admin_cmds.py - AdminCommandsMixin._audit()` resolves the actor's hostmask,
strips control bytes from both actor and hostmask, and calls `record()`.
Twenty-one actions are wired:

```
act            auth           auth_failed    auth_lockout   deauth
debug          load           loglevel       mode           nick
raw            rehash         reload         reloadall      restart
say            shadow-ban     shadow-unban   shutdown       snomask
unload
```

Auditing failures never break the admin command: `_audit()` catches
everything and logs a warning, choosing availability over the record. That is
a deliberate caller-side decision, not a property of `audit_log.py`.

Most handlers call `record()` synchronously on the event loop, because the
write is small and rare. `cmd_auth()`'s failure path is the exception: it
runs the write through `asyncio.to_thread`, outside `_auth_lock`, because
that path is reachable by unauthenticated users under flood. Recording also
stops once a nick is locked out, so a sustained flood cannot churn the log
through its rotation and destroy older history.

### Reading it back

`.audit` (admin only) reads the live file directly:

| Form | Behaviour |
| --- | --- |
| `.audit` | last 10 records |
| `.audit <N>` | last N, clamped to 1..200 |
| `.audit tail` | last 5 |
| `.audit grep <pattern>` | case-insensitive substring over the rendered record, last 50 matches |
| `.audit verify` | walk the chain; report intact plus record count, or the broken index |

`.stats` reports the record count, and `.health` reports chain integrity
(`intact` / `BROKEN at record index N` / `unavailable`) alongside its other
probes.

Display-side injection is closed at write time: `_clean_actor()` strips C0
and C1 control bytes from actor and hostmask, so a formatting byte in a nick
cannot forge an extra column in the `.audit` output and misattribute an
action. Note that `args` is control-stripped at neither end; in practice args
are admin-supplied or structured dicts.

### Verification and its limits

`AuditLog.verify()` re-walks the whole live file from genesis under the lock.
Per non-blank line it parses JSON, requires a dict, requires
`prev_hash == prev`, recomputes the digest, and compares. It returns
`(True, -1)` for an intact or absent file, `(False, idx)` at the first broken
record. Blank lines are tolerated and do not advance the index.

> **Known defect** (`audit_log.py - AuditLog.verify()`, ledger entry
> "Security concern (VERIFIED by orchestrator)"): the hash scheme is chosen
> from the record's own `v` field. Any record lacking `v == 2` is verified
> with **unkeyed SHA-256**, at any position in the chain. An attacker with
> write access to `audit.log` alone - no key required, the algorithm is in
> this file - can truncate the chain at any record and append forged
> legacy-format records that chain by plain SHA-256 from the last surviving
> `this_hash`, and `verify()` reports the chain intact. This contradicts the
> module docstring's claim that editing, reordering, or deleting any non-tail
> record is caught, and reduces effective tamper-evidence to that of the
> pre-3.0.0 scheme. A fix shape would accept legacy records only *before* the
> first v2 record (a chain, once upgraded, never downgrades) or pin a cutover
> index. Owner decision; not fixed.

> **Known defect** (`audit_log.py - AuditLog.record()`): there is no
> `os.fsync()` anywhere in the codebase. `record()` writes and closes, which
> flushes Python's buffer to the kernel page cache; records survive process
> death from that point, but a power loss or host crash can drop the most
> recent ones - including the `restart` and `shutdown` records that
> `cmd_restart()` and `cmd_shutdown()` deliberately write *early*, before
> requesting shutdown, so they cannot be lost. The comment at
> `admin_cmds.py - cmd_auth()` reading "record() writes and fsyncs" is
> stale and wrong. (The ledger records two such comments; a repository-wide
> search finds exactly one explicit fsync claim. The module docstring's
> separate claim that `record()` "opens the file in append-binary mode" is
> also wrong - it opens in text mode.)

### Rotation and its forensic limits

At 5 MiB the live file is renamed to `audit.log.<UTC %Y%m%dT%H%M%SZ>` and the
tip resets to genesis, so the new file starts a fresh chain. Rotation runs
*before* the tip is used, so a post-rotation record correctly chains from
genesis.

> **Known defect** (`audit_log.py - _rotate_if_oversize()` and
> `admin_cmds.py - cmd_audit()`): rotated segments are never verified and
> never displayed. `verify()`, `count()`, `.audit`, `.fingerprint`, and
> `.health` all read only the live `audit.log`. Consequences:
>
> - Each segment remains independently verifiable against the same key, but
>   nothing links segments cryptographically, so deleting an entire rotated
>   segment is undetectable by `verify()`.
> - `.audit verify` reports "intact" even when a rotated segment was
>   tampered with or removed.
> - History that has rotated is invisible to the bot's own tooling and must
>   be inspected on disk.
>
> A rotation failure is logged and the log simply keeps growing past the cap
> until a later attempt succeeds: fail-open on boundedness, fail-safe on
> data.

> **Known issue** (`audit_log.py - _rotate_if_oversize()`): the rotation
> stamp has one-second granularity and `Path.rename()` silently replaces an
> existing destination on POSIX, so two rotations within the same second
> would destroy the first segment. Reaching 5 MiB twice in one second is
> implausible under the capped auth-flood path, but the loss would be silent.

### What is deliberately never recorded

- **Passwords, password lengths, and any derivative.** `cmd_auth()` passes
  `args=None` on success and only the failure counter on failure. The
  in-code comment states the rule explicitly, because the record is durable
  and readable over IRC via `.audit`.
- **The raw argument of `.raw`.** That is the one command whose argument can
  carry an arbitrary credential-bearing protocol line (`identify <pw>`,
  `oper <name> <pw>`). The wire gets the full line; the echo, the log, and
  the audit record all get the `redact_secrets()` copy.
- **Cross-process serialization.** Stated as out of scope in the module
  docstring: one `threading.Lock` serializes writers within a process, there
  is no `flock`, and two processes writing the same file would interleave.
  Single-instance safety comes from
  [internals/process_lock](internals/process_lock.md).

## Operational notes

- To trace one subsystem without flooding the log, prefer
  `.debug <subsystem>` over `.debug on`; check `.loglevel` with no arguments
  afterwards to confirm what is actually enabled, and remember that
  `.loglevel WARNING` will not turn it off.
- For protocol-level diagnosis set `[logging] debug_file` (or
  `--debug-file`). That sink has no filter attached, so it captures
  everything at DEBUG regardless of the runtime base level, and it rotates on
  the same caps as the main log.
- Verify the audit chain after any host-level incident, but treat an
  "intact" verdict as covering the live file only, and check the rotated
  segments and the key sidecar by hand.
- Back up `audit.log`, `audit.log.key`, and every `audit.log.<stamp>`
  segment together. The log without the key cannot be verified; the key
  without the log proves nothing.

## Related reading

- [internals/botlog](internals/botlog.md) - handler construction, filter,
  and startup validation line by line.
- [internals/audit_log](internals/audit_log.md) - record, chain, key
  lifecycle, rotation.
- [internals/sender](internals/sender.md) - where outbound redaction is
  applied.
- [internals/admin_cmds](internals/admin_cmds.md) - the audited handlers.
- [state-and-persistence](state-and-persistence.md) - both logs as durable
  artifacts, retention, and backup.
- [security-model](security-model.md) - the admin boundary these records
  cover.
