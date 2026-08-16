# Privacy notice

**Effective date:** 2026-08-16
**Applies to:** Internets IRC bot 5.0.0 (`config.py __version__`)

This document describes what the Internets bot software records, what it sends
to third parties, how long it keeps things, and what a user can erase. It
describes **software behaviour**, verified against the source in this
repository. It is not a legal guarantee, a contract, or a compliance
certification, and it does not claim conformance with any privacy statute. If
you run this bot, the mapping from this behaviour onto your legal obligations
is yours to make.

Two audiences:

- **IRC users** - people who talk in a channel where the bot sits, or who run
  its commands. Read "What the bot records about you", "What leaves the
  machine", and "Your controls".
- **Operators** - whoever runs the bot process. The bot is self-hosted
  software, so **you** are the party responsible for the data it holds. Read
  "If you operate this bot".

The engineer-facing retention specification, with per-file defaults and
verification steps, is [docs/data-retention.md](docs/data-retention.md).

---

## Read this first if you are installing the bot

The shipped `config.ini.example` autoloads 67 modules. Five of them keep their
own store of user data: `seen`, `tell`, `notes`, `remind`, `steam`. `location`
keeps no file of its own but writes your saved location through the core store
and logs it. `linktitle` persists nothing, and writes every URL it announces or
skips, with the channel, into the application log. The `privacy` module is
**not** in that list.

A deployment that copies the template verbatim therefore collects user data and
offers **no `.privacy`, `.forgetme`, `.optout`, or `.optin` command at all**.
The erasure mechanism exists and works; it is switched off by default while
collection is switched on. Add `privacy` to `[bot] autoload` before putting the
bot on a live network.

This is recorded as item 4 in [docs/known-issues.md](docs/known-issues.md).

---

## What the bot records about you

All of it is plain files in the bot's working directory. There is no database
and nothing is sent to a central service operated by this project. Every file
below is written 0600 (owner-only) with one exception: `internets.log` takes
whatever the process umask gives it, and so do the segments it rotates into
(`internets.log.1` through `.3`).

| What | Why it is kept | Where it lands |
| --- | --- | --- |
| Nick, hostmask, first/last seen, per channel | Channel presence, so admin tools and `.privacy` can answer about you | `users.json` |
| Last activity plus up to 60 characters of context | Answers `.seen <nick>` | `seen.json` |
| Your saved weather location | So a weather command works with no argument | `locations.json` |
| Messages you left for someone offline | Delivered when the recipient next speaks | `tells.json` |
| Your personal notes | Recalled by `.notes` | `notes.json` |
| Your pending reminders | Fired at the time you asked for | `reminders.json` |
| Your registered Steam ID | So `.steam` works with no argument | `steamids.json` |
| Shadow-ban entries (set by an admin) | Moderation: your commands are then silently ignored | `shadow_bans.json` |
| Admin actions, with the admin's nick and hostmask | Tamper-evident accountability for privileged commands | `audit.log` |
| Every command you run, with your nick, hostmask, and the text you passed it | Operational logging | `internets.log` |
| Other application log lines, some carrying user data | Operational logging | `internets.log` |
| Every IRC line in and out, if the operator enabled it | Protocol debugging | `[logging] debug_file` |

### Channel presence tracking (`users.json`)

`store.py - Store.user_join()` records one row per nick per channel holding
your nick, your **hostmask** (which usually embeds your username and your host
or your network's cloak), a first-seen timestamp, a last-seen timestamp, and
your opt-out flag.

A row is created or refreshed when the bot observes you JOIN a channel it is
in, or send a message to that channel. `user_part()` and `user_quit()` refresh
the last-seen stamp. NAMES replies are deliberately not used, so merely being
listed in a channel does not create a row - the bot has to see you act.

This tracking is part of the bot core. It runs whether or not the `privacy`
module is loaded, and `.optout` does **not** stop it (see "Your controls").

### Last-seen tracking (`seen.json`, module `seen`)

`modules/seen.py - SeenModule._record()` keeps one entry per nick: the nick, a
Unix timestamp, the event type (`PRIVMSG`, `JOIN`, `PART`, `QUIT`, `NICK`), the
channel, and a `detail` field capped at 60 characters
(`modules/seen.py _DETAIL_MAX`).

For a channel message, `detail` is **the first 60 characters of what you said**.
For a part or quit it is your reason text. For a nick change it is the other
nick. Private messages to the bot are not recorded. The bot's own nick is
skipped. If you have opted out, nothing is recorded and any existing entry is
deleted at the next flush.

### Saved location (`locations.json`, module `location`)

`.regloc` stores the **raw string you typed**, keyed by nick
(`store.py - Store.loc_set()`). `.myloc` and `.regloc` reply into the channel
you ran them in, so running them in a channel shows your location to that
channel.

### Messages, notes, reminders

- **`tells.json`** (`modules/tell.py`) - sender nick, recipient nick, message
  text up to 350 characters, timestamp. Limits: 10 pending per recipient, 5 per
  sender.
- **`notes.json`** (`modules/notes.py`) - your nick, note text up to 200
  characters, timestamp. Limit 20 notes.
- **`reminders.json`** (`modules/remind.py`) - id, your nick, the channel you
  set it in, the reminder text, due time, creation time. Limit 10 pending, lead
  time between 30 seconds and 30 days.

### Steam registration (`steamids.json`, module `steam`)

`.regsteam` stores your lowercased IRC nick against a SteamID64. That is a link
between two identity namespaces and is the most directly identifying record the
bot keeps by consent.

### Shadow bans (`shadow_bans.json`)

An admin running `.shadow-ban <nick> [reason]` stores your lowercased nick and
the free-text reason they typed. This is an operator moderation record. It is
**not** reachable by `.forgetme`.

### Admin audit trail (`audit.log`)

`audit_log.py - AuditLog.record()` appends a hash-chained record for each
privileged action, holding the acting admin's nick and hostmask, the action,
its arguments, and the chain hashes. Records are append-only by design: erasing
one breaks verification. `.say` and `.act` records include the message text the
admin sent. If you are not an admin, your nick can still appear here inside the
`args` of an action taken about you, for example a shadow-ban.

### The application log (`internets.log`)

The bot's own log carries user-derived data at INFO level. The broadest source
is the command dispatcher, so read that one first:

- **Every command you run is logged.**
  `internets.py - IRCBot._handle_privmsg()` writes one INFO line per accepted
  command holding the command name, **the whole argument you typed**, your nick,
  your full hostmask, and the channel it came from or `(PM)`. That covers the
  text of your `.tell`, your `.note`, your `.remind`, and the raw string you
  gave `.regloc` - a private message to the bot is logged the same as a channel
  one. Only credentials are masked: `.auth` and `.deauth` arguments are replaced
  wholesale, and everything else is passed through
  `sender.py - redact_secrets()`, which masks the text after the first
  occurrence of `AUTHENTICATE`, `IDENTIFY`, `REGISTER`, `IDENT`, `OPER`, `PASS`,
  or `AUTH`, once per line. An argument containing none of those verbs is logged
  verbatim. See the note under `.pwn` in "What leaves the machine".
- `modules/location.py - cmd_regloc()` logs `regloc <nick> -> '<what you
  typed>' (<resolved place>)`, so a saved location is written twice.
- `modules/linktitle.py - LinkTitleModule.on_raw()` logs every URL it announces
  with the channel it announced it in, and every URL it skips without the
  channel. Neither line carries the nick that posted the URL, but the command
  lines above can supply it.
- `modules/weather.py` logs the resolved place name, country, and
  latitude/longitude of every weather, alerts, history, and nowcast query. No
  nick is on those four lines, but they are timestamped alongside lines that
  carry one.
- `internets.py - IRCBot._handle_membership()` logs
  `event=account_change nick=<nick> account=<account>` when the network reports
  that you logged in to or out of a services account, pairing your nick with
  your account name. This needs the `account-notify` capability, which the bot
  requests.
- `modules/channels.py` logs admin-requested joins and parts by nick, and the
  nick-to-channel pairs of its verification flow.
- `modules/privacy.py` logs `forgetme <nick>: removed [...]`, so the erasure
  request itself leaves a record naming you and what was deleted.

Unlike the state files and `config.ini`, `internets.log` is created with the
process umask and has no permission check. Nothing purges it. See item 15 in
[docs/known-issues.md](docs/known-issues.md), which predates the command-line
finding above and covers only the `regloc` and `linktitle` sites.

### The optional debug log (`[logging] debug_file`)

This one is off in the shipped template, and an operator can switch it on
without telling anyone. When `[logging] debug_file` is set to a path (or
`--debug-file` is passed), `botlog.py - _setup_logging()` adds a second
rotating file handler at DEBUG level **with no level filter attached**. The
main log only records DEBUG lines while debug is switched on; this file records
them always.

That matters because two DEBUG lines carry raw IRC traffic:
`internets.py - IRCBot.run()` logs every inbound line as `<< ...`, and
`sender.py - Sender._write_line()` logs every outbound line as `>> ...`. So the
debug file holds **every channel message, every private message to and from the
bot, and every command anyone runs**, not just the fraction other modules
choose to log. One regex masks the argument after a credential verb such as
`IDENTIFY`, on the inbound side through `internets.py - _redact_inbound()` and
on the outbound side through `sender.py - redact_secrets()`, and only for the
first match on a line. Nothing else is removed.

It rotates at the same size and depth as the main log, nothing purges it, and
`.forgetme` does not reach it.

### Held in memory only, lost on restart

Your current hostmask (`IRCBot._nick_hosts`), rate-limiter windows, the
geocoder result cache (24 hours, 1000 entries, holds the location strings
users queried), and `linktitle`'s URL dedup map, keyed by channel and URL with
a 300-second window. The 500 in that map is a cleanup trigger, not a limit:
crossing it sweeps out only the entries already older than the window, so the
map can hold more than 500 (`modules/linktitle.py - LinkTitleModule._mark()`).

---

## What leaves the machine

The bot is a relay between an IRC channel and public web APIs. Anything you
type into a networked command reaches a third party. The full verified
inventory of services, endpoints, and byte caps is in
[docs/integrations.md](docs/integrations.md). Summarised by what is sent:

| What is sent | Where it goes |
| --- | --- |
| A location string, then its coordinates | Nominatim, Zippopotam, weather providers |
| A URL you pasted in channel | The site itself, fetched automatically |
| An IP or hostname you asked about | DNS, RDAP, ip-api, and reputation feeds |
| Free-text queries you typed | Search, translation, and lookup APIs |
| Third-party account handles | Last.fm, Steam, Twitch, IdleRPG |
| The operator's contact identifier | Every outbound module request |

Specifics worth stating plainly:

- **Weather.** A `.w`, `.forecast`, `.alerts`, or similar command sends your
  location text to a geocoder (`nominatim.openstreetmap.org`, or
  `api.zippopotam.us` for postal codes), then sends the resulting
  latitude/longitude to whichever weather provider the dispatcher selects.
  Which provider that is depends on the operator's configuration and on live
  provider health, so the same query can reach a different company on a
  different day. 32 provider packages ship. **Twelve of them need no
  credential and register on a keyless install**: `currentuvindex`, `eccc`,
  `gdacs`, `metno`, `nasapower`, `nifc`, `noaa_coops`, `nws`, `openmeteo`,
  `pollendotcom`, `sunrisesunset`, `swpc`
  (`weather_providers/__init__.py - configure()`, and the `requires_key` flag
  on each provider class). The `[weather_providers] provider_priority` setting
  is an ordering preference, not an allowlist: `configure()` appends every
  unlisted provider after the listed ones, so leaving a provider out of it does
  not stop it registering. Each of the twelve is called for the capabilities it
  answers and is given your coordinates, with one exception verified here: the
  space-weather provider fetches two fixed global NOAA files and does the
  location match locally (`weather_providers/swpc/space_weather.py - fetch()`),
  so your coordinates are not in that request. `pollendotcom` goes further and
  reverse-geocodes your coordinates to a US ZIP through Nominatim before it
  queries.
- **Link titles.** With the `linktitle` module loaded, the bot fetches URLs
  posted in a channel it watches, with no command and no confirmation. The
  target site sees the request. It is not literally every URL
  (`modules/linktitle.py - LinkTitleModule._should_skip()`): at most the first
  three URLs in one message are considered, a fetch in a channel silences that
  channel for 3 seconds, the same URL in the same channel is skipped for 300
  seconds, and localhost-style hosts plus a list of image, audio, video,
  archive and document extensions are skipped outright. Messages that start
  with the command prefix are ignored. What is left is fetched without asking.
  For YouTube links it additionally queries YouTube's oembed endpoint, or the
  YouTube Data API if the operator configured a key.
- **Network lookups.** `.ip`, `.rep`, `.dns`, `.whois`, `.asn`, `.headers`,
  `.ssl`, `.tcp`, `.down`, `.ipinfo` genuinely contact the target you name. The
  bot's own IP appears in that target's logs. `.ipinfo` and three other
  integrations use plaintext `http://`, so the query is visible to the network
  path.
- **The operator's contact identifier.** The `weather_user_agent` value is
  spliced into the `User-Agent` header of most outbound module requests. It
  identifies the operator, not the user. Only the geocoder checks it:
  `modules/geocode.py - _ua_has_contact()` refuses to call Nominatim unless the
  string contains an `@` with a dot after it, or `http://`, or `https://`. Every
  other module sends whatever is configured, unchecked.
- **`.pwn` sends almost nothing, and then logs everything.** Over the network it
  is exactly what it claims: `modules/secinfo.py - _pwn_sync()` hashes the
  password locally and sends only the **first five hex characters** of the SHA-1
  digest, so the password and the full hash never reach the breach service. The
  command also refuses to run in a channel. But the dispatcher logs the argument
  of every command before running it, and a password is not a credential *verb*,
  so nothing redacts it: a `.pwn` in a private message writes your **plaintext
  password** into `internets.log`, where nothing purges it, `.forgetme` cannot
  reach it, and the file's permissions are whatever the operator's umask gave
  it. Treat any password you have typed at this bot as disclosed to its
  operator.

Four things the bot does **not** send: the `.fx` conversion amount (only the
currency codes go, and the arithmetic is local), the `.qr` payload (the bot
builds a link and makes no request), the full password or hash in `.pwn`, and
your IRC nick and channel name - no module in this repository puts either into
an outbound request.

`.qr` has a second half worth knowing: the link the bot prints carries your
payload inside its query string, so anyone who opens that link hands the payload
to `api.qrserver.com`. The bot's restraint does not extend to the reader.

Everything already sent upstream is outside every control in this document.
`.forgetme` cannot recall it.

---

## How long things are kept

Defaults as shipped. An operator can change several of these, and one of them
can be switched off entirely. Full specification, including what verification
looks like, in [docs/data-retention.md](docs/data-retention.md).

| Data | Default retention |
| --- | --- |
| `users.json` rows | 90 days after last seen |
| `seen.json` entries | 180 days |
| `tells.json` entries | 30 days, or until delivered |
| `reminders.json` | Until it fires or is cancelled |
| `locations.json` | Indefinite |
| `notes.json` | Indefinite |
| `steamids.json` | Indefinite |
| `shadow_bans.json` | Indefinite |
| `audit.log` | Indefinite |
| `internets.log` | 4 files of 5 MiB each |

Three carve-outs:

1. **Opting out makes your `users.json` row permanent.** `Store._prune_users()`
   skips any row carrying `opted_out: true`, so the preference outlives the
   90-day window. That is deliberate - otherwise the bot would silently resume
   tracking someone who asked it not to - but it does mean an opt-out is itself
   a record kept without limit until you run `.forgetme`.
2. **`seen` retention can be turned off.** `[seen] max_age_days = 0` disables
   pruning entirely (`modules/seen.py - _prune_stale()`). The shipped
   `config.ini.example` has no `[seen]` section at all, so the 180-day default
   applies unless the operator adds one.
3. **The audit log is never deleted.** It rotates to a new segment at 5 MiB
   (`audit_log.py - _MAX_BYTES`) and every segment is kept.

---

## Your controls

All four commands come from the `privacy` module. Two of them are **PM-only**:
`.privacy` and `.forgetme` refuse to run in a channel
(`modules/privacy.py - PrivacyModule._require_pm()`), because answering in a
channel would publish the data the command exists to protect. Message the bot
directly for those: `/msg <botnick> .privacy`.

`.optout` and `.optin` work in a channel. Their reply is a NOTICE addressed to
you rather than a channel message, so running them in public does not echo your
data to the channel, but the command you typed is visible there like any other
line, and it is logged like any other line. None of the four is rate-limited;
see [docs/command-reference.md](docs/command-reference.md).

The command prefix shown here as `.` is configurable.

### `.privacy`

Privately shows what the bot holds about you: your saved location, your current
hostmask as the bot sees it, the channels you are tracked in with first-seen
and last-seen stamps, and your opt-out status
(`modules/privacy.py - cmd_privacy()`).

It reports the core datasets only. It does not enumerate your tells, notes,
reminders, or Steam registration.

The channel list is narrower than what is stored. `cmd_privacy()` walks the
channels the bot is **currently in** (`bot.active_channels`), so a row held for
you in a channel the bot has since left is not shown, and neither is the
sentinel row that `.optout` creates in the synthetic `"*"` channel
(`store.py - Store.set_opt_out()`). `.forgetme` erases those rows even though
`.privacy` did not disclose them: `store.py - Store.user_purge()` works across
every stored channel, not just the active ones.

### `.optout` and `.optin`

`.optout` sets a flag on your tracking records
(`store.py - Store.set_opt_out()`). Verified against every consumer in the
repository, the flag gates exactly two things:

- Another user cannot look up your saved location with `.w -n <yournick>`
  (`modules/weather.py`).
- The `seen` module stops recording you, answers "never seen" if someone asks,
  and deletes your existing entry at its next flush (`modules/seen.py`).

It does **not** stop core channel tracking. Rows keep accruing in `users.json`
with the flag riding on them. Opt-out limits disclosure; it does not stop
collection. `.forgetme` is the collection remedy.

If you have never been tracked, `.optout` **creates** a record for you: a
sentinel row in a synthetic `"*"` channel so the preference survives a restart.
That row is prune-immune, so it persists until you run `.forgetme`.

`.optin` clears the flag.

### `.forgetme`

`modules/privacy.py - cmd_forgetme()` erases, in this order: your saved
location, your opt-out bookkeeping, every `users.json` row for your nick
(hard-deleted immediately, not queued for the prune cycle), and then everything
each loaded module holds, by calling `BotModule.forget(nick)` on all of them.

Modules that implement erasure: `seen` (your entry), `tell` (both messages
queued **for** you and messages **you sent** to others), `notes` (all of them),
`remind` (all pending, and their timers are cancelled), `steam` (your
registration). Every other module returns 0 because it stores nothing about
you.

### What `.forgetme` does not reach

This list is verified, not cautionary boilerplate.

- **The application log.** `internets.log` holds one line per command you ever
  ran, with your nick, your hostmask, and the text you passed it; your `.regloc`
  string a second time; the URLs `linktitle` announced, with their channel; and
  the record of the `.forgetme` run itself. Nothing purges it. This is the
  largest gap in `.forgetme` by volume.
- **Rotated log segments.** `internets.log.1` through `.3` are outside every
  erasure path.
- **The debug log, if the operator turned it on.** Everything you said in a
  channel the bot sits in, and everything you sent it privately, is in that
  file verbatim.
- **The audit log.** `audit.log` and its rotated `audit.log.<stamp>` segments
  are append-only and hash-chained. If you are an admin, your nick and hostmask
  are permanent there.
- **Backup files.** `store.py - Store._write()` copies each state file to
  `<name>.json.bak` before every write, so the previous `users.json` contents
  survive in `users.json.bak` until the next successful write replaces it. That
  `.bak` is written without a `chmod` and takes umask-default permissions,
  typically world-readable (item 13, known issues).
- **Quarantined files.** A state file that fails its integrity check is renamed
  to `<name>.json.corrupt.<timestamp>` and left in place with its contents
  intact.
- **Shadow-ban entries.** `shadow_bans.json` is an operator moderation record
  and is untouched.
- **Anything already sent to a third party.** See "What leaves the machine".
- **Operator backups.** Whatever your operator's backup policy captured.

Two behavioural notes on `.forgetme` itself:

- It reports what it deleted. If you were never tracked at all, a defect in the
  ordering of its steps makes it report "tracking in 1 channel(s) (erased now)"
  rather than "no stored records" (item 4, known issues). Nothing extra is
  retained; the count is wrong, not the erasure.
- Erasure is only as durable as the next successful disk write. No writer in
  this codebase calls `os.fsync()`, so a host crash immediately after an
  erasure can lose the write and restore the previous file contents.

---

## If you operate this bot

You are running the software yourself, on your own machine, against your own
IRC network. Nothing in this project phones home, and the authors of this
repository hold none of your users' data. That means the decisions and the
responsibility are yours.

What you have to decide, at minimum:

1. **Whether to load the collecting modules at all.** `seen`, `tell`, `notes`,
   `remind`, and `steam` each keep their own file; `location` writes through
   the core store; `linktitle` keeps no file but logs the URLs it saw. All
   seven are opt-in per deployment. A bot with none of them loaded still tracks
   channel presence in `users.json`, which is core behaviour you cannot switch
   off from config.
2. **Whether to load `privacy`.** Add it to `[bot] autoload`. Without it your
   users have no way to see or erase what you hold. See the notice at the top.
3. **Retention windows.** `[bot] user_max_age_days` (default 90, floored at 1
   so a misconfiguration cannot wipe the dataset) and `[seen] max_age_days`
   (default 180, `0` disables pruning). The template ships no `[seen]` section,
   so add one if you want to change it.
4. **Log handling.** `internets.log` is created with your umask, holds data
   `.forgetme` cannot reach, and rotates by size only. At the shipped INFO level
   it already records every command every user runs, with their nick, hostmask,
   and full argument text, which puts it closer to the debug file than its level
   suggests and makes any password typed at `.pwn` readable to whoever can read
   the file. Setting `UMask=0077` in the service unit is the immediate
   mitigation. Decide how long you keep rotated segments and who can read them.
   Setting `[logging] debug_file` is a
   much larger decision than it looks: it records every IRC line in and out,
   permanently and regardless of the debug level, including private messages.
   Leave it unset unless you are debugging, and delete the file afterwards.
5. **Backups.** `.bak`, `.corrupt.*`, rotated logs, and audit segments all
   carry the same personal data as the live files, some at weaker permissions.
   An erasure request does not reach your backups unless you make it.
6. **Telling your users.** Publish, somewhere your users can find it, who
   operates this bot and how to contact you. This document cannot do that for
   you.
7. **Admin discipline.** `.fingerprint <nick>` assembles a cross-referenced
   profile of one user from hostmask, channels, `seen`, `tells`, `notes`, and
   audit mentions, and it writes **no audit record** of having been run. Decide
   who holds admin.

Operational detail for all of the above is in
[docs/state-and-persistence.md](docs/state-and-persistence.md),
[docs/logging-and-auditing.md](docs/logging-and-auditing.md), and
[docs/data-retention.md](docs/data-retention.md).

---

## Questions, corrections, complaints

**About the software** - how it behaves, a bug in this document, a data path
that is not described here: open an issue at
<https://github.com/brandontroidl/Internets/issues>. Security-sensitive reports
should follow `SECURITY.md` at the repository root instead of the public
tracker.

**About a specific bot on a specific network** - what a particular operator
holds about you, or a request to erase it: contact **that operator**. This
project does not run any bot instance, cannot see any instance's data, and
cannot act on a request about one. Operators: publish your own contact route,
because there is no address in this document that reaches you.

---

## Status of this document

Every claim above was checked against the source in this repository, and each
one names the symbol it came from so you can check it too. Where the code has a
verified defect that affects privacy, this document states the defect rather
than the intent, and links to
[docs/known-issues.md](docs/known-issues.md).

This notice covers the software as published. It says nothing about what any
particular operator does with it.
