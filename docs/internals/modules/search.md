# search.py - web and image search (DuckDuckGo scrape + optional Brave API)

Two-provider search (228 lines): keyless DuckDuckGo HTML scraping always works;
a Brave Search API key upgrades web search and enables image search. Base
contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.sw` / `.g` | `.sw <query>` | `[Brave\|DuckDuckGo] **title** - url \| description` |
| `.si` / `.gi` | `.si <query>` | `[Brave Image] **title** - url \| WxHpx` (key required) |

Both handlers: usage line on empty arg, then rate-limit gate, then
`asyncio.to_thread` into the sync dispatcher.

## Integration

Three provider functions, one dispatcher each for web and image:

- `_ddg_web()` - `POST https://html.duckduckgo.com/html/` with form data
  `q=<query>&kl=us-en`, shared UA, 10 s timeout, streamed with an inline
  512 KB cap (the response is HTML, so `fetch_json` does not apply). Results
  are extracted by regex: `_DDG_RESULT_RE` pulls the first
  `<a class="result__a">` link+title, `_DDG_SNIPPET_RE` the first snippet, and
  `_extract_ddg_url()` unwraps DuckDuckGo's `uddg=` redirect parameter
  (`unquote`d). Title/snippet pass through `_strip()` - tag removal +
  `html.unescape` + `strip_ctrl` last (the comment notes `&#1;` un-escapes to
  a raw C0 byte, so `strip_ctrl` must be the final step). Snippet clipped to
  200 chars.
- `_brave_web()` - `GET https://api.search.brave.com/res/v1/web/search`
  `?q=<query>&count=3` via `fetch_json` (256 KB cap) with the key in the
  `X-Subscription-Token` header. First result formatted like DDG.
- `_brave_image()` - `GET https://api.search.brave.com/res/v1/images/search`,
  same auth; formats title, URL (falling back to `source`), and
  `properties.width x height`.

Dispatch (`_web_sync()`): Brave first when a key is present; on any Brave
exception it logs a warning naming the exception type and falls back to
DuckDuckGo; if DDG also fails it returns `search failed for '<query>'`. The
docstring makes the warning-level logging deliberate: an operator must be able
to distinguish a bad Brave key from a DDG 429 or markup drift. `_image_sync()`
has no fallback - keyless invocations get a config pointer message
(`image search requires a Brave API key - see [search] in config.ini`).

## Configuration

- Optional secret `brave_key`: `cred(cfg, "brave_key", "search", "brave_key")` -
  env override `INTERNETS_BRAVE_KEY`, else `config.ini` `[search] brave_key`
  (placeholder-guarded). `on_load()` logs the active provider set.
- `is_configured()` is not overridden (base default `True`) - correct, since
  web search works keyless; only `.si`/`.gi` degrade to the hint message.
- Shared `weather_user_agent` credential supplies the UA for both providers.

## Failure behavior

Provider functions log at debug and re-raise; the `_*_sync` dispatchers own the
catch-log-degrade policy (warning + fallback or flat failure string). Handlers
never see an exception. No results is a per-provider specific message, not a
failure.

## Security notes

- Query plus UA (plus the Brave key, to Brave only) is all that leaves the
  machine; no nick/channel. The key travels in a request header, never in the
  URL or logs.
- DDG result text is attacker-influenceable web content; the
  unescape-then-strip ordering in `_strip()` is the injection defense, and the
  redirect unwrap runs through `strip_ctrl` before display.
- Both hosts hardcoded; no SSRF surface. Inline 512 KB HTML cap bounds a
  tampered or bloated response.

## Findings

- questionable | search.py - `_DDG_RESULT_RE` / `_DDG_SNIPPET_RE` | Web search
  correctness for the keyless default rests on regexes over DuckDuckGo's
  unversioned HTML; any markup drift silently turns `.g` into "no results" or
  "search failed" (the dispatcher's warning logging is the acknowledged
  mitigation, not a fix).
- questionable | search.py - `SearchModule.cmd_web()` / `cmd_image()` |
  Rate-limit gate runs after the usage reply (same ordering issue as
  dictionary.py).
- test-gap | search.py | No tests exist for this module; the DDG extraction
  regexes, redirect unwrapping, and Brave-to-DDG fallback have no canned-body
  coverage, which matters more here than anywhere else in the batch because
  the parser is scrape-fragile.
