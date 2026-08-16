# privacy.py - user-facing data-protection commands (.forgetme / .privacy / .optout / .optin)

## Purpose

`PrivacyModule` is the GDPR-style hygiene surface: right-to-erasure
(`.forgetme`), transparency (`.privacy`), and a per-nick opt-out flag
(`.optout` / `.optin`). It owns no data store of its own; it orchestrates
erasure and disclosure across the bot's stores. Base contract:
[base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.forgetme` | `modules/privacy.py - PrivacyModule.cmd_forgetme()` | PM-only; purge every record of the invoking nick |
| `.privacy` | `PrivacyModule.cmd_privacy()` | PM-only; privately list everything stored about the invoker |
| `.optout` | `PrivacyModule.cmd_optout()` | set the opt-out flag (NOTICE-acked) |
| `.optin` | `PrivacyModule.cmd_optin()` | clear the flag |

`.forgetme` and `.privacy` are gated by `PrivacyModule._require_pm()` (a
channel invocation gets a NOTICE telling the user to /msg the bot) so nobody's
data is echoed into a channel.

## The opt-out flag: exact contract

Canonical storage is `store.Store` (`store.py - Store.set_opt_out()` /
`Store.is_opted_out()`): the flag is written onto EVERY per-channel
user-tracking record for the nick, and onto a sentinel record in a synthetic
`"*"` channel when the nick is not tracked anywhere, so the preference
survives restarts and pre-dates the user's next message. `is_opted_out`
returns True if ANY record carries the flag.

A legacy scheme (an `__optout__:<nick>` key squatting in the locations store)
is read-only supported: `PrivacyModule._is_opted_out()` checks the canonical
column first, then the legacy key, migrating legacy -> canonical and deleting
the legacy key on first sight (one-shot migration; the module never writes
the legacy key). If the canonical store is unavailable the legacy flag is
still honoured.

What the flag GATES (cross-checked against every `is_opted_out` consumer in
the repo):

- `modules/weather.py` - cross-user location lookups (`.w -n <nick>`) refuse
  for an opted-out target (weather.py:575-578).
- `modules/seen.py` - three layers: recording stops
  (`SeenModule._record()`), `.seen <nick>` answers "never seen"
  (`SeenModule.cmd_seen()`), and existing records are purged at each flush
  (`SeenModule._purge_opted_out()`).

What it does NOT gate (by design or by omission):

- `modules/location.py` self-only commands - first-party data, intentionally
  exempt (stated in this module's class docstring).
- `store.Store`'s own per-channel tracking (`user_join`/`user_seen`): records
  continue to accrue nick + hostmask + first/last seen; the flag rides on the
  records but does not stop them. Opt-out limits DISCLOSURE, not collection;
  `.forgetme` is the collection remedy.
- `.tell` - an opted-out nick can still be a tell recipient.
- `.remind`, `.notes` - self-only stores, not gated.
- `linktitle` / `urls` - no per-user storage to gate.

## `.forgetme` erasure flow (`cmd_forgetme`)

1. Saved location: `bot.loc_del(nick)`.
2. Opt-out bookkeeping: clears the canonical flag
   (`_store_set_opt_out(nick, False)`) and the legacy key, so a later
   `.optin`/`.optout` cycle is truthful. Not surfaced in the confirmation.
3. Channel tracking: snapshots which active channels currently track the nick
   (for honest reporting), then hard-deletes via
   `store.Store.user_purge(nick)` - immediate row removal across all
   channels, explicitly NOT `user_quit` (which would REFRESH last_seen; the
   in-code comment calls this out and fails loud if `user_purge` is missing).
4. Module fanout: iterates every loaded module under `bot._mod_lock` and
   calls `BotModule.forget(nick)` (`modules/base.py - BotModule.forget()`,
   default 0). Overridden by seen / tell / notes / remind; one module raising
   is logged and does not abort the rest.
5. Reports the itemized deletion list, or "no stored records" when nothing
   was found.

## `.privacy` disclosure (`cmd_privacy`)

Privately reports: saved location (or none), the invoker's own current
hostmask from the bot's in-memory `_nick_hosts` cache
(`PrivacyModule._own_hostmask()` deliberately never looks up anyone else's),
per-channel tracking rows (first/last seen, with the retention caveat),
opt-out status, and a pointer to `.forgetme` + PRIVACY.md.

## Integration / configuration

No external services, no secrets; `is_configured()` returns `True`.

## Failure behavior

Every store access is defensive (`getattr` + callable checks): a missing
store degrades `.optout` to a visible "opt-out is unavailable - see admin"
NOTICE rather than silently pretending success; a missing `user_purge` logs
an error and reports tracking as NOT erased.

## Findings

- defect | privacy.py - PrivacyModule.cmd_forgetme() | step 2's
  `_store_set_opt_out(nick, False)` runs BEFORE `user_purge`; for a nick with
  no tracked records, `store.Store.set_opt_out()` creates the `"*"` sentinel
  row, which `user_purge` then deletes and counts, so an untracked user is
  told "tracking in 1 channel(s) (erased now)" and never gets the honest
  "no stored records" reply (evidence: set_opt_out's not-seen branch creates
  the sentinel unconditionally, store.py:443-452).
- doc-drift | privacy.py - PrivacyModule class docstring | claims "All
  commands are PM-only", but `cmd_optout`/`cmd_optin` never call
  `_require_pm()`; they leak nothing (NOTICE replies), yet the docstring and
  the code disagree.
- questionable | privacy.py - PrivacyModule.cmd_forgetme() | none of the four
  commands calls `bot.rate_limited()`, unlike nearly every other module;
  `.privacy` in particular emits several lines per invocation.
- test-gap | privacy.py - PrivacyModule | no `tests/test_privacy*` exists;
  the legacy-flag migration, forgetme fanout, and PM gating are untested.
