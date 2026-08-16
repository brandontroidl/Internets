# Data retention specification

Normative, per-datum retention for every piece of user-derived data the bot
writes. This page is the reference an operator answers an erasure request from
and the one a maintainer updates when a module starts storing something new.

The user-facing companion is [the privacy notice](privacy.md), which is
`PRIVACY.md` at the repository root and describes the same behaviour in plain
language. Where the two disagree, this page and the source win. The file-level lifecycle (atomic write,
checksum envelope, quarantine, flush cadence) is in
[state-and-persistence](state-and-persistence.md); the two log streams are in
[logging-and-auditing](logging-and-auditing.md).

Every value below was read from source, not from a comment or a changelog
entry. Symbols are given so each row can be re-checked.

## Scope and conventions

- "Default" means the value that applies with the shipped
  `config.ini.example`. Several keys the code reads are absent from that
  template, so their built-in default is the effective one; those are marked.
- Nick keys are lowercased in every store. A verification grep must lowercase
  the nick.
- All paths are relative to the process working directory at start. See
  [state-and-persistence](state-and-persistence.md) for why that matters.
- "Reached by `.forgetme`" means `modules/privacy.py - cmd_forgetme()` erases
  it, directly or through `BotModule.forget()`.

The three tables below are keyed on the same datum names and are meant to be
read together. They are split rather than merged because a single seven-column
table overflows the PDF build.

## Table A: what and where

| Datum | Owner | File |
| --- | --- | --- |
| User-tracking row | `store.py - Store.user_join()` | `users.json` |
| Opt-out flag | `store.py - Store.set_opt_out()` | `users.json` |
| Saved location | `store.py - Store.loc_set()` | `locations.json` |
| Channel rejoin list | `store.py - Store.channels_save()` | `channels.json` |
| Seen entry | `modules/seen.py - SeenModule._record()` | `seen.json` |
| Tell | `modules/tell.py - TellModule.cmd_tell()` | `tells.json` |
| Note | `modules/notes.py - NotesModule._do_add()` | `notes.json` |
| Reminder | `modules/remind.py - RemindModule.cmd_remind()` | `reminders.json` |
| Steam registration | `modules/steam.py - cmd_regsteam()` | `steamids.json` |
| Shadow-ban entry | `internets.py - _save_shadow_bans()` | `shadow_bans.json` |
| Audit record | `audit_log.py - AuditLog.record()` | `audit.log` |
| Application log line | `botlog.py - _setup_logging()` | `internets.log` |
| Rotated app log | `logging.handlers.RotatingFileHandler` | `internets.log.1`..`.N` |
| Rotated audit segment | `audit_log.py - _rotate_if_oversize()` | `audit.log.<stamp>` |
| State backup | `store.py - Store._write()` | `<name>.json.bak` |
| Quarantined state | `store.py - Store._quarantine()` | `<name>.json.corrupt.<ts>` |
| Geocode cache | `modules/geocode.py` | memory only |
| URL dedup map | `modules/linktitle.py` | memory only |
| Hostmask cache | `internets.py - IRCBot._nick_hosts` | memory only |

Contents per datum:

- **User-tracking row** - `nick`, `hostmask`, `first_seen`, `last_seen`,
  `opted_out`, keyed channel then nick. Written on observed JOIN and on channel
  PRIVMSG (`internets.py` membership and PRIVMSG handlers). NAMES/353 is
  deliberately not a source.
- **Seen entry** - `nick`, `ts`, `event`, `channel`, `detail`. `detail` is the
  message body, part/quit reason, or nick-change target, truncated to
  `modules/seen.py _DETAIL_MAX` (60) and passed through `strip_ctrl` at record
  time. Channel PRIVMSG only; PMs to the bot are not recorded.
- **Tell** - `from`, `to`, `msg` (<= 350 chars), `ts`. Caps: 10 per recipient
  (`_MAX_TELLS_PER_RECIPIENT`), 5 per sender (`_MAX_TELLS_PER_SENDER`).
- **Note** - `text` (<= 200 chars, `_MAX_LEN`), `ts`, keyed by nick. Cap 20
  (`_MAX_NOTES`).
- **Reminder** - `id`, `nick`, `channel`, `msg`, `due_ts`, `created_ts`. Cap 10
  per nick (`MAX_PER_NICK`), lead time 30 s to 30 days (`MIN_LEAD_SECONDS`,
  `MAX_LEAD_SECONDS`).
- **Steam registration** - lowercased nick to SteamID64 string.
- **Shadow-ban entry** - lowercased nick plus operator-supplied reason text.
- **Audit record** - `v`, `ts`, `actor`, `host`, `action`, `args`, `prev_hash`,
  `this_hash`. `actor` and `host` are the admin's nick and hostmask; a
  non-admin's nick can appear inside `args`.

## Table B: retention and pruning

| Datum | Default retention | Prune trigger |
| --- | --- | --- |
| User-tracking row | 90 days since `last_seen` | `Store._prune_users()` on each dirty users flush |
| Opt-out flag | Unbounded | Never pruned; exempt by design |
| Saved location | Unbounded | `.delloc`, `.forgetme`, or overwrite only |
| Channel rejoin list | Unbounded | Replaced wholesale each save |
| Seen entry | 180 days | `_prune_stale()` at load and each flush |
| Tell | 30 days or delivery | `_expire_locked_unsafe()` at load, save, lookup |
| Note | Unbounded | `.notes del`, `.notes clear`, `.forgetme` |
| Reminder | Until fire or cancel | `_fire()`, `.remind-cancel`, `.forgetme` |
| Steam registration | Unbounded | `.regsteam` overwrite or `.forgetme` |
| Shadow-ban entry | Unbounded | `.shadow-unban` only |
| Audit record | Unbounded | Never deleted; rotates at 5 MiB |
| Application log line | 4 files x 5 MB | Size rollover discards the oldest |
| Rotated app log | Until rollover past `backup_count` | Same |
| Rotated audit segment | Unbounded | Never removed by the bot |
| State backup | Until next successful write | Overwritten, never deleted |
| Quarantined state | Unbounded | Manual removal only |
| Geocode cache | 24 h, 1000 entries | TTL and LRU eviction; lost on restart |
| URL dedup map | 300 s, 500 entries | `_DEDUP_TTL`; lost on reload |
| Hostmask cache | Session | Dropped on the nick's QUIT, and on restart |

Config keys behind the tunable values:

| Key | Default | Effect |
| --- | --- | --- |
| `[bot] user_max_age_days` | 90 | `users.json` age cutoff |
| `[seen] max_age_days` | 180 | `seen.json` age cutoff |
| `[logging] max_bytes` | 5242880 | App log rollover size |
| `[logging] backup_count` | 3 | Rotated app log copies kept |

`[bot] user_max_age_days` is the only one of the four present in
`config.ini.example`. `[seen]` has no section in the template at all, so its
180-day default is the shipped behaviour and an operator must add the section
to change it. `[tell] file`, `[notes] file`, `[remind] file`, and
`[bot] shadow_bans_file` are likewise absent and fall to their built-in
defaults. This matches item 13 in [known-issues](known-issues.md).

Hardcoded values with no config key: the tell TTL (`_TTL_SECONDS`, 30 days),
the audit rotation threshold (`audit_log.py _MAX_BYTES`, 5 MiB), the seen
detail cap, and every per-user count cap in Table A.

## Table C: erasure and verification

| Datum | Reached by `.forgetme` | Verify by |
| --- | --- | --- |
| User-tracking row | Yes, `Store.user_purge()` | Read `users.json` after 30 s |
| Opt-out flag | Yes, cleared then purged | Same file, same read |
| Saved location | Yes, `loc_del()` | Read `locations.json` after 30 s |
| Channel rejoin list | N/A, no user data | - |
| Seen entry | Yes, `SeenModule.forget()` | Read `seen.json`, written at once |
| Tell | Yes, both directions | Read `tells.json`, written at once |
| Note | Yes, `NotesModule.forget()` | Read `notes.json`, written at once |
| Reminder | Yes, timers cancelled too | Read `reminders.json`, written at once |
| Steam registration | Yes, `SteamModule.forget()` | Read `steamids.json` |
| Shadow-ban entry | **No** | `.shadow-list`, or read the file |
| Audit record | **No**, append-only by design | `.audit`, or read `audit.log` |
| Application log line | **No** | Search `internets.log` for the nick |
| Rotated app log | **No** | Search `internets.log.*` |
| Rotated audit segment | **No** | Search `audit.log.*` |
| State backup | **No** | Search `*.json.bak` |
| Quarantined state | **No** | Search `*.json.corrupt.*` |
| Geocode cache | No, but expires in 24 h | Not inspectable at runtime |
| URL dedup map | No, but expires in 300 s | Not inspectable at runtime |
| Hostmask cache | No, cleared on QUIT | `.privacy` shows the invoker's own |

Verification procedure for a live instance:

1. Run `.forgetme` from the affected nick, in a PM to the bot.
2. Wait 30 seconds. The store flush thread (`store.py _FLUSH_INTERVAL`) writes
   `users.json` and `locations.json` on its own cadence; every module store is
   written synchronously inside `forget()` and needs no wait.
3. Read each JSON file and confirm the lowercased nick is absent as a key.
   `tells.json` additionally needs a scan of every entry's `from` field, since a
   message the nick sent is stored under the recipient's key.
4. Confirm `users.json` no longer carries the nick in any channel, including the
   synthetic `"*"` channel that `Store.set_opt_out()` uses for untracked nicks.
5. Search `internets.log`, its rotated copies, `audit.log` and its segments,
   `*.json.bak`, and `*.json.corrupt.*` separately. These are outside the
   erasure path; if the request requires them to be cleared, that is a manual
   operator action.

A shortcut that does **not** work: reading the files immediately after
`.forgetme` and finding the nick still in `users.json` proves nothing, because
the write is up to 30 seconds behind the in-memory state. Conversely, no writer
in this codebase calls `os.fsync()` (verified by repository-wide search), so a
host crash immediately after a confirmed erasure can restore the pre-erasure
file contents from the page cache's last durable state.

## Unbounded retention

Every row marked "Unbounded" in Table B grows without limit unless an operator
intervenes. Called out individually because each is a different decision:

- **`locations.json`, `notes.json`, `steamids.json`.** User-set records with no
  age policy. Deliberate: they are content the user chose to store, not passive
  observation. They persist until the user deletes them or leaves forever
  without doing so.
- **The opt-out flag and its sentinel row.** `Store._prune_users()` skips any
  row with `opted_out: true`, so an opt-out row is permanent by construction.
  When an untracked nick runs `.optout`, `Store.set_opt_out()` creates a row in
  a synthetic `"*"` channel to carry the preference across restarts, and that
  row is prune-immune for the same reason. The result is that exercising a
  privacy control creates a record with no retention limit. The alternative -
  expiring the opt-out - silently resumes tracking a user who asked the bot to
  stop, which is worse. Only `.forgetme` removes it.
- **`shadow_bans.json`.** Moderation state. No expiry, no review prompt, and no
  record of when the ban was set: the file stores the nick and the reason only.
- **`audit.log` and its rotated segments.** Append-only and hash-chained.
  Deleting a record breaks `verify()`, which is the point. Segments accumulate
  at 5 MiB each with nothing that removes them, so audit storage grows for the
  life of the deployment.
- **`<name>.json.corrupt.<ts>` quarantine files.** Written when a state file
  fails validation, never cleaned up. Each one is a full snapshot of a state
  file's contents at the moment it went bad, including the PII in `users.json`.
- **`<name>.json.bak`.** Bounded to one generation per file, but never deleted,
  and it is the copy that survives an erasure until the next successful write.
  Note the permission defect: `Store._write()` creates it with
  `Path.write_bytes` and no `chmod`, so it takes umask-default permissions
  (typically 0644) while the file it backs is 0600. Item 13,
  [known-issues](known-issues.md).

## Controls that can be disabled or neutralised

Each of these is a retention or protection mechanism that a configuration value
can switch off. Treat a deployment that sets any of them as having made an
explicit decision, and record it.

| Setting | Value that disables | Consequence |
| --- | --- | --- |
| `[seen] max_age_days` | `0` or negative | `seen.json` never prunes |
| `[logging] max_bytes` | `0` | App log never rotates, grows unbounded |
| `[logging] backup_count` | `0` | No rotated copies retained |
| `[bot] autoload` | omit `privacy` | No `.forgetme` for users |

Detail:

- **`[seen] max_age_days = 0`.** `modules/seen.py - _prune_stale()` returns
  early on `self._max_age_days <= 0`, so retention is off, not shortened. A
  non-integer value falls back to 180 rather than to 0, so the failure mode of
  a typo is safe.
- **`[bot] user_max_age_days` cannot be disabled.** `store.py - Store.__init__`
  applies `max(1, user_max_age_days)`, so `0` becomes 1 day rather than "prune
  everything now". This is the one retention control that fails safe in both
  directions, and the asymmetry with `[seen]` is worth knowing.
- **`[logging] max_bytes = 0`.** `botlog.py` passes the value straight to
  `logging.handlers.RotatingFileHandler`, whose documented behaviour is never
  to roll over when `maxBytes` is 0. The log then grows without limit, and
  since nothing purges it, so does the user data in it.
- **Omitting `privacy` from autoload.** The shipped template does exactly this
  while autoloading six collecting modules. Item 4,
  [known-issues](known-issues.md).

## Logs, backups, and quarantine

These four categories hold user data and sit outside `.forgetme` entirely. An
erasure request that must cover them is a manual operator procedure.

**`internets.log`.** Verified INFO-level sites that write user-derived data:

| Symbol | What it writes |
| --- | --- |
| `modules/location.py - cmd_regloc()` | nick, raw input, resolved place |
| `modules/linktitle.py` | announced and skipped URLs, plus channel |
| `modules/weather.py` (4 sites) | resolved place, country, lat/lon |
| `modules/channels.py` | admin nick with requested channel |
| `modules/privacy.py` | nick and what `.forgetme` deleted |

The weather sites carry no nick, but they are timestamped alongside lines that
do. The `privacy` site means the erasure request itself is logged where the
erasure cannot reach.

The file is created with the process umask and has no permission check, unlike
`config.ini`, which is fail-closed at 0600. Item 15,
[known-issues](known-issues.md). `UMask=0077` in the service unit is the
immediate mitigation.

`sender.py - redact_secrets()` masks credentials in log output. It matches
credential verbs, not personal data, so it does nothing for any row above.

**Rotated app logs.** `internets.log.1` through `.backup_count`. Discarded only
by further rollover, which is driven by log volume, not by time. A quiet bot
can retain months of rotated logs; a busy one, hours.

**Audit segments.** `audit.log.<UTC stamp>` at one-second stamp granularity,
renamed with `Path.rename()`, which silently overwrites: two rotations within
the same second destroy the earlier segment. `verify()`, `.audit`, and the
record counter read only the live `audit.log`, so a rotated segment can be
altered or deleted with no detection. Item 5, [known-issues](known-issues.md).

**Backups and quarantine.** `<name>.json.bak` and
`<name>.json.corrupt.<unix-ts>` carry the same personal data as the live files.
Delete them once the live files are confirmed good; they are recovery
artifacts, not archives.

**Operator backups.** Whatever the backup checklist in
[deployment](deployment.md) captures is outside every control here.

## Maintaining this specification

A module that stores anything about a user creates a retention obligation. The
work is not done when the data persists correctly; it is done when the data can
be erased, its lifetime is stated, and both facts are written down.

When adding or changing a module that stores user data:

1. **Override `forget(nick)`.** `modules/base.py - BotModule.forget()` returns
   0 by default, so a module that does not override it is silently outside
   `.forgetme` with no error and no warning. Mutate the store, persist it
   synchronously, and return the number of records removed. Model on
   `modules/tell.py - TellModule.forget()`, which is the only one that also
   erases records the nick **authored** rather than only those keyed to them -
   check whether yours has the same shape.
2. **Decide a retention default and implement it.** If the answer is
   "unbounded", say so explicitly here rather than leaving the row blank. An
   unstated default is indistinguishable from an oversight.
3. **Give it a config key only if the value is a real deployment decision.** If
   you add one, add the section to `config.ini.example` as well, or it joins
   `[seen]` and `[tell]` as a key the code reads and no operator can find.
   Bound and floor the value so a `0` cannot silently disable a guard, the way
   `Store.__init__` floors `user_max_age_days`.
4. **Add three rows here** - one each in Tables A, B, and C - and a contents
   bullet under Table A. Keep the datum name identical across all three; the
   tables are joined on it by the reader.
5. **Check the log sites.** Any `log.info` that names a nick, a location, a
   URL, or a message body adds a row to the "Logs, backups, and quarantine"
   table above and is data `.forgetme` cannot reach. Prefer DEBUG, or drop the
   identifier.
6. **Update [the privacy notice](privacy.md).** It is the document the bot points
   users at from `modules/privacy.py - cmd_privacy()`. A datum described here
   and absent there makes the user-facing notice incomplete.
7. **Extend `.privacy` if the datum is one a user would expect it to report.**
   `cmd_privacy()` currently discloses the core datasets only, not tells,
   notes, reminders, or Steam registrations.

The pull request checklist in `.github/PULL_REQUEST_TEMPLATE.md` carries the
first half of this as a single line pointing at
[state-and-persistence](state-and-persistence.md). Extending it to name this
page would make step 4 enforced rather than remembered.

## Related reading

- [privacy](privacy.md) - the user-facing notice (`PRIVACY.md`) built on this
  page.
- [state-and-persistence](state-and-persistence.md) - file lifecycle, atomic
  writes, quarantine, and backup.
- [logging-and-auditing](logging-and-auditing.md) - both log streams in detail.
- [integrations](integrations.md) - what leaves the machine, and to whom.
- [known-issues](known-issues.md) - the defects this page references by number.
- [internals/store](internals/store.md) - the three core datasets.
- [internals/modules/privacy](internals/modules/privacy.md) - the erasure
  command's implementation.
