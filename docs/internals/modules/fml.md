# fml.py - random FMyLife quote (HTML scraper)

The one scraper in the fun-module batch (128 lines): fmylife.com has no public
JSON API, so this fetches the site's `/random` HTML page and extracts article
bodies by regex. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.fml` | `.fml` (args ignored) | `[fml #<id>] Today, ... FML` |

## Integration

`_lookup_sync()` (blocking, via `asyncio.to_thread`):
`GET https://www.fmylife.com/random` with the shared UA and
`Accept: text/html`, `timeout=15`, `stream=True` inside a `with` block (the
comment notes an unclosed streamed response leaks the socket/FD). Body capped
at 512 KB via `r.raw.read(512*1024 + 1)` - a page-sized cap, the page is
normally ~200 KB - with an oversize reply instead of parsing.

Extraction (`_FML_ARTICLE`): the site's 2024-2025 Tailwind markup renders each
article with two anchors to the same `/article/<slug>_<id>.html` URL; the
regex anchors on the `block text-blue-500` class signature that only the body
anchor carries, capturing the numeric article id and the inner HTML. Matches
are tag-stripped, entity-unescaped, and whitespace-collapsed (`_strip_tags`),
then filtered to real user submissions by the universal "Today, ... FML"
shape (case-insensitive `startswith("today")`), which drops editorial
compilation articles the `/random` page occasionally serves. If no candidate
survives the filter, the first raw match is used as a visible fallback and a
warning is logged. One candidate is picked via `random.SystemRandom`, clipped
to 400 chars (ellipsis at 397).

## Configuration

None. Keyless; base-default `is_configured()`. `on_load()` resolves the
shared `weather_user_agent` credential (secret store,
`INTERNETS_WEATHER_USER_AGENT` env override, config `[weather] user_agent`
fallback).

## Failure behavior

Zero regex matches -> `could not parse FML page - site layout may have
changed` (the expected symptom of the next redesign). A single broad
`except Exception` covers transport, HTTP status, and decode errors ->
`fmylife.com is temporarily unavailable` plus a logged warning. Nothing
propagates to the dispatcher.

## Security notes

Hardcoded HTTPS host, no user input in the request, no nick/channel sent.
Scraped quote text is third-party content and passes through `strip_ctrl`
before hitting the channel; the article id is regex-guaranteed digits, so
splicing it raw is safe (both facts noted in the source comment and verified
against `_FML_ARTICLE`). `cmd_fml` checks `bot.rate_limited(nick)` first.

## Findings

- questionable | fml.py - `_FML_ARTICLE` | Extraction is coupled to a
  specific Tailwind class string in third-party markup; a site redesign
  silently breaks it (degrading to the parse-error reply). Inherent to
  scraping and self-documented, but the most fragile integration in the
  batch.
- test-gap | fml.py - `_lookup_sync()` | No tests; the regex, the "today"
  filter, and the fallback path have no fixtures, so a layout change is only
  detectable live.
