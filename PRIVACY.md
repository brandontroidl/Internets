# Privacy Policy — Internets IRC Bot

This document describes the personal data the **Internets** IRC bot
collects, how long it retains it, and what controls users have over
their own data. It is written to be read by end users (people typing
`.help` in a channel), not just lawyers.

If something here is unclear or you believe the bot is mishandling your
data, see [Filing a deletion or correction request](#filing-a-deletion-or-correction-request)
below.

## What the bot collects

The bot stores the following on disk in JSON files alongside the source
tree:

| Field            | Where             | Example                          | Why                               |
|------------------|-------------------|----------------------------------|-----------------------------------|
| Lowercased nick  | `locations.json`  | `"alice"`                        | Key for saved location lookup     |
| Free-text location (often a ZIP) | `locations.json` | `"94110"` or `"Berlin"` | Used by `.weather`, `.myloc`, etc.|
| Nick (display)   | `users.json`      | `"Alice"`                        | Last known capitalisation         |
| Hostmask         | `users.json`      | `"~alice@host.example"`          | Tie-breaker for nick collisions   |
| `first_seen`     | `users.json`      | ISO-8601 UTC                     | When we first saw you in a channel|
| `last_seen`      | `users.json`      | ISO-8601 UTC                     | When we last saw activity         |
| Channel name     | `channels.json`   | `"#example"`                     | Which channels the bot rejoins    |

The bot does **not** collect or store:

- Message content beyond the command line itself (and command lines
  are logged with `[REDACTED]` arguments for `.auth` / `.deauth`).
- Other users' hostmasks revealed to you via any command.
- Real names, email addresses, or any data outside what IRC itself
  broadcasts.

Credentials (NickServ password, admin password hash, API keys) live in
a separate secret store (`secrets.ini` with `0600` permissions, or an
OS keyring) — see `secrets.ini.example`.

## Where the data lives

- `locations.json`, `users.json`, `channels.json` — JSON files in the
  bot's working directory. The bot opens them with `0600` permissions
  so only the user account running the bot can read them.
- In-memory copies are flushed to disk roughly every 30 seconds.
- Backups, if any, are the operator's responsibility — there is no
  built-in off-host replication.

## Retention

User-tracking entries in `users.json` are automatically pruned when
`last_seen` is older than the configured retention window. The default
is **90 days** (`user_max_age_days` in `config.ini`). Pruning runs on
every disk flush, so a row that ages out vanishes within ~30 seconds of
crossing the threshold.

`locations.json` entries are **not** time-pruned — they only go away
when the user runs `.delloc` or `.forgetme`, or when an operator deletes
the file. Saved locations are user-supplied data; we don't expire them
without a clear signal.

## User controls

All privacy commands work in a private message (`/msg <bot> .privacy`).
The `.privacy`, `.forgetme`, etc. commands refuse to answer in a
channel so your saved location and hostmask aren't echoed in public.

| Command           | Effect                                                         |
|-------------------|----------------------------------------------------------------|
| `.privacy`        | Privately lists everything the bot has stored about you, including your current hostmask as the bot sees it. Never reveals data about anybody else. |
| `.forgetme`       | Deletes your saved location and schedules your user-tracking rows for removal on the next prune. Also clears any opt-out flag so a subsequent `.optin` is honest. |
| `.optout`         | Marks your nick as opted out of tracking. **Limitation: see follow-ups below.** |
| `.optin`          | Undoes a prior `.optout`.                                      |
| `.delloc`         | Deletes only your saved location (older command, kept for compatibility). |

## Known limitations / follow-ups

These are tracked in `CHANGELOG.md`; flagged here so users know what
the bot does *not yet* do:

1. **`.forgetme` doesn't yet hard-erase tracking rows.** Until the
   data-store layer adds a `user_purge(nick)` API, `.forgetme`
   refreshes `last_seen` and relies on the 90-day pruner to sweep the
   rows. The rows are no longer "live" in any practical sense, but
   they remain on disk briefly. Track in the Robustness wave-2 work
   for the store module.
2. **`.optout` is recorded but not yet honoured by every module.**
   The flag is stored under a reserved `__optout__:<nick>` key in
   `locations.json` as an interim, because the store schema doesn't
   yet have a dedicated opt-out column. `modules/location.py` will
   read the new column once it lands; until then, opted-out users
   should also run `.forgetme` to erase existing records.
3. **No automated export.** GDPR Article 15 (right of access) is
   satisfied by `.privacy`; there is no machine-readable bulk export.
   Ask the operator (see below).

## Third-party data flow

The only third-party service that receives user-supplied location
strings is **Nominatim** (OpenStreetMap's geocoder), used to turn
`"94110"` or `"Berlin"` into latitude/longitude for `.weather` and
related commands. Nominatim's usage policy:
<https://operations.osmfoundation.org/policies/nominatim/>. Briefly:
queries are logged, must include a User-Agent identifying the operator,
and must not be issued faster than 1/second. The bot honours those
constraints; the location string you supply is the only personal data
sent.

Other API integrations (Last.fm, Twitch, Steam, YouTube, IMDB, etc.)
send only the query you typed — never your nick, hostmask, or saved
location — and are governed by those services' own policies.

## Filing a deletion or correction request

For self-service deletion, run `.forgetme` in a PM with the bot. That
is the fastest path and requires no operator involvement.

For anything `.forgetme` can't handle (correcting a stored field,
deleting another user's data on legal grounds, bulk export, etc.),
contact the bot operator. The contact channel and PGP key (if any)
are listed in `SECURITY.md` at the repository root, alongside the
private vulnerability-reporting instructions.

If `SECURITY.md` is missing from your deployment, fall back to the
maintainer address in `pyproject.toml`.

## Recommended autoload

Operators are encouraged to add `privacy` to the autoload list in
`config.ini` so the user-facing commands are available out of the box:

```
autoload = ...,privacy
```

(`config.ini` is owned by the deployment-config track; this doc only
documents the recommendation.)
