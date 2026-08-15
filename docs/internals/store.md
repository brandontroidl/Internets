# store.py - persistent bot state (locations, channels, user tracking) and rate limiting

## Purpose

`store.py` is the single owner of the bot's persistent JSON state and of in-memory
rate-limit bookkeeping. It answers "what files does the bot write, when, and what is
in them" for three datasets - saved weather locations, the channel-rejoin list, and
per-channel user tracking (including the privacy opt-out flag) - and provides the
`RateLimiter` used to throttle command traffic. Everything else the bot persists
(seen-tracking, audit logs, secrets) lives elsewhere; see Boundaries.

## Responsibilities / boundaries

Belongs here:

- The three state files and their full lifecycle: load at startup, in-memory
  mutation, periodic dirty-flag flush, atomic write, integrity envelope,
  corruption quarantine, retention pruning.
- The per-nick location map (`loc_get` / `loc_set` / `loc_del`).
- The channel-rejoin list (`channels_load` / `channels_save`).
- Per-channel user tracking: nick, hostmask, first/last seen, and the
  `opted_out` privacy flag (`user_join` .. `is_opted_out`).
- `RateLimiter`: per-nick flood cooldown, per-nick API cooldown, per-channel
  sliding-window burst gate. Purely in-memory; never persisted.

Deliberately not here:

- Seen-tracking with message text. `modules/seen.py` keeps its own separate
  store and file; it only calls back into `Store.is_opted_out()` to honour
  opt-outs (`modules/seen.py:154,255,352`).
- Enforcement of the opt-out flag. `Store` records the flag; consumers
  (`modules/seen.py`, `modules/weather.py - cross-user lookup`) decide what it
  gates. See Findings for a comment in this file that overstates this.
- Config parsing. File paths and tuning arrive as constructor arguments,
  resolved by `internets.py` from `config.ini` `[bot]` keys.
- Secrets. `secret_store` handling is a separate subsystem; nothing here reads
  or writes credentials.

## Dependencies and dependents

External/stdlib only: `hashlib`, `json`, `logging`, `os`, `tempfile`,
`threading`, `time`, `datetime`, `pathlib`. No project imports, no network.

Dependents:

- `internets.py` - constructs both classes (`internets.py:259` and
  `internets.py:265`) and is the only writer of user-tracking events
  (JOIN/PART/QUIT/NICK/KICK/PRIVMSG handlers call `user_join`, `user_part`,
  `user_quit`, `user_rename`). It also proxies `loc_get`/`loc_set`/`loc_del`/
  `channel_users` as thin bot methods (`internets.py:462-465`) so modules do
  not touch `self._store` directly (though some do; see below).
- `modules/location.py` - `.setloc`/`.myloc`/`.delloc` via the bot proxies.
- `modules/weather.py` - reads saved locations; checks `is_opted_out` via
  `getattr(self.bot, "_store", ...)` before a cross-user `-n <nick>` lookup.
- `modules/privacy.py` - `.forgetme`/`.privacy`/`.optout`/`.optin`; calls
  `set_opt_out`, `is_opted_out`, `user_purge` (the latter directly on
  `bot._store`, `modules/privacy.py:158`).
- `modules/channels.py` - `.users` output from `channel_users`.
- `modules/seen.py` - opt-out checks only.
- `modules/health.py` - introspects the dirty flags for `.health` output
  (with a naming bug; see Findings).

Tests: `tests/test_store.py` (192 lines) covers locations, channels, user
tracking, pruning, flush/atomic-write, and the per-nick limiter windows.

## Lifecycle

- Imported once by `internets.py` (`from store import Store, RateLimiter`).
- `Store` is constructed in the bot's `__init__` (`internets.py:259`) with
  paths from `[bot]` config: `locations_file` (default `locations.json`),
  `channels_file` (default `channels.json`), `users_file` (default
  `users.json`), and `user_max_age_days` (default 90). Construction reads all
  three files synchronously, then starts the daemon flush thread
  (`store-flush`).
- `RateLimiter` is constructed immediately after (`internets.py:265`) with
  `FLOOD_CD` / `API_CD` from `config.py` (`flood_cooldown` default 3s,
  `api_cooldown`; both already floored at 1 in `config.py:102-103` and floored
  again in the constructor).
- Shutdown: the bot's teardown calls `Store.stop()` (`internets.py:557`),
  which sets the stop event and performs one final synchronous `flush()`. The
  flush thread is a daemon and is not joined; it exits on its next wakeup or
  dies with the process.

## State

### On-disk datasets (the complete list of files this class writes)

| File (config key) | Payload (the `data` field) | Written by | Read by |
| --- | --- | --- | --- |
| `locations.json` (`locations_file`) | `{"<nick lowercased>": "<raw location string>"}` | `flush()` when `_dirty_locs` | `Store.__init__` only |
| `channels.json` (`channels_file`) | sorted `["#chan", ...]` | `flush()` when `_dirty_chans` | `Store.__init__`; consumed via `channels_load()` on reconnect rejoin (`internets.py:839`) |
| `users.json` (`users_file`) | `{"<channel lowercased>": {"<nick lowercased>": {"nick", "hostmask", "first_seen", "last_seen", "opted_out"}}}` | `flush()` when `_dirty_users` (prunes first) | `Store.__init__` only |

Every file on disk is a v2 envelope, pretty-printed with `indent=2`:

```json
{
  "schema": 2,
  "checksum": "<sha256 hex of canonical-JSON of data>",
  "data": { ... }
}
```

`first_seen`/`last_seen` are ISO-8601 UTC strings from `_utcnow()`. Nick and
channel keys are lowercased at every entry point; the original-case nick is
preserved in the `nick` field. `opted_out` is a bool; a synthetic channel key
`"*"` can hold a sentinel record for a nick that opted out before ever being
tracked (`set_opt_out`).

Side files this class also creates:

- `<file>.bak` - one-deep copy of the previous good file, taken inside
  `_write()` just before the atomic replace.
- `<file>.corrupt.<unix-ts>` - quarantined original when a read rejects the
  file (`_quarantine`).
- `<random>.tmp` in the same directory - transient `mkstemp` output, renamed
  over the target on success, unlinked on failure.

### In-memory vs on-disk split

All reads and mutations happen against the in-memory dicts (`_locs`,
`_channels`, `_users`); disk is only touched at load (once, in the
constructor) and at flush. A mutation sets the dataset's dirty flag; the
flush thread writes dirty datasets every `_FLUSH_INTERVAL` (30s). Crash
exposure is therefore up to 30 seconds of unflushed mutations, minus whatever
the final `stop()` flush captures on clean shutdown.

`RateLimiter` state (`_flood`, `_api`, `_channel` timestamp maps) is
in-memory only and resets on restart.

### Retention

- Users: `_prune_users()` runs inside every dirty flush of the users dataset
  and on the public `prune_users()`. It deletes any entry whose `last_seen`
  is older than `user_max_age_days` (floored at 1 day in the constructor so a
  misconfigured 0 cannot wipe the dataset), except entries with
  `opted_out: true`, which are kept forever so the preference outlives the
  inactivity window. Empty channel dicts are removed. Malformed or missing
  `last_seen` counts as stale (`_before` returns True on parse failure).
- Locations and channels: no retention; entries persist until explicitly
  deleted (`loc_del`, `.forgetme`) or replaced (`channels_save`).

### Privacy implications

`users.json` is PII: nick + hostmask (which usually embeds username and host
or cloak) + activity timestamps, per channel. `locations.json` is
user-supplied location strings (ZIP codes, city names). Mitigations in this
file: `chmod 0o600` on the temp file before the atomic replace (POSIX only,
`_write`), the 90-day inactivity prune, hard deletion via `user_purge()`
(the `.forgetme` path - removes rows immediately rather than stamping
`last_seen`), and the `opted_out` column. The `.bak` copy does not get the
same permission treatment (see Findings). `README.md:526` documents the user
tracking behaviour publicly.

## Concurrency

- One dedicated flush thread per `Store` (`store-flush`, daemon). All other
  calls come from the asyncio event-loop thread via `internets.py` or from
  modules (which run on the loop or in `to_thread` offloads).
- Three independent `threading.Lock`s - `_loc_lock`, `_chan_lock`,
  `_user_lock` - one per dataset, so a weather location read never blocks
  behind a user-tracking flush. Every public method takes exactly one lock;
  no method takes two, so lock ordering cannot deadlock.
- `flush()` is safe from any thread; the dirty-flag test-and-write happens
  under the dataset lock, so a concurrent mutation either lands before the
  write (and is included) or after (and re-marks dirty for the next cycle).
  Note the disk write itself (`_write`) happens while holding the dataset
  lock, so a slow disk briefly blocks mutators of that one dataset.
- `stop()` can race the flush thread's own in-progress `flush()`; the per
  dataset locks serialize them, worst case producing one redundant write.
- `channel_users()` returns a copied snapshot (`{k: dict(v) ...}`) so callers
  cannot mutate tracked state outside the lock.
- `RateLimiter` uses a single lock for all three maps; every check is a
  short critical section (read, compare, stamp).

## Failure behavior

Startup, per file (`_read`):

- Missing file: return the dataset default (`{}` or `[]`). Normal first run;
  nothing logged.
- Present but unusable - any of: unreadable (`OSError`), invalid JSON,
  invalid UTF-8, larger than `_MAX_FILE_SIZE` (10 MB), v2 envelope with wrong
  `schema`, missing/non-string `checksum`, checksum mismatch, or payload type
  not matching the default (list where dict expected, `BUG-051`): the file is
  quarantined - renamed to `<name>.corrupt.<unix-ts>` via `os.replace` - an
  error is logged naming the reason and destination, and the dataset starts
  empty. Quarantining exists precisely so the next flush cannot overwrite the
  only copy of locations, rejoin state, and opt-out flags; recovery is a
  manual rename-back (or restore of `<file>.bak`). If the quarantine rename
  itself fails, that too only logs (`_quarantine` is best-effort) - in that
  worst case the next flush will overwrite the corrupt file, but the `.bak`
  still holds the previous good version.
- Legacy v1 file (bare payload, no `schema` key): accepted silently, no
  checksum verification possible, re-written as v2 on the next flush.

Failed write (`_write` returns False): logged at warning, the temp file is
unlinked, the target file and its `.bak` are untouched, and the dirty flag
stays set so the flush thread retries every 30s indefinitely. A raised
exception anywhere in `flush()` is caught by `_flush_loop`, logged with
traceback, and the thread continues - a persistence-thread death would
otherwise silently stop all future saves with no liveness signal.

Write atomicity: `mkstemp` in the target's own directory (same filesystem,
so the final `os.replace` is an atomic rename on POSIX), full JSON serialized
to the temp fd, permissions tightened, then replace. Readers can never
observe a half-written target. There is no `fsync` before the rename, so an
OS crash or power loss shortly after a flush can still lose or truncate the
newest version at the filesystem's discretion; the checksum envelope plus
quarantine plus `.bak` turn that into a detected, recoverable event rather
than silent corruption.

`RateLimiter` has no failure modes beyond its inputs; constructor floors
(`max(1, cd)`) prevent a zero/negative cooldown from silently disabling the
gate (`now - ts < 0` would never be true).

## Security

- Trust boundary: nick, hostmask, channel, and location strings all originate
  from the IRC network (attacker-influenced). They are used only as dict keys
  and JSON values - never as filesystem paths, shell input, or format
  strings - so there is no injection surface in this file. File paths come
  from the operator's config, not from network data.
- Integrity: the v2 SHA-256 checksum (canonical JSON: `sort_keys=True`,
  compact separators) detects on-disk corruption or casual tampering. It is
  not authentication - anyone who can write the file can recompute the
  checksum - and does not claim to be.
- Resource caps: the 10 MB `_MAX_FILE_SIZE` read cap prevents a grown or
  malicious state file from exhausting memory at startup.
- Permissions: `0o600` before the atomic replace so the final file is never
  world-readable even momentarily (POSIX; Windows ACLs left to the operator).
  The `.bak` copy is exempt from this - see Findings.
- `RateLimiter` is itself a security control: `flood_check` (per-nick, admins
  bypass), `api_check` (per-nick, protects paid/rate-limited geocode and
  weather APIs), `channel_check` (per-channel sliding window, catches
  coordinated floods where N distinct nicks each stay under their per-nick
  limits). On an over-budget channel the new attempt is deliberately NOT
  recorded, so attackers cannot keep the window pinned full by continuing to
  spam after the limit trips.

## Classes

### `Store`

In-memory state with periodic disk flush. Constructed once per bot process;
owns the three datasets, their locks, dirty flags, and the flush thread.

Constructor `(loc_file, channels_file, users_file, user_max_age_days=90)`:
stores paths, floors the prune age at 1 day, creates the three locks, reads
all three files (quarantining any unusable one), zeroes the dirty flags, and
starts the daemon flush thread. Construction never raises on bad state
files - worst case it logs and starts empty.

Invariants:

- All nick and channel keys in `_locs` and `_users` are lowercase.
- A dataset's dirty flag is True whenever memory differs from disk (modulo
  the write-failure retry window).
- `_users` never contains an empty channel dict after a prune or purge.
- Opted-out records survive pruning.

Extension constraint: any new dataset must follow the same pattern - own
lock, own dirty flag, `_read` with a matching-type default, a branch in
`flush()` - or it silently never persists.

### `RateLimiter`

Three independent throttling windows over in-memory timestamp maps.
Constructed once (`internets.py:265`); no persistence, no thread of its own.
`_cleanup(now)` runs lazily inside every check, at most once per
`_CLEANUP_INTERVAL` (300s), evicting expired per-nick stamps and channel
windows so the maps cannot grow without bound. Class constants:
`_CHANNEL_WINDOW` = 10s, `_CHANNEL_DEFAULT_BURST` = 20 commands/window.

## Functions and methods

Module-level helpers:

| Symbol | Behavior |
| --- | --- |
| `_utcnow()` | ISO-8601 UTC now; the only timestamp format written to `users.json`. |
| `_before(iso, cutoff)` | True if `iso` parses to a datetime older than `cutoff`. Parses (handling `Z` suffix, naive values assumed UTC) rather than comparing strings; unparseable/missing values return True (treated stale). |
| `_checksum(payload)` | SHA-256 hex of canonical JSON (`sort_keys`, compact separators, `ensure_ascii=False`), insertion-order independent. |
| `_wrap_v2(payload)` | Builds the `{schema, checksum, data}` envelope. |
| `_unwrap(raw)` | Inverse: returns `data` after validating schema version and checksum; raises `_StoreRejected` on any envelope defect; passes a v1 bare payload through unchanged. |
| `_StoreRejected` | Exception marking a present-but-unusable file so `_read` quarantines instead of silently loading empty. |

`Store` methods:

- `_read(path, default)` (static) - load one file with the full validation
  chain (exists, size cap, JSON parse, envelope unwrap, type-matches-default);
  quarantine and return `default` on any failure. Called only from the
  constructor.
- `_quarantine(p, reason)` (static) - best-effort `os.replace` of the bad
  file to `<name>.corrupt.<ts>`; logs either way.
- `_write(path, data)` (static) - atomic envelope write as described under
  Failure behavior; returns bool success. Called only from `flush()`.
- `_flush_loop()` - flush thread body: `Event.wait(30)` then `flush()`,
  swallowing and logging any exception so the thread survives.
- `flush()` - per dataset, under its lock: if dirty and `_write` succeeds,
  clear the flag. Users flush prunes first (`_prune_users`). Channels are
  re-sorted at write time (redundant with `channels_save`, harmless).
- `stop()` - set the stop event, final flush. Does not join the thread.
- `_prune_users()` / `prune_users()` - retention pass (see State); private
  form assumes `_user_lock` held, public form takes it. Returns count removed.
- `loc_get/loc_set/loc_del(nick, ...)` - lowercase-keyed map operations under
  `_loc_lock`; `loc_del` returns whether an entry existed. Callers:
  `modules/location.py`, `modules/weather.py`, `modules/privacy.py` (which
  also uses the map's legacy `__optout__:<nick>` keys read-and-delete-only
  during migration).
- `channels_load()` / `channels_save(channels)` - copy-out / replace-with
  sorted-list under `_chan_lock`. Writer: `internets.py - _save_channels()`
  on every join/part; reader: `internets.py - _deferred_rejoin()` after
  (re)connect.
- `user_join(channel, nick, hostmask)` - upsert the tracked record: create
  with `first_seen`/`last_seen`/`opted_out=False`, or refresh `last_seen`,
  `hostmask`, display `nick` on an existing record (preserving `first_seen`
  and `opted_out`, back-filling `opted_out` on legacy records). Called on
  observed JOIN (`internets.py:1058`) and on channel PRIVMSG
  (`internets.py:1131`); NAMES/353 is deliberately not used (`README.md:526`).
- `user_part(channel, nick)` / `user_quit(nick)` - stamp `last_seen` on the
  one channel record / on every channel record for the nick. No-op if
  untracked.
- `user_purge(nick)` - hard-delete every record of the nick across all
  channels (including the `"*"` sentinel), drop emptied channels, return the
  row count. The `.forgetme` erasure path; deliberately distinct from
  `user_quit`, which records activity rather than erasing it
  (`modules/privacy.py:160-163`).
- `user_rename(old, new, hostmask)` - re-key the record in every channel that
  tracks `old`, updating display nick, hostmask, `last_seen`. Also used as an
  in-place metadata refresh via `user_rename(n, n, hm)` on account-change
  events (`internets.py:1028,1050`). If `new` was already tracked in a
  channel, its record is overwritten (see Findings).
- `channel_users(channel)` - deep-enough copy (per-entry `dict()`) of one
  channel's records. Callers: `.users` (`modules/channels.py:152`), `.privacy`
  and `.forgetme` reporting (`modules/privacy.py:148,232`).
- `set_opt_out(nick, value)` - set the flag on every tracked record of the
  nick; if none exist, create a sentinel record under synthetic channel `"*"`
  (empty hostmask) so the preference survives a restart even before the user
  next speaks.
- `is_opted_out(nick)` - True if any tracked record (any channel, including
  `"*"`) carries a truthy `opted_out`. The any-channel scan is what makes the
  answer consistent bot-wide regardless of where the flag was set.

`RateLimiter` methods:

- `_cleanup(now)` - lazy eviction, at most every 300s; drops per-nick stamps
  older than their cooldown and channel timestamp lists whose whole window
  elapsed. Callers hold `_lock`.
- `flood_check(nick, is_admin=False)` - True if the nick issued a command
  within `flood_cd` seconds; otherwise stamps now and returns False. Admins
  return False without stamping. Wired as `bot.check_flood`
  (`internets.py:394`); a positive answer produces the "slow down" notice
  (`internets.py:630`).
- `api_check(nick)` - same shape with the longer `api_cd` window; gates the
  expensive geocode/weather paths (`internets.py:397`).
- `channel_check(channel, threshold=None)` - sliding-window count for real
  channels only (prefix `# & + !`; PMs return False so only per-nick limits
  apply there). Filters the window, refuses at >= cap without recording the
  refused attempt, records otherwise. `internets.py:405` always calls it with
  the default threshold (20 per 10s); the parameter is exercised only by
  tests.

## Implementation walk

- Lines 1-14: imports and the `internets.store` logger. Stdlib only.
- Lines 17-36: timestamp helpers (`_utcnow`, `_before`) - validation-hardened
  parsing so pruning cannot be mis-ordered by a `Z` suffix, naive value, or
  offset variant; malformed means stale.
- Lines 39-40: tuning constants (30s flush, 90-day prune default).
- Lines 42-103: the v2 integrity envelope - version constant, canonical-JSON
  checksum, wrap/unwrap pair, and `_StoreRejected` as the
  quarantine-not-clobber signal. v1 compatibility is a read-side-only shim:
  accepted on load, upgraded on next flush, matching the repo's
  additive-migration policy.
- Lines 106-143 (`__init__`): path/limit capture with the 1-day prune floor
  (a 0/negative config value would otherwise make the cutoff "now" and wipe
  all users on first flush), per-dataset locks, the three synchronous reads,
  dirty flags, flush-thread start.
- Lines 145-234: disk I/O. `_read` is the validation gauntlet (size cap,
  parse, unwrap, type check tagged `BUG-051`); `_quarantine` preserves
  evidence; `_write` is the atomic temp-chmod-backup-replace sequence with
  cleanup of the temp file on every failure path.
- Lines 236-268: the flush machinery - exception-proof loop, per-dataset
  dirty-write-clear, `stop()`.
- Lines 270-305: retention (`_prune_users` / `prune_users`) with the
  opt-out-records-are-immortal carve-out.
- Lines 307-341: locations and channels accessors - minimal lock-wrapped map
  and list operations, lowercasing and copy-out where needed.
- Lines 343-420: user tracking - upsert, last-seen stamps, hard purge,
  re-key, snapshot. All lowercase-keyed, all single-lock.
- Lines 422-461: the opt-out column - fan-out write across all records plus
  the `"*"` sentinel, any-channel read.
- Lines 464-563: `RateLimiter` - constants, floored constructor, lazy
  cleanup, and the three check methods, ending with the
  refuse-without-recording rule that stops an attacker from pinning a
  channel's window full.

Nothing in the file is unreachable; every block is exercised by the wiring in
`internets.py` or by tests, except the noted test gaps.

## Findings

- defect | `modules/health.py` - `.health` store block | Reads
  `_dirty_locations` / `_dirty_channels` but the `Store` fields are
  `_dirty_locs` / `_dirty_chans` (`store.py - Store.__init__`), so `.health`
  permanently reports `?` for those two datasets while looking wired.
- defect | `store.py - Store._write()` (backup branch) | The `.bak` file is
  created with `Path.write_bytes` and never chmodded, so on first creation it
  gets umask-default permissions (typically 0644) - the PII in `users.json`
  that the 0600-before-replace dance protects is world-readable in
  `users.json.bak` on a default umask.
- questionable | `store.py - Store.user_join()` (comment) | The inline
  comment says opted-out users' "last-seen / hostmask updates should be
  skipped", but no caller skips them: `internets.py` calls `user_join`
  unconditionally on JOIN/PRIVMSG, and `modules/privacy.py`'s documented
  contract scopes the flag to cross-user lookups only. The comment overstates
  the flag's effect; tracking updates continue for opted-out users.
- questionable | `store.py - Store.user_rename()` | Renaming onto a nick that
  already has a record in the same channel overwrites that record, discarding
  its `first_seen` and `opted_out` - a user who opted out under the target
  nick loses the flag on that record if another user renames onto it (the
  any-channel `is_opted_out` scan masks this only while some other record
  still carries the flag).
- questionable | `store.py - Store._write()` | No `fsync` before
  `os.replace`; a power loss shortly after a flush can lose the newest
  version. Detected-and-recoverable via checksum + quarantine + `.bak`, but
  worth knowing for the durability story.
- test-gap | `tests/test_store.py` | No tests for: corruption quarantine
  (bad JSON / checksum mismatch / wrong schema / oversize / wrong payload
  type), v1-to-v2 upgrade on flush, the opt-out API (`set_opt_out` /
  `is_opted_out` / `"*"` sentinel / prune immunity), `user_purge`,
  `user_rename` collision, or `RateLimiter.channel_check` and `_cleanup`.
