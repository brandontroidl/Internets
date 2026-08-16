# channels.py - join/part management with ChanServ founder verification

## Purpose

`ChannelsModule` lets bot admins - and, uniquely, verified channel founders -
ask the bot to join or leave a channel, plus an admin-only roster query. The
interesting machinery is the asynchronous founder-verification flow: WHOIS the
requester's services account, ask services (`ChanServ` by default) for the
channel's founder, compare. Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.join` | `modules/channels.py - ChannelsModule.cmd_join()` | `.join <#channel>` - admin: immediate; otherwise founder verification |
| `.part` | `ChannelsModule.cmd_part()` | `.part <#channel>` - same authorization model |
| `.users` | `ChannelsModule.cmd_users()` | `.users [#channel]` - admin only; tracked nick+hostmask roster |

Channel names are validated against `_CHAN_RE`
(`^[#&+!][^\s,\x07]{1,49}$`). `.join` refuses channels already joined,
`.part` refuses channels not joined (checked against
`bot.active_channels`).

`.users` replies via `bot.preply()`/`notice` (private to the requester),
shows the 20 most-recently-seen entries (`_CAP`) with hostmask and
first/last-seen timestamps from `store.Store.channel_users()`, plus a summary
line - the cap exists to keep one command from flooding the shared send queue
(the in-code comment explains this).

## Founder verification flow

State: `_PendingJoin` (slots: nick, channel, reply_to, action, created,
account, founder, whois_done, info_failed), keyed by lowercased channel in
`self._pending`; at most one verification per channel at a time.

1. `ChannelsModule._start_verify()` registers the pending entry, then sends
   `WHOIS <nick>` and `PRIVMSG <services> :INFO <channel>` (both priority 1)
   and tells the user verification started.
2. `ChannelsModule.on_raw()` collects the answers:
   - numeric 330 (`is logged in as`) fills `p.account` for any pending entry
     matching the WHOIS'd nick;
   - numeric 318 (end of WHOIS) sets `whois_done` and calls `_try_complete`;
   - NOTICEs from the configured services nick are scanned: any `#channel`
     token matching a pending entry stamps a recency context in
     `self._svc_ctx`; a `Founder:`/`Owner:` line (`_FOUNDER_RE`) is attributed
     to the most recent context channel (`_ctx_by_recency()`); a
     `not registered` match (`_NOT_REG_RE`) sets `info_failed`, preferring a
     channel named in the same line, else the recency context.
3. `ChannelsModule._try_complete()` resolves when it can: not-registered ->
   deny; WHOIS done with no account -> deny ("identify with NickServ");
   account and founder both known -> case-insensitive compare -> approve or
   deny. Approval sends the JOIN/PART; every outcome messages the requester
   (`_resolve()`).
4. `ChannelsModule._cleanup_loop()` (asyncio task started in `on_load`,
   cancelled in `on_unload`) expires entries older than 15 s
   (`_VERIFY_TIMEOUT`) with a timeout message, and prunes stale `_svc_ctx`
   stamps.

The founder-attribution step is a correlation heuristic: services INFO output
is free-form text, so the module has no protocol-level way to bind a
`Founder:` line to a channel; it relies on the channel name appearing in an
earlier NOTICE line and recency ordering. With a single pending verification
(the common case) this is unambiguous; with several concurrent ones a
misattribution window exists (see Findings).

## Integration

IRC services only (WHOIS numerics + NOTICE from the services nick). No HTTP,
no secrets. Services nick configurable: `[bot] services_nick` (default
`ChanServ`).

## State and concurrency

All state is in-memory and transient (`_pending`, `_svc_ctx`); nothing is
persisted. `self._lock` (threading.Lock) guards both dicts; `on_raw` runs
synchronously on the event-loop thread, command handlers and the cleanup task
also run on the loop, so the lock is defensive. `bot.privmsg`/`send` are
called while the lock is held in `_resolve`, `on_raw`, and `_cleanup_loop` -
these enqueue rather than block on the network, so it is cheap, just wider
than necessary.

## Failure behavior

Verification that never completes (services down, WHOIS lost) times out at
15 s with a user-visible message. Duplicate requests for a channel already
being verified are refused. Cleanup-loop exceptions are caught and logged so
the task never dies silently.

## Security notes

- Authorization is admin OR founder; `bot.is_admin()` is the hostmask-bound
  session check in `internets.py`.
- The services NOTICE check trusts the NICK only
  (`m.group(1).lower() == self._services.lower()`), not the host. On networks
  where services enforce their own nicks this is fine; on a network where a
  user could hold the nick `ChanServ`, forged INFO output could fake a
  founder approval. The implementation implies services-protected nicks are
  assumed.
- `.users` deliberately exposes hostmask PII to admins only, privately.

## Findings

- questionable | channels.py - ChannelsModule.on_raw() | founder/not-registered
  attribution by `_svc_ctx` recency can misattribute a `Founder:` line when
  two verifications overlap within the 15 s window and the INFO output does
  not repeat the channel name on the founder line; worst case one channel's
  founder answer resolves the other pending request.
- questionable | channels.py - ChannelsModule._resolve() | `privmsg`/`send`
  are invoked while `self._lock` is held (also in `_cleanup_loop`); harmless
  with the current queueing sender, but the lock scope is wider than the
  state it protects.
- test-gap | channels.py - ChannelsModule | no `tests/test_channels*` exists;
  the verification state machine (330/318/NOTICE ordering, timeout,
  concurrent verifications) is untested.
