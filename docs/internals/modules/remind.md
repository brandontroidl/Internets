# remind.py - scheduled per-user reminders delivered in-channel

## Purpose

`RemindModule` lets a user schedule a message the bot repeats back to them at a
future time, in the channel (or PM) where it was set. Reminders survive restarts
via a JSON store; delivery is driven by one `asyncio` task per pending reminder.
Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.remind` | `modules/remind.py - RemindModule.cmd_remind()` | `.remind <when> <message>` - schedule for yourself |
| `.remind-list` | `RemindModule.cmd_remind_list()` | list your pending reminders (max 5 lines shown) |
| `.remind-cancel` | `RemindModule.cmd_remind_cancel()` | `.remind-cancel <N>` - cancel your reminder #N |

Reply shape: `nick: ⏰ reminder #3 set for 2026-05-20 18:00 UTC - <msg>` on set;
delivery is `nick: ⏰ <msg>  (set 2h 15m ago)` or, when the bot was down past the
due time, `(was due 4h 2m ago)` (`RemindModule._fire()`).

Accepted `<when>` forms (`modules/remind.py - _parse_when()`): relative durations
`30s / 5m / 2h / 1d` and combinations (`1h30m`, `2d4h`); `tomorrow` (exactly
now+24h); `tonight` (20:00 UTC today, else tomorrow); `HH:MM` (next occurrence,
UTC); `YYYY-MM-DDTHH:MM` (absolute UTC). All clock math is UTC only.

Limits (module constants): lead time 30 s minimum / 30 days maximum
(`MIN_LEAD_SECONDS` / `MAX_LEAD_SECONDS`), 10 active reminders per nick
(`MAX_PER_NICK`), 200-char message (`MAX_MSG_LEN`).

## Integration

None. No external HTTP. `is_configured()` returns `True` unconditionally.

## State and persistence

- Store file: `reminders.json` (override: `[remind] file` in config), JSON shape
  `{"next_id": N, "reminders": {"<id>": {id, nick, channel, msg, due_ts, created_ts}}}`.
- Written by `RemindModule._save()`: atomic `tempfile.mkstemp` + `os.chmod 0o600`
  + `os.replace`, with unlink-on-error. `_save()` documents "caller holds lock"
  and every call site complies (`cmd_remind`, `cmd_remind_cancel`, `_fire`,
  `forget` all hold `self._lock`).
- Loaded by `RemindModule._load()` with per-entry shape validation (required
  fields, type coercion); malformed entries are dropped silently and `next_id`
  is bumped past the highest loaded id so ids never collide.
- Retention: a reminder lives until it fires or is cancelled; the 30-day max
  lead bounds worst-case retention of undelivered content. Fired and cancelled
  reminders are deleted from the store immediately.
- Privacy: the file holds nick + channel + free-text message content, 0600,
  local only. `RemindModule.forget()` implements the `.forgetme` right-to-erasure
  hook (see [privacy](privacy.md)): deletes every reminder whose `nick` matches
  case-insensitively, saves under the lock, then cancels the timer tasks outside
  the lock, returning the count.

## Lifecycle and concurrency

`on_load()` loads the store, then creates one `_fire(rid)` task per pending
reminder on `bot._loop` (already-overdue reminders get `delay <= 0`, skip the
sleep, and deliver immediately with the "was due" tail). `on_unload()` cancels
all outstanding tasks; the JSON re-loads on next `on_load`, so a module reload
does not lose reminders.

`self._lock` (a `threading.Lock`) guards `_reminders`/`_next_id`/`_save`.
`_fire()` re-checks existence after the sleep and pops atomically, so
cancel-during-sleep loses cleanly (the cancelled task returns; the pop finds
nothing). `self._tasks` is mutated only from the event-loop thread (command
handlers, `_fire`'s `finally`, `forget`), so it is unlocked by design.

## Failure behavior

- Unreadable or malformed store file: warn and start empty (`_load()`).
- Save failure: warning only; in-memory state stays authoritative until the
  next save attempt.
- `bot._loop` missing at `on_load`: warning "reminders will not fire"; store
  intact but no delivery until reload.
- Delivery `privmsg` raising: logged, reminder already removed (delivery is
  at-most-once; a crash between pop-and-save and `privmsg` drops the reminder).

## Security notes

- Message content is passed through `base.strip_ctrl` at capture
  (`cmd_remind`), so the stored-then-replayed, bot-attributed message cannot
  carry IRC colour/format/BEL/ANSI injection; the sender additionally strips
  CR/LF/NUL (`sender.py`).
- Rate limiting via `bot.rate_limited()` on all three commands.
- Delivery targets the captured `channel` (== `reply_to` at set time) with no
  re-check; if the bot has since left that channel the PRIVMSG is simply
  rejected by the server.

## Findings

- questionable | remind.py - module imports | `strip_ctrl` and
  `BotModule`/`help_row` are imported from `.base` in two consecutive separate
  import statements (remind.py:34-36); cosmetic duplication.
- questionable | remind.py - RemindModule.cmd_remind() | the comment above
  `channel = reply_to` says "Only deliver in channels" but the code delivers
  wherever the command was issued, including PMs (the comment's own
  parenthetical admits this); the leading claim is misleading.
- test-gap | remind.py - RemindModule | no `tests/test_remind*` exists; the
  only test reference is a `help_row` formatting fixture in
  `tests/test_help.py`, so parsing (`_parse_when`), persistence round-trip, and
  cancel/fire races are untested.
