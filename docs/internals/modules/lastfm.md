# lastfm.py - Last.fm profile and now-playing lookup

Keyed wrapper around the Last.fm (audioscrobbler) REST API. One class
`LastfmModule` on the shared [base](base.md) contract; blocking helper
`_lookup_sync()` run via `asyncio.to_thread` plus two small formatters.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.lastfm <username>` | `lastfm.py - LastfmModule.cmd_lastfm()` | `.lastfm RJ` | One line: bold username [realname, country], play count since registration date, profile URL, then either `Now playing artist - track` or `Latest artist - track (2h 5m ago)` |

Only the first whitespace-separated token of the argument is used
(`arg.strip().split()[0]`).

## Integration

Two sequential calls to `https://ws.audioscrobbler.com/2.0/`
(`lastfm.py - _lookup_sync()`), both via `modules.base - fetch_json()`
(256 KB cap), timeout 10 s each:

1. `method=user.getinfo` - profile fields. A JSON body containing `error`
   maps to the API's own message (stripped) or `user not found`.
2. `method=user.getrecenttracks&limit=1` - most recent or currently playing
   track. Last.fm returns a dict instead of a list when there is exactly one
   track; the code normalizes that (`if isinstance(tracks, dict)`).

Formatting details:

- `_timeago()` renders seconds/minutes/hours-minutes/days-hours from the track's
  unix timestamp; `_fmt_thousand()` adds comma separators to the play count.
- `nowplaying` is signalled by `@attr.nowplaying == "true"`; otherwise the track
  needs a `date` field to be shown at all.
- All third-party strings pass through `strip_ctrl`.
- Privacy: sends the queried username and the API key; nothing about the IRC
  caller.

## Configuration

- Key: `cred(cfg, "lastfm_key", "lastfm", "lastfm_key")` -
  `INTERNETS_LASTFM_KEY` env var, then secret_store, then legacy
  `[lastfm] lastfm_key`.
- Keyless: `is_configured()` False (hidden from `.help`); direct invocation
  replies "Last.fm API key not configured".

## Failure behavior

One broad `except Exception` around both calls: any transport, size-cap, JSON,
or key error (`data["user"]` is a direct index) returns `lookup failed`. A
failure in the second (recent-tracks) call therefore discards the already
fetched profile data instead of degrading to profile-only output. Rate limit
check as in the rest of the batch.

## Security notes

HTTP via `fetch_json`; output sanitized per field with `strip_ctrl`.

## Findings

- questionable | lastfm.py - `_lookup_sync()` | a failure in the second
  (recenttracks) fetch throws away the successful user.getinfo result and
  reports a total `lookup failed`; degrading to profile-only output would be
  strictly better.
- questionable | lastfm.py - `_lookup_sync()` | `log.warning` on failure embeds
  the request URL including `api_key=<key>` in the bot log (log-only leak;
  batch-wide pattern, see imdb.md).
