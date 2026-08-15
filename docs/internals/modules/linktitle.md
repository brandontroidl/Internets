# linktitle.py - passive URL title announcer for channel messages

## Purpose

`LinkTitleModule` registers NO commands (`COMMANDS = {}`); it watches every
channel PRIVMSG via `on_raw`, extracts up to 3 http(s) URLs, and announces the
page title (`Link <title>`), with a richer YouTube path. Because it fetches
URLs chosen by arbitrary channel users, SSRF defense is the module's central
security property. Base contract: [base](base.md); SSRF machinery:
[_netsafe](_netsafe.md).

## Trigger filtering (`LinkTitleModule.on_raw()` + `_should_skip()`)

A URL is announced only when ALL hold:

- the line is a PRIVMSG to a channel (`#&+!` prefixes) from someone other than
  the bot itself;
- the text is not a bot command (prefix check) and not a CTCP (`\x01`);
- per-channel cooldown of 3 s (`_COOLDOWN`) has elapsed;
- the same channel+URL pair was not announced in the last 300 s
  (`_DEDUP_TTL`, keyed `channel\0url`, dict pruned above 500 entries in
  `_mark()`);
- the host is not in `_IGNORE_HOSTS` (localhost literals - cosmetic; the real
  block is `_netsafe`) and the path extension is not in `_IGNORE_EXTS`
  (images/media/archives/binaries - avoids pointless fetches).

Each surviving URL spawns an `_announce()` task (`asyncio.ensure_future` with
a done-callback that logs failures, `_task_done()`). Shadow-banned nicks never
reach `on_raw` (fanout skip in `internets.py - _handle_line()`).

## Fetch paths (`LinkTitleModule._announce()`)

1. YouTube (`_RE_YT` matches watch/short/youtu.be forms, 11-char id):
   - with a configured key: Data API v3
     (`https://www.googleapis.com/youtube/v3/videos`,
     `part=snippet,contentDetails,statistics`) via `base.fetch_json`, 8 s
     timeout - title, channel, duration (`_fmt_duration` parses ISO-8601
     `PT#H#M#S`), views, likes (`modules/linktitle.py - _fetch_yt_api()`);
   - keyless or on API failure: the free oembed endpoint
     (`https://www.youtube.com/oembed`) for title + channel
     (`_fetch_yt_oembed()`);
   - if both fail, falls through to the generic title fetch.
2. Generic (`_fetch_title_sync()`): `_netsafe.safe_open("GET", url,
   follow_redirects=True, timeout=8)` - scheme allowlist, metadata-host block,
   public-IP validation, and DNS pinning at EVERY redirect hop, so a
   user-posted URL (or a redirect chain it starts) can never reach an internal
   address. Content-type must contain `text/html` or `application/xhtml`;
   body read is capped at 768 KiB (`_TITLE_MAX_BYTES`) via
   `resp.raw.read(cap, decode_content=True)`; `_TitleParser` (an
   `html.parser.HTMLParser`) prefers `og:title` over `<title>`, collapses
   whitespace, unescapes entities.

All three workers run via `asyncio.to_thread`; every emitted string passes
`base.strip_ctrl` (title 300 chars, channel 80), so a hostile page title
cannot inject IRC formatting or control bytes into the bot-attributed
announcement.

## Integration / configuration

| Item | Value |
|---|---|
| YouTube key | `base.cred(cfg, "youtube_key", "youtube", "youtube_key")` - secret store (`INTERNETS_YOUTUBE_KEY` env override) with legacy `[youtube] youtube_key` config fallback; OPTIONAL - keyless operation uses oembed |
| UA | reuses `weather_user_agent` (same `cred` pattern, default `Internets/1.0`) |
| Gating | none - the module has no `is_configured` override and works keyless |

Privacy of outbound traffic: the posted URL is fetched with the bot's UA; for
YouTube, the video id (and the operator's API key) go to Google. No nick or
channel information leaves the host.

## State

In-memory only: `_last` (per-channel cooldown stamps) and `_seen_urls`
(dedup), both `time.monotonic()`-based. Nothing persisted.

## Failure behavior

Fail-quiet by design: `SSRFBlocked`, transport errors, non-HTML content,
oversized bodies, unparseable HTML, and empty titles all end in silence (a
passive announcer must not spam failure lines into channels). Errors are
logged at debug; task exceptions surface via `_task_done` at warning.

## Findings

- questionable | linktitle.py - LinkTitleModule.on_raw() | every announced AND
  every cooldown-skipped URL is logged at INFO with channel name
  (`log.info("linktitle: announcing %s in %s", ...)`), writing user browsing
  activity into the bot log; debug level would match the module's other
  logging and the bot's PII posture.
- questionable | linktitle.py - _fetch_title_sync() | the 768 KiB cap bounds
  memory but a `<title>` past the cap in a huge document is silently lost;
  acceptable trade-off, noted for completeness.
- test-gap | linktitle.py - LinkTitleModule | no `tests/test_linktitle*`
  exists; URL extraction, dedup/cooldown, the YouTube fallback chain, and the
  title parser are untested (`tests/test_netsafe.py` covers only the SSRF
  layer).
