# scinews.py - STEM news aggregator and article reader (`.sci`)

Curated keyless RSS/Atom aggregation: 173 hardcoded feed URLs across 12 topics, fetched
concurrently, merged newest-first with a per-source diversity cap, plus a follow-up
reader that fetches a listed article and extracts its lead paragraph. The only module
in the batch with two-step interaction state (a per-channel "last list"). Base
contract: [base](base.md).

## Purpose

Give IRC users a `.sci [topic]` headline digest without any API key, and let them
drill into one item (`.sci read <N>`) without leaving IRC. All feed URLs are
operator-curated constants (`scinews.py - _FEEDS`); only the reader follows
attacker-influenceable URLs, and it does so through the SSRF-pinned fetch.

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.sci` | `[topic]` (default `all`) | Fetch all feeds tagged with the topic, merge, reply with a numbered 10-item list. The topic set is derived from `_FEEDS` tags: all, bio, bsd, chem, earth, eng, linux, math, physics, security, space, tech. |
| `.sci read <N>` | 1-based index | Re-print item N from the channel's last list plus a lead paragraph and the URL (3 lines). |
| `.sci sources` | - | List the topic names and the feed count. |

Handler: `scinews.py - ScinewsModule.cmd_sci()`. Unknown topic replies with the topic
list. `.sci read` without a prior list (or after the 600 s `_LIST_TTL`) replies
"run .sci first".

Note the module docstring advertises a `physics / cs / math / bio / astro / space`
topic set that does not match the derived `_TOPICS` (there is no `cs` or `astro`
tag); see Findings.

## Integration

Two distinct fetch paths, deliberately different:

1. **Feed fetch** - `scinews.py - _http_bytes()`: plain `requests.get` (stream=True),
   timeout `_FEED_TIMEOUT` = 6 s, cap `_FEED_MAX_BYTES` = 12 MiB (comment: podcast
   and arXiv feeds run 8-10 MB). No SSRF guard - justified in the docstring because
   the URLs are hardcoded operator-curated constants, not user input.
2. **Article fetch** - `scinews.py - _read_article()`: goes through
   `_netsafe.safe_open()` (DNS-resolve + public-IP validate + IP pinning on the
   initial host and every redirect hop), timeout 6 s, cap `_ART_MAX_BYTES` = 512 KiB.
   Article URLs come from feed content, i.e. third-party-influenceable, so they get
   the full SSRF treatment. `SSRFBlocked` is surfaced to the user as
   "can't read that article (...)".

Both paths send only the shared User-Agent (see Configuration); no nick, channel, or
user data leaves the bot. Feed parsing uses `defusedxml.ElementTree` (entity-expansion
safe); article parsing uses the stdlib `html.parser` subclass `scinews.py - _Lead`.

## Configuration

No key; `ScinewsModule.is_configured()` returns True unconditionally. The User-Agent
is `cred(cfg, "weather_user_agent", "weather", "user_agent", "Internets/1.0")`
(`ScinewsModule.on_load()`) - i.e. it reuses the weather UA, which by convention
contains the operator's contact email (env override `INTERNETS_WEATHER_USER_AGENT`).
That UA is presented to every feed host and every article host.

## State

All in-memory, owned by the module instance (`ScinewsModule.on_load()`):

- `_last: dict[reply_to -> (monotonic_ts, items)]` - the per-channel/PM numbered
  list backing `.sci read`. TTL `_LIST_TTL` = 600 s. Evicted by a sweep on every
  successful list command (`cmd_sci`), with an explicit comment that PM keys are
  attacker-controlled nicks, so the map must not grow unbounded.
- `_cache: dict[topic -> (monotonic_ts, items)]` - aggregate fetch cache, TTL
  `_CACHE_TTL` = 120 s. Key space is bounded by the validated topic set.

Nothing is persisted; a module reload drops both.

## Concurrency

`ScinewsModule._get_items()` fans the feed fetches out with
`asyncio.to_thread(_fetch_one, ...)` under an `asyncio.Semaphore(_FETCH_CONCURRENCY)`
(= 8), gathered with `return_exceptions=True` so one broken feed cannot fail the
batch; non-list results (exceptions) are silently dropped from the merge. The
article read is a single `to_thread` call. No locks: state is only touched from the
event loop.

## Merge algorithm

`_get_items()` after the gather:

1. Flatten all per-feed item lists (each feed already truncated to its 10 newest by
   `_fetch_one()`), sort globally newest-first by feed timestamp. Undated items
   (`_parse_date` returned 0.0) sink to the end.
2. First pass: take items newest-first, skipping duplicate normalized titles
   (`_norm_title()` - lowercase, whitespace-collapsed) and skipping any source that
   already contributed `_PER_SOURCE` = 3 items, until `_MAX_ITEMS` = 10.
3. Second pass (only if short): refill from the remainder ignoring the per-source
   cap, still deduplicating. This keeps `.sci bsd`-style topics with few live feeds
   from returning 3 items.

Behavior pinned by `tests/test_scinews.py - test_diversity_and_order` (cap holds,
newest first, exactly `_MAX_ITEMS` returned).

## Parsing

- `_parse_feed()` - namespace-agnostic RSS/Atom: iterates every element, treats any
  `item`/`entry` local name as an entry, reads child `title`, `link` (Atom `href`
  attribute or RSS text), and the first of `pubdate`/`published`/`updated`/`date`.
  Malformed XML returns `[]` (broad except around `fromstring`). Titles are
  HTML-stripped (`_clean()`), entity-unescaped, control-stripped and capped at 160.
- `_parse_date()` - RFC 822 via `email.utils.parsedate_to_datetime`, falling back to
  ISO 8601 (`Z` normalized to `+00:00`, naive treated as UTC), else 0.0.
- `_Lead` (HTMLParser) - collects `og:description` (preferred), else
  `description`/`twitter:description` meta, else the first non-empty `<p>` text.
  `_read_article()` truncates the lead to 320 chars and control-strips it.

Both parsers are exercised in `tests/test_scinews.py` (RSS, Atom href, garbage XML,
og preference, first-paragraph fallback).

## Failure behavior

- Per-feed failure (`requests.RequestException` or over-cap `ValueError`): logged at
  warning, feed contributes nothing (`_fetch_one()` returns `[]`).
- All feeds failing: "no headlines right now (feeds unreachable?)" - but the empty
  result is still cached for 120 s (see Findings).
- Reader: `SSRFBlocked` -> "can't read that article (...)"; transport error ->
  "could not fetch article"; over-cap -> "could not fetch article (too large)";
  unparseable/empty lead -> "(no preview available)". The reader never raises to the
  dispatcher (`tests/test_scinews.py - test_reader_handles_ssrf_block`,
  `test_read_article_refuses_internal`).
- Per-nick rate limiting via `ScinewsModule._gate()` before any work.

## Security notes

- SSRF: the trust split described under Integration is the load-bearing decision -
  curated constants fetched directly, feed-derived article URLs only via
  `_netsafe.safe_open` (redirect-hop re-validation closes the DNS-rebinding TOCTOU).
- XML: `defusedxml` blocks entity-expansion and external-entity attacks from a
  compromised feed.
- Output injection: every third-party string that reaches IRC (source name, title,
  lead, URL) passes `strip_ctrl` with explicit caps, so a hostile feed cannot embed
  IRC control codes or flood a line.
- Memory: both size caps bound a hostile upstream; `_last` eviction bounds the map
  against PM-key growth.

## Findings

- doc-drift | `scinews.py` module docstring | Advertises topics
  `all / physics / cs / math / bio / astro / space` but the real set derived from
  `_FEEDS` has no `cs` or `astro` tag (cs lives under `tech`, astronomy under
  `space`) and omits bio-adjacent extras (bsd, chem, earth, eng, linux, security);
  `.sci sources` prints the correct set.
- questionable | `scinews.py - _parse_feed()` | For an entry with multiple `<link>`
  children (Atom entries with `rel="alternate"` plus `rel="enclosure"`, e.g. the
  BSD Now podcast feed) the last link element wins with no `rel` filtering, so the
  stored URL - and what `.sci read` then fetches - can be a media enclosure rather
  than the article page.
- questionable | `scinews.py - ScinewsModule._get_items()` | A total-failure empty
  merge is cached for `_CACHE_TTL` (120 s), so a transient network blip pins
  "no headlines" for 2 minutes with no retry.
- test-gap | `scinews.py - _http_bytes()` | The feed-side size-cap rejection path
  (ValueError at > 12 MiB) and the reader's over-cap branch in `_read_article()`
  have no test; `tests/test_scinews.py` covers parsing, merge, gating and SSRF only.
