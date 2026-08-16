# seen.py - passive last-seen tracker with opt-out enforcement

## Purpose

`SeenModule` passively observes PRIVMSG/JOIN/PART/QUIT/NICK lines via the
`on_raw` hook and records the single most recent event per nick, answering
`.seen <nick>`. It is the module most directly governed by the privacy opt-out
flag (see [privacy](privacy.md)). Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.seen` | `modules/seen.py - SeenModule.cmd_seen()` | `.seen <nick>` - e.g. `**bob** last seen 2h 5m ago - PRIVMSG in #chan: "text"` |

Event renderings (`SeenModule._format_entry()`): PRIVMSG (channel + quoted
60-char snippet), JOIN (channel), PART (`left` / `left: reason`), QUIT
(`quit: reason`), NICK (old and new nick each get an entry pointing at the
other via arrow details).

## Integration

None external. Internal consumer of `store.Store.is_opted_out()`
(via `bot._store`).

## State and persistence

- In-memory dict `{nick_lower: {nick, ts, event, channel, detail}}`, one entry
  per nick (later events overwrite).
- Store file: `seen.json` (override: `[seen] file`), flushed every 60 s
  (`_FLUSH_INTERVAL`) by an asyncio task (`SeenModule._periodic_flush()`
  offloading `_flush_sync` to a thread) and once more at `on_unload`. Writes
  are dirty-flag gated, atomic (`mkstemp` is 0600, `os.replace` keeps the
  mode), unlink-on-error, with the dirty flag re-set on failure so the next
  interval retries.
- Retention: entries older than `max_age_days` (default 180, `[seen]
  max_age_days`, `0` disables) are pruned at startup and at every flush
  (`SeenModule._prune_stale()`).
- Privacy: the file contains nicknames, channels, and up to 60 chars of message
  body / part-quit reasons (`_DETAIL_MAX`) - the module docstring says so
  explicitly. Local only, 0600.

## Opt-out enforcement (three layers)

1. Record time: `SeenModule._record()` returns early when
   `bot._store.is_opted_out(nick)` - no new data for opted-out users.
2. Query time: `cmd_seen` answers `never seen <nick>` for an opted-out target,
   even if a pre-opt-out entry still exists on disk.
3. Retroactive: `SeenModule._purge_opted_out()` runs inside every flush and
   deletes entries whose nick has opted out since recording.

The bot's own nick is never recorded (`_record` checks `_own_nick()`), and
PRIVMSGs to non-channel targets (PMs to the bot) are deliberately not recorded
(`on_raw` PRIVMSG branch). `.forgetme` erasure is `SeenModule.forget()`: pops
the entry and flushes synchronously, returning 0 or 1.

## Concurrency

`self._lock` guards `_seen`/`_dirty`. `_record` mutates under the lock;
`_flush_sync` prunes, purges, and snapshots under the lock, then writes the
snapshot outside it (writer thread never blocks the IRC read path on disk
I/O). `on_raw` runs synchronously on the event-loop thread and is wrapped in a
catch-all so a parse error can never break the read path. `forget()` calls
`_flush_sync()` on the caller's (event-loop) thread - same minor pattern as
tell.py.

## Failure behavior

Unreadable/malformed `seen.json`: warn, start empty (only dict entries with a
`ts` survive the load filter). Flush failure: warn, re-mark dirty, retry next
interval. Missing `bot._loop` at load: flush task not scheduled (warning);
data still accumulates in memory and the unload flush persists it.

## Security notes

- `detail` is passed through `base.strip_ctrl` at record time
  (`SeenModule._record()`), so replayed last-messages and part/quit reasons
  cannot inject IRC formatting into bot-attributed `.seen` output; the comment
  notes this keeps the data clean both on disk and on replay.
- Shadow-banned nicks are excluded upstream (the `on_raw` fanout skip in
  `internets.py - _process()`).
- `cmd_seen` is rate-limited.

## Findings

- questionable | seen.py - SeenModule.cmd_seen() | the user-supplied `target`
  is echoed back unsanitized in `never seen {target}`; the sender strips
  CR/LF/NUL but IRC colour/format codes pass through, unlike this module's own
  strip-at-capture discipline for `detail` (cosmetic self-injection, not line
  injection).
- test-gap | seen.py - SeenModule | no `tests/test_seen*` exists; the on_raw
  event parsing, opt-out layers, and prune/flush cycle are untested.
