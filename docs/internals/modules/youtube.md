# youtube.py - YouTube video search

Keyed wrapper around the YouTube Data API v3. One class `YoutubeModule` on the
shared [base](base.md) contract; blocking helper `_search_sync()` via
`asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.yt <search>` (alias `.youtube`) | `youtube.py - YoutubeModule.cmd_yt()` | `.yt never gonna give you up` | `YouTube <title> | https://www.youtube.com/watch?v=<id> (m:ss) | Views N | [+] N likes` |

## Integration

Two sequential calls (`youtube.py - _search_sync()`), both via
`modules.base - fetch_json()` (256 KB cap), timeout 10 s:

1. `GET https://www.googleapis.com/youtube/v3/search` with
   `part=snippet&order=relevance&type=video&maxResults=1&q=<query>&key=<key>` -
   picks the single top result. Empty `items` maps to `no results for '<query>'`.
2. `GET https://www.googleapis.com/youtube/v3/videos` with
   `part=contentDetails,statistics&id=<videoId>` - duration, views, likes. If
   this returns nothing, the reply degrades to title + link only (unlike
   lastfm, partial success is preserved here).

- `_fmt_duration()` parses ISO 8601 `PT#H#M#S` via `_DURATION_RE`; non-matching
  values (e.g. `P0D` on live streams) render as `?`.
- A hidden like count is absent from `statistics`, so it displays as `0` -
  indistinguishable from a real zero.
- Privacy: sends the search query and API key only.

## Configuration

- Key: `cred(cfg, "youtube_key", "youtube", "youtube_key")` -
  `INTERNETS_YOUTUBE_KEY` env var, then secret_store, then legacy
  `[youtube] youtube_key`.
- Keyless: `is_configured()` False; direct invocation replies
  "YouTube API key not configured".

## Failure behavior

Single broad `except Exception` returns `search failed` (covers transport,
size cap, JSON, and the direct indexes `items[0]["id"]["videoId"]` /
`["snippet"]["title"]`). Rate limit check via `bot.rate_limited()` before the
thread spawn.

## Security notes

HTTP via `fetch_json`; title stripped with `strip_ctrl`. The video id is
spliced into the reply URL unstripped, but it originates from Google's API
response and the sender strips CR/LF/NUL anyway.

## Findings

- questionable | youtube.py - `_search_sync()` | `log.warning` on failure embeds
  the request URL including `key=<key>` in the bot log (log-only leak;
  batch-wide pattern, see imdb.md).
- questionable | youtube.py - `_search_sync()` | hidden like counts render as
  `0 likes` because `statistics.likeCount` is simply absent; a `?` would be
  more honest.
