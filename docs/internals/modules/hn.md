# hn.py - Hacker News top story (`.hn`)

Keyless two-request wrapper around the official Firebase HN API. Base contract:
[base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.hn` | `[rank]` (1-30, default 1) | The rank-N top story: title, points, comment count, author, URL, one line. |

Handler: `hn.py - HnModule.cmd_hn()`. Non-numeric arg replies with usage;
out-of-range rank (validated 1-30 in the handler) replies "rank must be 1-30".
Rate-limit gate first, then `_fetch_sync` in `asyncio.to_thread`.

## Integration

Two sequential GETs via the module-local inline stream+cap `hn.py - _get_json()`
(timeout 10 s, cap `_MAX_BODY_BYTES` = 64 KiB, None on any error/over-cap):

1. `https://hacker-news.firebaseio.com/v0/topstories.json` - array of ~500 story
   ids.
2. `https://hacker-news.firebaseio.com/v0/item/<id>.json` - metadata for the
   selected id.

`_fetch_sync()` re-checks rank against the actual id-list length (belt to the
handler's 1-30 suspenders). Ask/job posts without a `url` field fall back to the
`news.ycombinator.com/item?id=` permalink. No auth, no user data sent.

## Configuration

Keyless; `is_configured()` unconditionally True. `on_load()` reads only
`weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`).

## Failure behavior

Top-stories fetch failure/malformed -> "Hacker News unavailable"; item fetch
failure -> "Hacker News item fetch failed". `_get_json` swallows all
transport/JSON errors into None, so nothing raises to the dispatcher.

## Security notes

Streamed size-capped reads; the only user input (rank) is a validated small int
selecting into the id array, never interpolated into a URL as a string beyond the
numeric id; title/author/URL pass `strip_ctrl` (cap 400) before IRC.

## Findings

- test-gap | `hn.py` | No test file exercises this module (rank validation, id-list
  bounds, permalink fallback).
