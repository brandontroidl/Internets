# admin_cmds.py - admin and core command handlers (AdminCommandsMixin)

## Purpose

Holds every built-in (non-module) IRC command handler for the Internets bot: the
admin authentication flow, the help system, module management, IRC mode/raw/speech
commands, diagnostics, the shadow-ban family, logging controls, and shutdown. The
handlers are extracted into a mixin so `internets.py - IRCBot` stays focused on
connection, dispatch, and state (module docstring, `admin_cmds.py`). The file also
owns the `_CORE` dispatch table that `IRCBot._dispatch()` uses to route these
commands, and a set of module-level formatting/parsing helpers used by `.help`,
`.uptime`, `.stats`, `.audit`, and `.fingerprint`.

## Responsibilities / boundaries

Belongs here:

- All `cmd_*` coroutines with the uniform signature `(self, nick, reply_to, arg)`.
- The `_CORE` command -> handler-name table and `_CORE_PUBLIC` visibility split,
  plus `_core_admin_cmds()` which derives the admin help grid from them.
- The admin gate helper `_require_admin` and the audit wrapper `_audit` with its
  actor sanitization (`_clean_actor`).
- Pure module-level helpers: `_wrap_list`, `_help_grid`, `_humanize_delta`,
  `_read_rss_kb`, `_audit_parse`, `_audit_haystack`, `_audit_format`,
  `_state_file`, `_read_json_dict`, `_count_audit_mentions`.

Deliberately not here:

- Session and identity state semantics: `is_admin()` (hostmask re-check on every
  call), `_nick_hosts` maintenance, and admin-session revocation live in
  `internets.py - IRCBot`. The mixin only reads and writes the dicts under the
  host's locks.
- Password hashing/verification (`hashpw.py`), the tamper-evident audit store
  (`audit_log.py`), log filtering (`botlog.py`), config layering (`config.py`),
  and outbound redaction (`sender.py - redact_secrets`).
- PM-only enforcement for `auth`/`deauth` is in the dispatcher
  (`internets.py - IRCBot._dispatch()`), not in the handlers.
- Module command dispatch: `_dispatch()` checks `_CORE` first, then the
  module `_commands` registry; this file never routes module commands.
- The modules package. The file keeps its own `_CTRL_RE` regex rather than
  importing from `modules/` precisely to stay independent of that package
  (comment above `_CTRL_RE`).

## Dependencies and dependents

Internal imports:

| Import | Used for |
|---|---|
| `config.cfg / CONFIG_PATH / __version__ / CMD_PREFIX / MODULES_DIR` | version banner, usage strings, module discovery in `cmd_modules` |
| `botlog.log_filter / get_hash / apply_debug / apply_loglevel` | `.rehash` log-level reset, password hash retrieval, `.debug`, `.loglevel` |
| `hashpw.MAX_PASSWORD_BYTES / verify_password` | `.auth` length guard and verification |
| `sender.redact_secrets` | credential masking for `.raw` echoes/logs/audit |
| `audit_log.default as _audit` | the process-wide `AuditLog` singleton (note: the imported name `_audit` is the module-level factory; `AdminCommandsMixin._audit` is the method that calls it) |

External: stdlib only (`asyncio`, `re`, `time`, `logging`, plus deferred `json`,
`os`, `pathlib` inside helpers).

Dependents:

- `internets.py - IRCBot` subclasses the mixin (`class IRCBot(AdminCommandsMixin)`)
  and dispatches from `self._CORE` by inheritance (internets.py:650).
- `tests/test_admin_cmds.py` drives the real mixin against a `FakeBot`;
  `tests/test_help.py` guards the module-side half of the help surface.

Host contract - attributes the mixin declares for type checkers and that
`internets.py - IRCBot.__init__` actually provides:

| Attribute | Provided by IRCBot | Meaning |
|---|---|---|
| `_nick` | connection state | current bot nick |
| `_authed: dict[str, str]` | `__init__` | lowercased nick -> hostmask bound at auth time |
| `_auth_fails: dict[str, tuple[int, float]]` | `__init__` | lowercased nick -> (failure count, last attempt ts) |
| `_auth_lock` | `threading.Lock` guarding `_authed` AND `_nick_hosts` (internets.py:245) |
| `_mod_lock` | `threading.Lock` guarding `_modules` / `_commands` |
| `_nick_hosts: dict[str, str]` | protocol handlers | lowercased nick -> `user@host` |
| `_modules / _commands` | module loader | loaded module instances / module command registry |
| `_AUTH_CLEANUP_THRESHOLD = 50`, `_AUTH_MAX_FAILS = 5`, `_AUTH_LOCKOUT = 300` | class constants (internets.py:186-188) | auth brute-force knobs |

Methods the mixin declares as stubs (`preply`, `send`, `is_admin`,
`load_module`, `unload_module`, `reload_module`, `request_shutdown`) are the
host-side collaborators; the stub bodies (`...`) exist only so the mixin
type-checks standalone. Handlers additionally use host members not declared in
the stub block: `privmsg`, `active_channels`, `_store`, `_sender`, `cfg`,
`_shadow_bans`, `_shadow_ban_reasons`, `_save_shadow_bans`, `_restart_flag`,
and the `_stats_*` counters - all reached via `getattr` with defaults where
absence is tolerable (diagnostics), directly where it is not.

## Lifecycle

- Imported once at bot startup (`internets.py` imports the mixin at module load;
  importing `admin_cmds` transitively imports `config`, which parses CLI args and
  reads `config.ini` - the test file has to pin `sys.argv` around the import for
  exactly this reason - the module-level argv-pinning preamble in
  `tests/test_admin_cmds.py`).
- No instances of its own: the class is mixed into `IRCBot`, so its "constructor"
  is `IRCBot.__init__`.
- Handlers are invoked as asyncio tasks by `IRCBot._dispatch()` ->
  `IRCBot._run_cmd()`, each wrapped in `asyncio.wait_for` with the command
  timeout, so a wedged handler cannot pin a task slot forever.
- Nothing here has teardown; durable state (audit log, shadow-ban file) is owned
  by collaborators.

## State

Owned by this file: none at runtime beyond two module-level constants
(`_CTRL_RE`, `_MODULE_GROUPS`) and the class-level tables `_CORE` /
`_CORE_PUBLIC`. Everything else the handlers touch is host state:

- Read/write under `_auth_lock`: `_authed`, `_auth_fails`.
- Read under `_mod_lock`: `_modules` (snapshotted as a list, then used outside
  the lock).
- Read without a lock: `_nick_hosts` (see Concurrency), `_shadow_bans`,
  `_shadow_ban_reasons`, `_stats_*` counters, `_sender._q`.
- Write without a lock: `_shadow_bans` / `_shadow_ban_reasons` (event-loop-only
  mutation), `_restart_flag`.
- Persistent state touched: the audit log (`audit_log.AuditLog`, append-only
  JSON lines, HMAC-chained), `shadow_bans.json` (via
  `internets.py - IRCBot._save_shadow_bans()`, atomic 0600 write), and read-only
  views of module state files (`seen.json`, `tells.json`, `notes.json`) in
  `cmd_fingerprint`.

## Concurrency

All handlers run on the event loop as tasks; the locks exist because other
threads (module worker threads via `to_thread`, the audit log's own lock) and
concurrently scheduled command tasks touch the same dicts.

- `_auth_lock` sections are short and never span an `await`. `cmd_auth`
  deliberately snapshots the failure count, releases the lock, awaits
  `verify_password` in a thread, then RE-READS the count inside the lock before
  incrementing - a concurrent attempt during the (slow, by design) hash
  verification would otherwise be under-counted (comments at admin_cmds.py:227-229
  and 254-257).
- The failed-auth audit write is pushed off the loop with
  `asyncio.to_thread(self._audit, ...)` and executed OUTSIDE `_auth_lock`. Both
  properties are load-bearing: `is_admin()` takes `_auth_lock` on every inbound
  command, so holding it across a blocking disk write would stall dispatch for
  the whole channel under an auth flood; and `AuditLog.record()` is blocking
  file I/O. `tests/test_admin_cmds.py -
  TestAuthFailureAuditing.test_audit_write_never_happens_under_the_auth_lock`
  pins the lock-free property with a non-blocking acquire spy.
- Success-path and admin-action audits (`_audit` called directly from `cmd_load`,
  `cmd_say`, etc.) run synchronously on the loop. The implementation implies this
  asymmetry is a volume argument: those paths are admin-only and low-rate,
  whereas the failure path is unauthenticated-reachable and floodable.
- `cmd_shadow_ban` / `cmd_shadow_unban` mutate the ban set on the loop, then
  `await asyncio.to_thread(self._save_shadow_bans)` for the disk write. The
  worker thread iterates `self._shadow_bans` without a lock; see Findings.
- `cmd_help` / `cmd_modules` / `cmd_reloadall` / `cmd_stats` snapshot
  `_modules` under `_mod_lock` and then iterate the snapshot lock-free, so a
  concurrent load/unload cannot cause "dict changed size during iteration".
- `_nick_hosts` is read without the lock in `_audit`, `cmd_auth` (some reads),
  and `cmd_fingerprint`. CPython dict reads are atomic; the auth-critical
  comparison logic tolerates a missing/stale value by failing closed
  (`cmd_auth` refuses to bind, `is_admin` denies).
- `cmd_audit` and `cmd_fingerprint` perform synchronous file reads on the loop
  (see Findings); bounded by audit rotation at 5 MB
  (`audit_log._MAX_BYTES`) and by the dispatcher's command timeout.

## Failure behavior

- `_audit` (the method) catches ALL exceptions and degrades to a `log.warning` -
  an audit-store failure must never break the admin command it records
  (docstring, `AdminCommandsMixin._audit`). Note the inversion of the usual
  fail-closed rule: availability of admin control is preferred over guaranteed
  audit coverage; `audit_log.AuditLog.record()` itself raises on write failure,
  and this wrapper is where that is swallowed.
- `cmd_auth` splits verification errors in two: `ValueError` from
  `hashpw.verify_password` is a known configuration problem whose message is
  safe to log ("No password hash configured" / "Unrecognised hash format" /
  "bcrypt not installed") and is reported as "config error"; any other
  exception is treated as a FAILED ATTEMPT (counter incremented, "wrong
  password" reply) and only the exception TYPE is logged, because some backends
  echo partial input or hash fragments in exception text
  (admin_cmds.py:219-231, pinned by
  `TestAuth.test_backend_exception_counts_as_failure`).
- `cmd_rehash` aborts with a user-visible error if `config.reload_config()`
  raises; a syntactically valid config with a bad `password_hash` prefix aborts
  after log-level application (see Findings).
- `cmd_audit` reports `OSError` on log read by exception type only; garbage
  lines are dropped by `_audit_parse` returning `None`.
- `cmd_fingerprint` treats every data source as optional: missing files return
  `{}` (`_read_json_dict`), store errors are swallowed
  (`try/except` around channel scan), audit absence yields zero counts.
- `cmd_reloadall` partitions results into `OK:` / `FAILED:` per module instead
  of aborting on the first failure.
- `cmd_restart` / `cmd_shutdown` write their audit record BEFORE calling
  `request_shutdown` because the process may not get another chance to flush a
  write once shutdown begins (comments at admin_cmds.py:529-530 and 1013-1015).
  The restart itself is completed by `internets.py` main: `_restart_flag` is
  checked after loop teardown and triggers `execv` (internets.py:1471).

## Security

Trust boundaries and the controls at each:

- **Admin gate.** Every state-changing or information-rich handler starts with
  `_require_admin`, which delegates to `internets.py - IRCBot.is_admin()`: a
  session exists only if the nick is in `_authed` AND its current hostmask
  matches the one bound at auth time (fail-closed on unknown/changed hostmask).
  `tests/test_admin_cmds.py - TestAdminGate` parametrizes all 22 gated handlers
  and asserts no side effect leaks past the gate. Ungated by design: `cmd_help`,
  `cmd_version`, `cmd_modules`, `cmd_auth` (the `_CORE_PUBLIC` set) and
  `cmd_deauth` (only meaningful to an authed session; a non-authed caller gets
  "not authenticated").
- **Password never logged.** `cmd_auth` logs presence/length-class only, never
  the value: the too-long reply names the limit but not the actual length of the
  wire value beyond what the sender already knows; the success audit passes
  `args=None` ("Never pass the password (or any derivative) as args",
  admin_cmds.py:251); the failure audit carries only the counter
  (`{"fails": n, "max": 5}`) - `TestAuthFailureAuditing.
  test_failure_record_carries_no_password_length_oracle` proves two wrong
  passwords of lengths 8 and 100 produce records identical apart from the
  counter. Backend exceptions are logged as type name only (see Failure
  behavior). One layer down, `hashpw._verify_bcrypt` also refuses to log the
  candidate length on its own refusal path for the same oracle reason.
- **Audit-record injection (`_clean_actor`).** `_CTRL_RE` strips C0/C1 control
  bytes (`\x00-\x1f`, `\x7f-\x9f`) and truncates to 64 chars before the actor
  and hostmask reach `AuditLog.record()`. The attack this prevents: `.audit`
  renders each durable record into a single IRC line via `_audit_format` as
  `ts  <bold>actor</bold>  action  args`, with fields separated by formatting
  and spacing. Failed-auth records made the recording path reachable by
  UNAUTHENTICATED users, so the actor string (an IRC nick, which on lax
  networks can carry bytes like `\x02` bold or `\x0f` reset) became
  attacker-influenced. A control byte embedded in a nick could terminate the
  bold span early and fabricate what reads as an extra column, letting a
  `.audit` reader attribute an action to the wrong nick - classic log forging,
  except the log here is tamper-EVIDENT (HMAC chain), not tamper-PROOF against
  garbage fed in at write time. Stripping at the producer keeps the rendered
  line's column structure honest. Pinned by `TestAuthFailureAuditing.
  test_actor_control_characters_are_stripped_before_recording`.
- **Credential redaction on `.raw`.** `.raw` is the one command whose argument
  is legitimately a credential-bearing protocol line (`OPER name pw`,
  `PRIVMSG NickServ :IDENTIFY pw`). The wire gets the full line; the echo, the
  bot log, and the audit record all get `sender.redact_secrets(line)`, which
  masks everything after the first credential verb (`AUTHENTICATE`, `IDENTIFY`,
  `REGISTER`, `IDENT`, `OPER`, `PASS`, `AUTH`), longest verb first.
- **Protocol injection on `.raw`.** CR/LF/NUL are rejected (no smuggling a
  second IRC command), and the line is capped at 510 bytes
  (`TestModes.test_raw_rejects_crlf`, `test_raw_rejects_oversize`).
- **Input validation elsewhere.** `cmd_mode`/`cmd_snomask` allowlist-match
  their argument (`^[a-zA-Z+\- ]+$` / `^[a-zA-Z+\-]+$`) so mode strings cannot
  carry arbitrary protocol bytes; `cmd_nick` enforces an RFC 2812 nick grammar;
  `cmd_say`/`cmd_act` reject targets containing `,` or space (a comma would
  multicast the PRIVMSG to several targets). `cmd_rehash` validates the
  reloaded hash by prefix allowlist (`scrypt`/`bcrypt`/`argon2`) and
  deliberately reports only the prefix LENGTH on failure so a mis-pasted secret
  in the `password_hash` field is not echoed back into the log
  (admin_cmds.py:561-568).
- **Privacy in `cmd_fingerprint`.** Notes are reported as a count, never
  content (comment at admin_cmds.py:900).
- **Reply channel.** `preply` (host-side) sends a NOTICE to the invoking nick
  when the command came from a channel, so admin traffic and auth prompts do
  not spam the channel.

## Classes

### AdminCommandsMixin

- **Responsibility:** namespace for all core command handlers plus the dispatch
  table they are routed from. No state of its own; every attribute is inherited
  from the host.
- **Lifecycle:** mixed into `IRCBot` as a base class; never instantiated
  directly (tests build a `FakeBot(AdminCommandsMixin)` to run the real
  handler code against stub collaborators).
- **Important class attributes:**
  - `_CORE: dict[str, str]` - command word -> handler method name. Lives here
    (with the handlers) rather than on `IRCBot` so the `.help` branches can
    derive their listings from the same table the dispatcher uses; the previous
    hand-copied help grid drifted from the dispatch table (comment at
    admin_cmds.py:78-80). `shutdown` and `die` both map to `cmd_shutdown` -
    the only alias pair.
  - `_CORE_PUBLIC: frozenset[str]` = `{"help", "modules", "version", "auth"}` -
    usable and LISTED without auth. Everything else is admin-only in the help
    surface. `deauth` intentionally stays on the admin side of the help split.
- **Invariants:** every `_CORE` handler must have a docstring (its first line
  IS the `.help <cmd>` text) - enforced by `TestCoreCommandHelp.
  test_every_core_handler_has_docstring`. The admin help grid must equal the
  derived primary set - enforced by `test_help_admin_grid_matches_core`, so a
  newly added core command cannot silently skip help (test class added
  2026-08-15).
- **Extension constraints:** a new core command needs exactly two things - a
  `cmd_*` coroutine with a docstring and a `_CORE` entry; help, the admin grid,
  alias collapsing, and dispatch all derive from the table.

## Functions and methods

### Dispatch-table machinery

- **`_core_admin_cmds()` (classmethod)** - returns the admin core commands,
  one per handler method, sorted. Iterates `_CORE`, skips `_CORE_PUBLIC`
  entries, and uses `dict.setdefault(method, cmd)` so the FIRST command listed
  for a method becomes the primary and later aliases collapse (`shutdown` wins
  over `die` because it appears first; `test_help_admin_collapses_aliases`
  asserts `SHUTDOWN` present, `DIE` absent). Callers: `.help admin`,
  `.help all`.

### Helpers (methods)

- **`_require_admin(nick, reply_to) -> bool`** - the gate. On failure replies
  `auth first - /MSG <botnick> AUTH <pw>` and returns False; caller returns
  immediately. Pure convenience over `is_admin`; holds no state.
- **`_audit(nick, action, args=None)`** - resolves the actor's hostmask from
  `_nick_hosts` (empty string if untracked), sanitizes both actor and hostmask
  with `_clean_actor`, and appends via `audit_log.default().record()`. Catches
  every exception (see Failure behavior). Callers: every state-changing
  handler; the auth failure path calls it via `to_thread`.
- **`_split_target_and_text(arg, reply_to)`** - parses `[target] <text>` for
  `.say`/`.act`. The first token counts as a target if it starts with a channel
  sigil (`#&+!`) or matches the nick grammar AND more text follows; a single
  bare word falls back to `(reply_to, whole arg)` so `.say hi` speaks in the
  invoking channel (`TestSplitTargetAndText.
  test_single_word_falls_back_to_reply_to`).

### The auth path (deepest treatment)

**`cmd_auth(nick, reply_to, arg)`** - PM-only (enforced upstream in
`_dispatch`), five phases:

1. **Preconditions.** No configured hash -> "run hashpw.py" and return (auth is
   disabled, not broken, on a fresh install - `botlog._validate_hash` allows an
   empty hash at startup for the same reason). No argument -> usage. Argument
   longer than `hashpw.MAX_PASSWORD_BYTES` (128, measured in UTF-8 BYTES) ->
   explicit too-long reply. The limit is imported from `hashpw` so creation
   and verification can never disagree again: they previously disagreed 8x
   (hashpw accepted 1024 chars, auth rejected >128), letting an operator create
   a password that hashed cleanly and could then never authenticate, with no
   error naming length as the cause (comment at admin_cmds.py:172-177,
   `hashpw.check_password` docstring).

2. **Lockout gate (inside `_auth_lock`).** Housekeeping first: once
   `_auth_fails` exceeds `_AUTH_CLEANUP_THRESHOLD` (50 on the real bot), stale
   entries older than the lockout window are pruned, bounding the dict against
   a many-nicks flood (`TestAuth.test_cleanup_threshold_prunes_stale`). Then
   the gate: the caller's `(fails, last_t)` is read; if the last attempt is
   older than `_AUTH_LOCKOUT` (300 s) the count is treated as zero FOR THE
   GATE. If `fails >= _AUTH_MAX_FAILS` (5), the reply names the remaining
   seconds, `verify_password` is never invoked
   (`test_lockout_after_max_fails` asserts the verifier is not called), and -
   critically - the timestamp is REFRESHED to `now`. That makes the window
   sliding: an attacker who trickles one attempt per window would otherwise
   get a free verification every 300 s; with the refresh, any attempt while
   locked restarts the clock (comment at admin_cmds.py:197-199). The lockout
   branch also deliberately audits nothing (see phase 5's cap).

   An emergent consequence, pinned as intended by
   `test_lockout_window_expiry_unblocks_gate`: window expiry resets the count
   only for the GATE check, never in the stored dict, and the failure
   increment re-reads the STORED value. So after a lockout expires, one more
   wrong password takes the counter from 5 to 6 and the nick is immediately
   locked again - post-lockout, an attacker gets exactly one verification per
   window until a success (which pops the entry entirely).

3. **Verification (off-loop, lock released).** `pre_hostmask` is snapshotted,
   then `verify_password(arg.strip(), h)` runs in `asyncio.to_thread` -
   scrypt/bcrypt/argon2 are deliberately slow, and running them on the loop
   would freeze the bot for every user per attempt. Exception split per
   Failure behavior: `ValueError` = config error (message loggable, no
   counter); anything else = counted failure with type-only logging.

4. **Success binding (TOCTOU checks, then `_auth_lock`).** The CURRENT
   hostmask is re-read; a missing or `"unknown"` value refuses to bind
   ("can't confirm your hostmask right now") and a hostmask that CHANGED
   across the verify await refuses too ("identity changed during auth") -
   both fail closed rather than create a nick-only session that a nick-grabber
   could inherit (`test_success_refused_without_hostmask`,
   `test_success_refused_with_unknown_sentinel`; the matching deny-side lives
   in `internets.py - IRCBot.is_admin()`, which re-checks the binding on every
   call and revokes on mismatch). On success the failure entry is popped, the
   binding stored, and the audit record written with `args=None` - never the
   password or any derivative.

5. **Failure accounting and auditing.** Inside the lock, the count is re-read
   (a concurrent attempt during the await could have bumped it; the snapshot
   would under-count) and incremented. The reply is a uniform "wrong
   password." Then the audit write, whose three properties are each
   load-bearing (comment block at admin_cmds.py:264-280):

   - **Outside `_auth_lock`** - `is_admin` takes that same lock on every
     inbound command; holding it across a blocking disk write would stall the
     event loop for the whole channel under an auth flood.
   - **Off the loop via `to_thread`** - `AuditLog.record()` is blocking file
     I/O (open-append, write, chmod) serialized on the audit log's own
     internal lock.
   - **Capped per window** - recording stops once the nick is locked out,
     because locked-out attempts return from the phase-2 branch which audits
     nothing. A sustained flood therefore cannot churn the 5 MB audit log
     through its rotation and destroy older forensic history: at most
     `_AUTH_MAX_FAILS` `auth_failed` records plus one `auth_lockout` record
     (written once, at the transition) per nick per window.
     `test_lockout_is_recorded_once_at_the_transition_not_per_attempt` drives
     20 attempts and asserts exactly 1 lockout record and <= 5 failure
     records.

   The record's `args` carries `{"fails": n, "max": 5}` only - never the
   password, its length, or any derivative, because the record is durable and
   readable via `.audit`
   (`test_failure_record_carries_no_password_length_oracle`).

**`cmd_deauth`** - pops the caller from `_authed` under the lock; audits only
when a session actually ended. No admin gate (see Security).

### Help system

**`cmd_help(nick, reply_to, arg)`** - single handler, five lookup branches.
It first snapshots modules under `_mod_lock` and partitions them into
`configured` vs `hidden` (has `COMMANDS` but `is_configured()` is False -
typically missing API key); hidden modules are visible to admins only,
everywhere. A leading command prefix on the argument is stripped
(`test_help_prefix_stripped`).

Lookup ordering for `.help <target>` - each branch returns on match:

1. **`all`** - full alphabetical uppercase grid: `_CORE_PUBLIC`, plus (admin
   only) `_core_admin_cmds()`, plus one primary command per module handler
   method (aliases collapse via the same first-wins `setdefault` idiom),
   skipping unconfigured modules for non-admins. Admins get a trailing
   `(hidden, no key: ...)` note.
2. **`admin`** - admin grid from `_core_admin_cmds()`; non-admins get the
   generic "no command 'admin' loaded" (indistinguishable from a nonexistent
   command, so the branch does not leak the admin surface).
3. **Module name** - exact match against a loaded module's name shows the
   whole roster: a `[name] N commands` header plus the module's
   `help_lines(prefix)`. Unconfigured module + non-admin `break`s out and
   falls through to the later branches.
4. **Core command** - if the target is in `_CORE` and visible to the caller
   (admin, or in `_CORE_PUBLIC`), reply with the handler docstring's FIRST
   LINE plus all aliases joined as `.die/.shutdown - ...`. Admin core
   commands stay invisible to non-admins, falling through to the generic
   not-found reply (`test_help_named_core_admin_command_hidden_from_non_admin`).
5. **Module command** - scan each visible module's `COMMANDS`
   (case-insensitive); on a hit, show only the help lines whose first token
   is `.cmd`, `.cmd/...`, or `.cmd ...`, falling back to the module's full
   help if no line matches.
6. **Not found** - `no command '<target>' loaded - try .help`.

Why module names outrank core names (branch 3 before branch 4): the comment at
admin_cmds.py:364-368 gives the rationale for the analogous module-vs-command
collision - a target like `weather` matches both the module name and the
`.weather` command, and showing the whole module roster is more useful than
collapsing to one line; a user wanting a single command can use an alias or a
more specific name. The same ordering means a module NAMED like a core command
(e.g. a hypothetical `stats` module) keeps winning the lookup -
`test_help_named_core_prefers_module_name_match` pins exactly this with a fake
`stats` module. Note the ordering governs only `.help`; actual DISPATCH is the
opposite (`_dispatch` checks `_CORE` before module commands), so a module
command shadowed by a core name is unreachable regardless of help.

No-argument `.help` is the compact progressive-disclosure index: module names
grouped by `_MODULE_GROUPS` category, wrapped by `_wrap_list`; any loaded
module not listed in a group falls into a trailing `More:` row, so new modules
appear without editing the table (comment above `_MODULE_GROUPS`). Admins
additionally see hidden modules inline plus the
`(admin) .help admin ... .help all ...` footer with the hidden count;
non-admins see neither (`test_default_non_admin_hides_admin_note`).

**`cmd_version`** - one-liner: version + repo URL. Public.

**`cmd_modules`** - lists loaded modules with per-module command counts
(aliases NOT collapsed here - `len(COMMANDS)` counts keys, so
`weather (2)` for `weather`/`w` - `TestModules.test_lists_loaded`), then
"Available" modules discovered by globbing `MODULES_DIR` minus the
infrastructure stems (`__init__`, `base`, `geocode`, `units`) and the loaded
set. Public.

### Module management

`cmd_load` / `cmd_unload` / `cmd_reload` share one shape: admin gate, usage on
missing arg, lowercase the name, delegate to the host
(`load_module`/`unload_module`/`reload_module`, each returning
`(ok, message)`), relay the message, audit with the module name.
`cmd_reloadall` snapshots names under `_mod_lock`, reloads serially, and
reports `OK:` / `FAILED:` partitions (`test_reloadall_reports_ok_and_fail`).

`cmd_restart` sets `_restart_flag` (consumed after loop teardown for `execv`,
internets.py:1471) and calls `request_shutdown`; `cmd_rehash` re-reads BOTH
config layers via `config.reload_config()` (re-reading `config.ini` alone
would clobber the local overlay's `password_hash` with the template's empty
placeholder - comment at admin_cmds.py:539-541 and `config.reload_config`
docstring), re-applies the base log level and clears debug overrides,
validates the reloaded hash prefix (allowlist, length-only error), clears ALL
admin sessions (a rehash may have rotated the password), and audits. Both
audit before initiating shutdown (see Failure behavior).

### IRC oper / speech / nick

- **`cmd_mode`** - validates against `^[a-zA-Z+\- ]+$` and sends
  `MODE <botnick> <modes>` - always self-targeted; the regex cannot even
  express a channel name (see Findings re: the docstring).
- **`cmd_snomask`** - validates `^[a-zA-Z+\-]+$`, sends
  `MODE <botnick> +s <mask>`.
- **`cmd_raw`** - CR/LF/NUL rejection, 510-byte cap, full line to the wire,
  `redact_secrets` copy to echo/log/audit (see Security).
- **`cmd_say` / `cmd_act`** - `_split_target_and_text`, reject `,`/space
  targets, `privmsg` (`.act` wraps in `\x01ACTION ...\x01` CTCP framing),
  audit `{target, text}` - note the spoken text intentionally lands in the
  audit log; impersonation-by-bot is exactly what `.audit` should attribute.
- **`cmd_nick`** - RFC 2812 nick grammar (first char letter/special, then up
  to 29 of letter/digit/special/hyphen), no-op if already current, sends
  `NICK` and waits: `_nick` is updated only when the server confirms via the
  NICK echo, avoiding divergence if the server rejects with 432/433/437
  (comment at admin_cmds.py:699-701). Audits `{from, to}`.

### Diagnostics

- **`cmd_uptime`** - process age from `_stats_boot_ts` and connection age from
  `_stats_connect_ts` (None -> "not connected"), both via `getattr` defaults
  so a partially initialized host cannot crash it. Admin-gated.
- **`cmd_stats`** - one screen: uptimes, configured/loaded module counts
  (under `_mod_lock`), channel count, traffic counters, sender queue depth
  (reaches into `sender._q.qsize()`, documented as approximate-but-adequate;
  a private-attribute reach-in tolerated by `try/except`), audit record count
  (`AuditLog.count()`), and RSS from `/proc/self/status`
  (`_read_rss_kb`, "n/a" off-Linux).
- **`cmd_audit`** - views the audit log. Argument grammar:
  nothing = last 10; `N` = last N clamped to [1, 200]; `tail` = last 5;
  `grep <pattern>` = case-insensitive substring over the flattened record
  (`_audit_haystack`), showing the last 50 matches; `verify` = re-walk the
  HMAC chain via `AuditLog.verify()`, reporting intact-with-count or the
  zero-based index of the first broken record
  (`TestAudit.test_verify_broken` corrupts a record and asserts the BROKEN
  reply). Reads the whole file into memory - acceptable because rotation
  bounds it at 5 MB (comment at admin_cmds.py:813).
- **`cmd_fingerprint`** - cross-references one nick across every store the bot
  has: current hostmask (`_nick_hosts`), channel membership
  (`_store.channel_users` per active channel), shadow-ban status and reason,
  last-seen (reads the seen module's JSON state file directly, path resolved
  via `_state_file` from the module's own config section), pending tells
  to/from the nick, note COUNT only, and audit mentions
  (`_count_audit_mentions`). Every source degrades independently
  (`TestFingerprint.test_unknown_target_minimal` vs `test_full_crossref`).
  Reads module state files directly rather than through the owning modules -
  works whether or not those modules are loaded, at the cost of duplicating
  their path-resolution convention.

### Shadow-ban family

Shadow-bans silently drop ALL traffic from a nick at the top of
`IRCBot._dispatch()` (and raw-line delivery, internets.py:875) - no reply, no
rate-limit consumption, no audit entry per dropped command; the banned nick
cannot distinguish being ignored from the bot being offline (comment at
internets.py:620-623).

- **`cmd_shadow_ban`** - refuses to ban the bot itself or the invoking admin,
  refuses if the store attribute is missing (fail-closed rather than
  `AttributeError`), dedupes, adds to the in-memory set + optional reason,
  persists via `to_thread(self._save_shadow_bans)` (atomic 0600 JSON write on
  the host), audits `{nick, reason}`.
- **`cmd_shadow_unban`** - discards from set and reasons, persists, audits the
  nick.
- **`cmd_shadow_list`** - read-only listing with reasons; no audit (consistent
  with the read-only-commands-do-not-audit policy throughout the file).

### Logging controls

- **`cmd_loglevel`** - delegates to `botlog.apply_loglevel` with `preply` as
  the reply sink: no args = read-only listing (prefixed by a "Log levels:"
  header, not audited); one arg = base level; two args = per-subsystem level.
  Audits only when arguments were given AND no error returned.
- **`cmd_debug`** - delegates to `botlog.apply_debug` (`on`/`off`/subsystem
  toggles); always audits, defaulting the recorded arg to `"on"`.

### Shutdown

**`cmd_shutdown`** (alias `die`) - optional reason, audit BEFORE
`request_shutdown` (flush hazard, see Failure behavior); the audited args are
the operator-supplied reason or None, deliberately not the default placeholder.

### Module-level helpers

| Helper | Behavior (evidence) |
|---|---|
| `_clean_actor(s, max_len=64)` | `_CTRL_RE.sub("", str(s))[:max_len]` - control-byte strip + length bound for audit actor/hostmask (see Security) |
| `_wrap_list(items, lead, width=74)` | space-separated list with hanging indent aligned under the lead; wraps when the next item would exceed the width; empty list returns the bare lead (`TestWrapList`) |
| `_help_grid(items, cols=4, col_w=14)` | uppercase /HELP-style grid, left-to-right top-to-bottom, last column unpadded to save bytes (`TestHelpGrid.test_uppercases_and_pads`) |
| `_humanize_delta(seconds)` | clamps negatives to 0; `45s` / `1m 30s` / `1h 1m` / `1d 1h` tiers (`TestHumanizeDelta`) |
| `_read_rss_kb()` | parses `VmRSS:` from `/proc/self/status`; None off-POSIX or on any error |
| `_audit_parse(line)` | `json.loads`, dict-or-None - non-dict JSON and garbage both yield None (`TestAuditParse`) |
| `_audit_haystack(e)` | joins ts/actor/host/action plus JSON-serialized args into one lowercase-searchable string for `.audit grep` |
| `_audit_format(e)` | one IRC line: ISO ts truncated to seconds with `T` -> space, bold actor, action, args JSON-compacted and truncated at 160 chars with `...` (`TestAuditFormat`) |
| `_state_file(cfg, section, default)` | module state-file path from `cfg[section]["file"]`, else the default; tolerates configparser errors |
| `_read_json_dict(path)` | top-level JSON dict or `{}` on missing/garbage/non-dict (`TestReadJsonDict`) |
| `_count_audit_mentions(target)` | walks the audit log counting exact actor matches and case-insensitive substring hits in args (`TestCountAuditMentions`) |

## Implementation walk

- **Lines 1-37 (imports, `_CTRL_RE`, `_clean_actor`):** security enforcement +
  initialization. `_CTRL_RE` is local rather than imported from `modules/` to
  keep this file independent of that package.
- **Lines 44-57 (`_MODULE_GROUPS`):** formatting data for the compact `.help`
  index; unlisted modules fall into "More" by construction, so the table can
  lag reality without hiding anything.
- **Lines 60-100 (class header, host-attribute declarations, `_CORE`,
  `_CORE_PUBLIC`):** the typed attribute block is documentation-for-checkers
  only; the tables are the single source of truth for core dispatch AND help.
- **Lines 102-118 (`_core_admin_cmds`, method stubs):** derivation + interface
  stubs (see Classes / Dependencies).
- **Lines 122-147 (`_require_admin`, `_audit`):** security enforcement and
  error containment.
- **Lines 151-301 (auth/deauth):** the file's security core; walked in full
  above.
- **Lines 305-474 (help/version/modules):** protocol formatting +
  progressive disclosure; walked above.
- **Lines 478-577 (module management + restart/rehash):** thin delegations
  with audit; rehash's config-layering and hash-validation subtleties above.
- **Lines 581-630 (mode/snomask/raw):** allowlist validation then wire send;
  `.raw`'s redaction split above.
- **Lines 634-704 (say/act/nick):** target parsing, CTCP framing,
  server-confirmed nick change.
- **Lines 708-836 (uptime/stats/audit):** diagnostics; defensive `getattr`
  everywhere so a half-built host (or FakeBot) cannot crash them.
- **Lines 840-976 (fingerprint, shadow-ban family):** cross-store read-only
  aggregation; moderation set + persistence.
- **Lines 980-1017 (loglevel/debug/shutdown):** delegation to botlog;
  audit-before-shutdown ordering.
- **Lines 1022-1195 (module-level helpers):** pure formatting/parsing; table
  above. The triple blank line at 1064-1067 is cosmetic only. Helpers import
  `json`/`os`/`pathlib` inside function bodies - defers import cost, at the
  price of repetition (`import json as _json` appears in five helpers);
  consistent with the file's stdlib-only, dependency-light posture rather than
  a defect.

## Findings

- **doc-drift | admin_cmds.py - cmd_mode | Docstring (which `.help mode`
  displays) claims "Set a user or channel mode. Usage: .mode <target>
  <modes>", but the implementation always sends `MODE <botnick> ...` and the
  `^[a-zA-Z+\- ]+$` allowlist cannot express a channel name or ban mask; the
  in-band usage string (`usage: .mode <+/-modes>`) matches the code, the
  docstring does not.**
- **doc-drift | admin_cmds.py - cmd_auth (comment, failure-audit block) | The
  comment asserts "record() writes and fsyncs", but
  `audit_log.AuditLog.record()` performs open-append/write/chmod with no
  fsync; the off-loop rationale still holds (it is blocking I/O), the fsync
  claim is inaccurate.**
- **questionable | admin_cmds.py - cmd_shadow_ban / cmd_shadow_unban |
  `asyncio.to_thread(self._save_shadow_bans)` has the worker thread iterate
  `self._shadow_bans` (`sorted(...)` in `internets.py -
  IRCBot._save_shadow_bans`) with no lock while a concurrently scheduled
  shadow-ban task can mutate the set on the event loop; a mid-iteration
  mutation raises inside the save, which the host's broad `except` degrades to
  a warning - the persist is then silently skipped for that call.**
- **questionable | admin_cmds.py - cmd_rehash | The bad-hash-prefix abort
  returns AFTER the new log level has been applied but BEFORE sessions are
  cleared and before `_audit(nick, "rehash", ...)`, so a partially applied
  rehash (log level changed, sessions kept, bad hash now live for the next
  auth) leaves no audit record.**
- **questionable | admin_cmds.py - cmd_audit / _count_audit_mentions |
  Synchronous reads of the audit log (up to the 5 MB rotation bound) run on
  the event loop, in a file whose auth path offloads its audit WRITE to a
  thread for exactly the loop-stall reason; bounded and admin-only, but
  inconsistent.**
- **test-gap | admin_cmds.py - _clean_actor | The control-byte strip is
  pinned by test, the 64-char truncation is not; an over-long hostmask
  silently truncating in audit records is unverified behavior.**
- **test-gap | admin_cmds.py - cmd_help (module-command branch) | The
  fallback that dumps a module's FULL help when no line prefix-matches the
  requested command (`if not matched: matched = hl`) has no test; nor does
  the unconfigured-module `break` in the module-name branch that falls
  through to the command branches for non-admins.**
