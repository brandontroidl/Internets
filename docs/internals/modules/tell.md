# tell.py - offline messages delivered on the recipient's next PRIVMSG

## Purpose

`TellModule` queues a message for an absent user and flushes the queue the next
time that user says anything the bot can see. Delivery is passive, via the
synchronous `on_raw` hook (fanned out from `internets.py - _handle_line()`),
not via a command from the recipient. Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.tell` | `modules/tell.py - TellModule.cmd_tell()` | `.tell <nick> <message>` - queue a message; ack by NOTICE `will tell <nick> when they next speak` |
| `.tell-cancel` | `TellModule.cmd_tell_cancel()` | delete all tells YOU sent (any recipient); NOTICE `cancelled N pending tell(s)` |
| `.tell-list` | `TellModule.cmd_tell_list()` | list up to 5 of your outgoing tells, via NOTICE (private) |

Delivery line (`TellModule.on_raw()`):
`<recipient>: <sender> said at 2026-08-01 14:02 UTC: <msg>` - sent to the
channel the recipient spoke in, or back to the recipient directly if they PM'd
the bot.

Limits: 10 queued per recipient (`_MAX_TELLS_PER_RECIPIENT`), 5 outstanding per
sender across all recipients (`_MAX_TELLS_PER_SENDER`), 350-char message
(`_MAX_MSG_LEN`), 30-day TTL (`_TTL_SECONDS`). Self-tells and tells to the bot
are rejected with quips.

## Integration

None. No external HTTP. `is_configured()` inherits the base `True`.

## State and persistence

- Store file: `tells.json` (override: `[tell] file`), shape
  `{"<recipient_lower>": [{"from": nick, "msg": str, "ts": epoch, "to": display_nick}, ...]}`.
- `TellModule._save_sync()` snapshots under `self._lock` (expiring first), then
  writes atomically (`mkstemp` + `chmod 0o600` + `os.replace`, unlink-on-error).
  Command paths call it via `asyncio.to_thread`; the delivery path schedules it
  through `TellModule._schedule_save()` (create_task of a `to_thread` call, with
  a synchronous fallback when no loop is running).
- Retention: TTL-based. `TellModule._expire_locked_unsafe()` drops entries older
  than 30 days; it runs at load, at every save, and inside every command and
  delivery lookup, so expiry needs no timer task.
- Privacy: the file holds sender nick, recipient nick, and free-text message
  content, 0600, local only. `TellModule.forget()` (the `.forgetme` hook, see
  [privacy](privacy.md)) erases both directions: the nick's inbound queue and
  every entry they sent to others, then saves synchronously.

## Delivery flow (`TellModule.on_raw()`)

1. Match inbound `PRIVMSG` with `_PRIVMSG_RE`; key = sender lowercased. Any
   PRIVMSG counts, including command invocations - typing `.tell-list` also
   flushes your own inbound queue.
2. Lock-free membership pre-check `key not in self._tells` avoids taking the
   lock on every IRC line (a benign race: worst case one delivery is missed
   until the next message).
3. Under the lock: expire, then pop the whole queue (delivery is
   all-or-nothing per flush).
4. Reply target: the channel the recipient spoke in; if the message was a PM to
   the bot (target == bot nick) or some other non-channel target, reply to the
   sender directly.
5. Each entry is formatted and sent; a per-entry `privmsg` failure is logged
   and does not stop the rest. The popped entries are NOT re-queued on send
   failure (at-most-once delivery).
6. Save is scheduled asynchronously.

Note that opted-out users (see [privacy](privacy.md)) are still valid `.tell`
recipients; the opt-out flag gates cross-user lookups, not messaging.

## Failure behavior

Unreadable/malformed store: warn, start empty (entries lacking
`from`/`msg`/`ts` are dropped at load). Save failure: warning only.
`on_raw` regex mismatch: silent return (non-PRIVMSG lines).

## Security notes

- Message body and target nick pass through `base.strip_ctrl` at capture
  (`cmd_tell`), so the stored-then-replayed, bot-attributed message cannot
  inject IRC formatting; CR/LF/NUL are additionally stripped by the sender.
- Rate limiting on all three commands via `bot.rate_limited()`.
- `.tell-list` and its ack go via NOTICE to the sender, so message contents are
  not echoed into the channel.
- Shadow-banned nicks never reach `on_raw` (the fanout is skipped in
  `internets.py - _handle_line()`), so they can neither trigger delivery nor be
  recorded.

## Findings

- questionable | tell.py - TellModule.forget() | calls `_save_sync()` directly
  on the event-loop thread (blocking file I/O during `.forgetme`), unlike the
  command paths which offload via `asyncio.to_thread`; a single small JSON
  write, but inconsistent with the module's own pattern.
- questionable | tell.py - TellModule.on_raw() | in the expired-empty branch
  both arms set `changed = True` unconditionally, making the later
  `if changed:` guard dead; harmless but implies a condition that no longer
  exists.
- test-gap | tell.py - TellModule | no `tests/test_tell*` exists; queue caps,
  TTL expiry, and the `on_raw` delivery/reply-target logic are untested.
