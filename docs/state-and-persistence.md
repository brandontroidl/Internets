# State and persistence

Every byte the bot writes to disk, who owns it, when it is written, how it
survives a crash, and what it means for privacy and backup.

The bot has no database. All durable state is plain files in the process
working directory, written by five independent owners that share a common
technique (temp file, tighten permissions, atomic rename) but not a common
implementation. Understanding which owner writes which file is the whole
model, because each owner has its own flush cadence, its own locking, and its
own corruption behaviour.

For line-level detail follow the links into
[internals](internals/store.md); this page stays at the inventory and
lifecycle level. Operational quick reference (backup checklist, upgrade
procedure) lives in [deployment](deployment.md).

## Where files live

Every default path is relative, and every relative path resolves against the
**current working directory at process start**, not against the installation
directory:

- `store.py` receives its three paths from `internets.py` as plain strings
  taken from `[bot]` config keys.
- `audit_log.py - default()` resolves `./audit.log` on the first call.
- `secret_store.py` resolves `SECRETS_FILE = Path("config.ini").resolve()`
  once at import.
- `process_lock.py` absolutizes its path at `acquire()` time, deliberately
  not at import.

Consequence: launching the bot from a different directory silently creates a
second, empty set of state files rather than failing. Run it from its
deployment root, or set absolute paths in `config.ini`. See
[deployment](deployment.md) for the service unit that pins this.

## Complete artifact inventory

### Files the bot process writes

| Artifact (default) | Owner | Config key | Contents |
| --- | --- | --- | --- |
| `locations.json` | `store.py - Store` | `[bot] locations_file` | nick to saved weather location |
| `channels.json` | `store.py - Store` | `[bot] channels_file` | channel rejoin list |
| `users.json` | `store.py - Store` | `[bot] users_file` | per-channel user tracking (PII) |
| `shadow_bans.json` | `internets.py - IRCBot` | `[bot] shadow_bans_file` | shadow-banned nicks plus reasons |
| `seen.json` | `modules/seen.py` | `[seen] file` | last event per nick (PII) |
| `tells.json` | `modules/tell.py` | `[tell] file` | queued offline messages |
| `notes.json` | `modules/notes.py` | `[notes] file` | per-nick personal notes |
| `reminders.json` | `modules/remind.py` | `[remind] file` | scheduled reminders |
| `steamids.json` | `modules/steam.py` | `[steam] steamids_file` | nick to SteamID64 registry |
| `audit.log` | `audit_log.py` | (fixed `./audit.log`) | privileged-action records (PII) |
| `audit.log.key` | `audit_log.py` | (derived) | 32-byte HMAC key, hex |
| `internets.pid` | `process_lock.py` | (fixed, set by `_entry()`) | `pid\|start_time\|hostname` |
| `internets.log` | `botlog.py` | `[logging] log_file` | application log |

Only `[seen]` and `[steam]` appear in `config.ini.example`; the `[tell]`,
`[notes]`, `[remind]`, and `[bot] shadow_bans_file` keys are read by the code
but not templated, so those four files land on their built-in defaults unless
an operator adds the sections by hand.

### Side files created as a byproduct

| Artifact | Created by | When |
| --- | --- | --- |
| `<name>.json.bak` | `store.py - Store._write()` | before every successful store write |
| `<name>.json.corrupt.<unixts>` | `store.py - Store._quarantine()` | a state file failed validation at load |
| `*.tmp` / `.<name>.*.json.tmp` | every atomic writer | transient, unlinked on failure |
| `audit.log.<UTC stamp>` | `audit_log.py - _rotate_if_oversize()` | live log exceeded 5 MiB |
| `audit.log.key.bad` | `audit_log.py - _load_key()` | an existing key was malformed or short |
| `internets.log.1` .. `.N` | `logging.handlers.RotatingFileHandler` | main log exceeded `max_bytes` |

### Files the bot process reads but never writes

`config.ini` and the optional `config.local.ini` overlay are read at startup
and re-read on `.rehash`. The only writer is the operator CLI
(`python -m secret_store set|delete|init|migrate`), which edits the
`[secrets]` section with a comment-preserving line rewrite plus an atomic
0600 replace. The bot process itself never writes a credential to disk. See
[security-model](security-model.md) and
[internals/secret_store](internals/secret_store.md).

## Two persistence patterns

### Pattern A: the integrity envelope (`store.py` only)

The three core datasets are wrapped before writing:

```json
{
  "schema": 2,
  "checksum": "<sha256 hex of canonical JSON of data>",
  "data": { }
}
```

The checksum is SHA-256 over canonical JSON (`sort_keys=True`, compact
separators, `ensure_ascii=False`), so it is insertion-order independent. It
detects corruption and casual tampering; it is not authentication, since
anyone who can write the file can recompute it, and the code does not claim
otherwise.

A legacy v1 file (bare payload, no `schema` key) is accepted on read without
verification and is rewritten as v2 on the next flush. That read-side shim is
the repo's additive-migration policy in practice: the runtime reads only the
current canonical shape plus one explicitly handled legacy shape.

### Pattern B: bare JSON with an atomic write (everything else)

`shadow_bans.json`, `seen.json`, `tells.json`, `notes.json`,
`reminders.json`, and `steamids.json` are written as plain JSON with no
envelope and no checksum. Each writer implements the same sequence inline:
`tempfile.mkstemp` in the target directory, write, `chmod 0o600`,
`os.replace`, unlink the temp file on any error. Corruption in these files is
detected only as a parse failure at load, and the response is always "log a
warning and start empty" - there is no quarantine, so the next successful
flush overwrites the damaged file.

That asymmetry is deliberate for the core datasets (losing opt-out flags and
the rejoin list silently is the failure quarantine exists to prevent) but it
means module stores are strictly less recoverable than store-owned ones.

```{graphviz}
digraph atomic_write {
  rankdir=LR;
  node [shape=box, fontsize=10, fontname="Helvetica"];
  mk   [label="mkstemp\n(same dir)"];
  wr   [label="write JSON"];
  ch   [label="chmod 0600"];
  bak  [label="copy target\nto .bak", style=dashed];
  rep  [label="os.replace\n(atomic)"];
  fail [label="unlink temp\nkeep target", shape=box, style=filled,
        fillcolor="#eeeeee"];
  mk -> wr -> ch -> bak -> rep;
  wr -> fail [style=dashed, label="error"];
  ch -> fail [style=dashed];
}
```

The `.bak` step (dashed) exists only in `store.py - Store._write()`. No
writer calls `fsync` before the rename, so the atomic replace guarantees a
reader never sees a torn file but does not guarantee the newest version
survives a power loss.

## Write timing, flush, and locking

| Artifact | Write trigger | Lock held |
| --- | --- | --- |
| `locations.json` | dirty flag, flush thread every 30 s | `_loc_lock` |
| `channels.json` | dirty flag, same thread; forced first in shutdown | `_chan_lock` |
| `users.json` | dirty flag, same thread; prunes before writing | `_user_lock` |
| `shadow_bans.json` | `.shadow-ban` / `.shadow-unban`, via `to_thread` | none |
| `seen.json` | dirty flag, asyncio task every 60 s, and `on_unload` | `SeenModule._lock` |
| `tells.json` | every mutating command and every delivery | `TellModule._lock` |
| `notes.json` | every mutating subcommand, via `to_thread` | `NotesModule._lock` |
| `reminders.json` | set, cancel, fire, and `forget` | `RemindModule._lock` |
| `steamids.json` | `.regsteam` and `forget`, via `to_thread` | `SteamModule._lock` |
| `audit.log` | synchronously on every privileged action | `AuditLog._lock` |
| `internets.pid` | once at `acquire()`; removed at `release()` | none |
| `internets.log` | continuously, per log record | stdlib handler lock |

`store.py` runs one daemon thread named `store-flush`; it wakes every
`_FLUSH_INTERVAL` (30 s) and writes only datasets whose dirty flag is set.
Each dataset has its own `threading.Lock`, and no method takes two, so lock
ordering cannot deadlock. The disk write happens while holding the dataset
lock, so a slow disk briefly blocks mutators of that one dataset only.

Crash exposure is bounded by the cadence: up to 30 s of store mutations, up
to 60 s of seen-tracking, and zero for the command-triggered writers, which
persist before returning to the caller.

### Shutdown ordering

`internets.py - IRCBot.graceful_shutdown()` persists in a deliberate order:
channels first (before anything else can fail), then module unload (which
gives `seen`, `tell`, `remind`, and `notes` their `on_unload` flush), then
`Store.stop()` (stop event plus one final synchronous flush), then the
socket, tasks, metrics server, and log handlers. The flush thread is a daemon
and is never joined; it exits on its next wakeup or dies with the process.

### Cross-process locking

There is none, anywhere. No writer uses `fcntl` or `flock`. Two bot
processes sharing a directory would interleave audit records and clobber each
other's store flushes. The entire safety argument rests on
`process_lock.py` making a second instance impossible: an `O_CREAT | O_EXCL`
lockfile carrying `pid|start_time|hostname`, with liveness probing so a
`kill -9` does not permanently wedge startup. See
[internals/process_lock](internals/process_lock.md).

> **Known defect** (`process_lock.py - ProcessLock.acquire()`, ledger entry
> "process_lock stale-reclaim not atomic"): the stale-lock path is
> read, unlink, then exclusive create. Two processes starting simultaneously
> over the same stale lockfile can interleave so that the second unlinks the
> first's freshly created file and both acquire. The window is microseconds
> and is only reachable after a crash left a stale file, but the outcome is
> exactly the dual-run the lock exists to prevent, and the cross-process
> safety of every writer above depends on it.

## Startup restoration

| Artifact | Restored by | On failure |
| --- | --- | --- |
| `locations.json` | `Store.__init__` | quarantine, start empty |
| `channels.json` | `Store.__init__`, replayed by `_deferred_rejoin()` | quarantine, no rejoin |
| `users.json` | `Store.__init__` | quarantine, start empty |
| `shadow_bans.json` | `IRCBot._load_shadow_bans()` | warn, empty ban set |
| `seen.json` | `SeenModule.on_load()` | warn, start empty |
| `tells.json` | `TellModule` load, expiring as it reads | warn, start empty |
| `notes.json` | `NotesModule` load, shape-filtered | warn, start empty |
| `reminders.json` | `RemindModule._load()`, one task per pending | warn, start empty |
| `steamids.json` | `SteamModule.on_load()` | warn, start empty |
| `audit.log` | tip hash only, lazily on first `record()` | genesis tip |
| `internets.pid` | stale-detection probe | see failure table above |

The store reads all three files synchronously in the constructor and never
reads them again; `channels_load()` serves the rejoin path from memory. Every
module store is validated per entry at load, dropping malformed rows rather
than rejecting the file: `reminders.json` coerces types and bumps `next_id`
past the highest loaded id so ids cannot collide; `tells.json` drops entries
lacking `from`, `msg`, or `ts`; `seen.json` keeps only dict entries carrying
a `ts`; `notes.json` keeps only list-valued keys with non-empty `text`.

## Survives a restart, survives a reload

`.reload <module>` (and `.reloadall`) unloads and re-executes one module file.
It is not a process restart: nothing outside that module is touched.

| State | Survives `.reload` | Survives restart |
| --- | --- | --- |
| Store datasets (locations, channels, users) | yes, untouched | yes, from disk |
| Module stores (seen, tells, notes, reminders, steamids) | yes, via `on_unload` flush then reload | yes, from disk |
| Shadow-ban set | yes, untouched | yes, from disk |
| Audit chain | yes | yes, tip re-read from the live file |
| Authenticated admin sessions | yes (held by core, not a module) | no |
| `RateLimiter` windows | yes | no |
| Geocode LRU cache | yes (helper module, not reloadable) | no |
| Per-module in-memory caches | no, reset on reload | no |
| Metrics counters | yes | no |
| `linktitle` URL dedup window | no | no |

`modules/geocode.py`, `modules/base.py`, `modules/_netsafe.py`,
`modules/units.py`, and `modules/__init__.py` have no `setup()` and are not
loadable modules; they are imported helpers, so their module-level state
(notably the geocode TTL/LRU cache and its hit/miss counters) lives for the
process lifetime and is unaffected by `.reload`.

## In-memory state that is never persisted

Reading the file list as "the bot's state" understates how much is volatile.
The following exist only in RAM and reset on every restart:

- Authenticated admin sessions and the failed-auth counters
  (`admin_cmds.py`), so a restart deauthenticates everyone. Lockouts also
  clear.
- `RateLimiter`'s three timestamp maps: per-nick flood, per-nick API, and the
  per-channel sliding window. A restart is a free pass through every rate
  gate.
- Connection state: `_caps`, `_chanops`, `_nick_hosts`, ISUPPORT-derived
  `_chanmode_types` and `_prefix_modes`, and the `_stats_*` counters behind
  `.stats`.
- The private `IRCBot._metrics` counter dict (reconnects, dropped messages,
  command timeouts, oversized lines, SASL failures, unexpected errors) used
  for the shutdown summary line. Despite the name it is unrelated to
  `metrics.py`; see [metrics-and-observability](metrics-and-observability.md).
- Every module cache: the geocode result cache, `linktitle`'s per-channel URL
  dedup map (capped at 500 entries), `scinews` / `spacex` / `crypto` /
  `ipintel` TTL caches, and the weather provider session cache.
- `notes.py`'s pending two-step `.notes clear` confirmations.

No cache is persisted, so no cache can be poisoned across a restart, and no
cache needs a backup.

## Corruption, quarantine, and recovery

`store.py - Store._read()` rejects a file that is present but unusable for
any of: unreadable (`OSError`), invalid JSON, invalid UTF-8, larger than
`_MAX_FILE_SIZE` (10 MB), wrong `schema` value, missing or non-string
`checksum`, checksum mismatch, or a payload whose type does not match the
dataset default (a list where a dict is expected). The response is to rename
the file to `<name>.corrupt.<unix-ts>`, log an error naming both the reason
and the destination, and start that dataset empty.

Quarantine exists precisely so the next 30-second flush cannot overwrite the
only copy of saved locations, rejoin state, and privacy opt-out flags.
Recovery is manual: inspect the `.corrupt.*` file, or rename `<name>.bak`
back into place. If the quarantine rename itself fails, that too only logs,
and in that case the next flush does overwrite the damaged file - but the
`.bak` still holds the previous good version.

A missing file is not corruption: it returns the dataset default silently,
which is the normal first-run path.

Failed writes never damage the target. `_write()` returns `False`, the temp
file is unlinked, the target and its `.bak` are untouched, and the dirty flag
stays set so the flush thread retries every 30 s indefinitely. An exception
anywhere in `flush()` is caught by the flush loop, logged with traceback, and
the thread continues, because a dead persistence thread would silently stop
all future saves with no liveness signal.

Module stores have none of this. A corrupt `tells.json` is logged and
replaced with an empty queue on the next save.

## Retention and pruning

| Dataset | Policy | Where |
| --- | --- | --- |
| `users.json` | delete rows whose `last_seen` is older than `user_max_age_days` (default 90) | `Store._prune_users()`, on every dirty users flush |
| `seen.json` | delete entries older than `[seen] max_age_days` (default 180, `0` disables) | at load and every flush |
| `tells.json` | 30-day TTL per entry | at load, at every save, and inside every lookup |
| `reminders.json` | deleted on fire or cancel; 30-day maximum lead bounds worst case | `RemindModule` |
| `locations.json` | none; entries persist until deleted or replaced | - |
| `channels.json` | none; replaced wholesale on each save | - |
| `notes.json` | none; notes live until the owner deletes them | - |
| `steamids.json` | none | - |
| `audit.log` | size rotation at 5 MiB; segments kept forever | `_rotate_if_oversize()` |
| `internets.log` | size rotation at `max_bytes` (5 MB), `backup_count` (3) kept | `RotatingFileHandler` |

Two carve-outs matter. `user_max_age_days` is floored at 1 day in the `Store`
constructor, so a misconfigured `0` cannot make the cutoff "now" and wipe the
dataset on first flush. Records carrying `opted_out: true` are exempt from
pruning entirely, so a privacy preference outlives the inactivity window;
a malformed or missing `last_seen`, by contrast, counts as stale.

## Privacy

Four artifacts hold personal data:

- **`users.json`** - nick, hostmask (usually embedding username and host or
  cloak), first-seen and last-seen timestamps, per channel, plus the
  `opted_out` flag. Populated from observed JOIN and channel PRIVMSG only;
  NAMES/353 replies are deliberately not used.
- **`seen.json`** - nick, channel, event type, and up to 60 characters of
  message body or part/quit reason.
- **`tells.json`**, **`notes.json`**, **`reminders.json`** - free-text user
  content keyed by nick, plus the channel a reminder was set in.
- **`audit.log`** - actor nick and hostmask for every privileged action.
- **`steamids.json`** - links an IRC nick to a Steam identity, which is PII
  by correlation across two namespaces.

`locations.json` is user-supplied location strings (ZIP codes, city names),
which are not identifiers on their own but are close to it in a small
channel.

### What the opt-out flag actually gates

`store.py` records the flag; consumers decide what it means. Verified against
every `is_opted_out` caller in the repo, it gates exactly two things: a
cross-user weather lookup (`.w -n <nick>`) and the `seen` module, which
enforces it three ways (stop recording, answer "never seen" on query, and
purge existing entries at each flush).

It does **not** stop the core's own user tracking. Rows continue to accrue in
`users.json` with the flag riding on them. Opt-out limits disclosure, not
collection; `.forgetme` is the collection remedy. An inline comment in
`store.py - Store.user_join()` claiming that last-seen and hostmask updates
are skipped for opted-out users overstates this - no caller skips them.

### What `.forgetme` reaches

`modules/privacy.py - PrivacyModule.cmd_forgetme()` deletes the saved
location, clears opt-out bookkeeping, hard-deletes every `users.json` row for
the nick (`Store.user_purge()`, deliberately not `user_quit`, which would
refresh `last_seen` instead of erasing it), then fans out to every loaded
module's `BotModule.forget(nick)`. `seen`, `tell`, `notes`, and `remind`
override it; `steam` also implements it.

What it cannot reach:

- **`audit.log`.** Records are append-only and chained; deleting one breaks
  verification by design. An admin's own nick and hostmask are permanent.
- **`internets.log`.** Nothing purges the application log. Two known
  logging paths put user data there, below.
- **Rotated segments and backups.** `<name>.bak`, `<name>.corrupt.*`,
  `audit.log.<stamp>`, and `internets.log.N` are outside every erasure path.
  A `.forgetme` run leaves the previous `users.json` contents intact in
  `users.json.bak` until the next successful write replaces it.

> **Known defect** (`modules/privacy.py - cmd_forgetme()`): step 2 clears the
> opt-out flag before `user_purge` runs. For a nick with no tracked records,
> `Store.set_opt_out()` creates the synthetic `"*"` sentinel row, which
> `user_purge` then deletes and counts, so an untracked user is told
> "tracking in 1 channel(s) (erased now)" and never receives the honest
> "no stored records" reply.

> **Known defect** (`store.py - Store._write()`, backup branch): the `.bak`
> copy is written with `Path.write_bytes` and never chmodded. On first
> creation it takes umask-default permissions (typically 0644), so the PII
> that the 0600-before-replace sequence protects in `users.json` is
> world-readable in `users.json.bak`.

> **Known defect** (privacy, logging): `modules/location.py - cmd_regloc()`
> logs `regloc <nick> -> '<raw input>' (<resolved place>)` at INFO, and
> `modules/linktitle.py` logs every announced and every skipped URL together
> with the target channel at INFO. Both land in `internets.log`, where
> `.forgetme` cannot purge them. See
> [logging-and-auditing](logging-and-auditing.md).

## Permissions

| Artifact | Mode | Enforcement |
| --- | --- | --- |
| store JSON files | 0600 | `chmod` on the temp file *before* the replace |
| `<name>.json.bak` | umask default | none (see defect above) |
| module JSON stores | 0600 | `mkstemp` default, or explicit `chmod` |
| `audit.log`, `audit.log.key` | 0600 | `os.open` mode bits, re-chmod after each append |
| `config.ini` | 0600 required | `secret_store.perms_ok()`, fail-closed on read |
| `internets.pid` | 0644 | deliberate; contents are diagnostic, not secret |
| `internets.log` | umask default | none |

All `chmod` calls are POSIX-gated (`os.name != "nt"`); on Windows the
operator owns the ACLs. Note the asymmetry: `config.ini` permissions are
*enforced* (a mode other than exactly 0600 makes `secret_store.get()` refuse
to read the file and the bot runs keyless), while the log file, which can
contain user URLs and locations, is left at umask default.

## Backup

Back up as one consistent set, because the audit chain and its key are
useless apart:

1. `config.ini` (holds `[secrets]`; treat as a credential store).
2. The state JSON files: `locations.json`, `channels.json`, `users.json`,
   `shadow_bans.json`, `seen.json`, `tells.json`, `notes.json`,
   `reminders.json`, `steamids.json`.
3. `audit.log`, `audit.log.key`, and every rotated `audit.log.<stamp>`
   segment.

Do not back up `internets.pid` - restoring a lockfile from another host or
another PID is exactly the stale-lock case, and on a foreign hostname the
lock is refused conservatively without probing at all.

Keep `.bak` and `.corrupt.*` files until the live files are confirmed good,
then delete them; they are recovery artifacts and they carry the same PII as
the originals at weaker permissions.

The cleanest consistent snapshot is taken with the bot stopped, since a
running bot can flush mid-copy. Individual files are never torn (atomic
rename), but the set is not written transactionally, so a live copy can
capture `users.json` from after a prune and `audit.log` from before it.

Restoring is a file copy plus a permissions check: confirm 0600 on the state
files, `config.ini`, and the audit pair before starting the process, because
a loose `config.ini` mode makes every secret lookup return its default.

> **Durability caveat**: no writer in the codebase calls `os.fsync()` -
> verified by repository-wide search. Every atomic replace guarantees a
> reader never observes a partial file, but a host crash or power loss
> shortly after a write can still lose the newest version at the filesystem's
> discretion. For the store this degrades to a detected, recoverable event
> (checksum plus quarantine plus `.bak`); for the audit log and the module
> stores it is a silent loss of the most recent records.

## Related reading

- [internals/store](internals/store.md) - the three core datasets, envelope,
  quarantine, and `RateLimiter`.
- [internals/audit_log](internals/audit_log.md) - record format and chain.
- [internals/process_lock](internals/process_lock.md) - single-instance
  protocol.
- [internals/secret_store](internals/secret_store.md) - the `[secrets]`
  backend.
- [logging-and-auditing](logging-and-auditing.md) - the two log streams.
- [security-model](security-model.md) - state-file integrity in the wider
  threat model.
- [deployment](deployment.md) - running, backup checklist, upgrades.
