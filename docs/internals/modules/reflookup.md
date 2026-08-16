# reflookup.py - keyless reference lookups (wiki / doi / isbn / so / rfc / rtfm / arxiv / element)

Eight reference commands in one module (682 lines), all keyless. Seven hit public
APIs; `.element` is a fully offline periodic-table lookup. Base contract (dispatch,
`fetch_json`, `strip_ctrl`, `cred`, rate limiting) is described in [base](base.md);
this doc covers what is specific to `modules/reflookup.py`.

## Purpose

Give channel users one-line factual lookups against authoritative reference sources
without any API key: Wikipedia summaries, Crossref DOI metadata, Open Library ISBN
records, Stack Overflow questions, RFC metadata, tldr-pages command references,
arXiv papers, and periodic-table entries.

## Commands

| Command | Usage | Backend | Reply shape |
|---|---|---|---|
| `.wiki` | `.wiki <query>` | Wikipedia REST + opensearch | `**Title**: first-sentences extract - URL` |
| `.doi` | `.doi <doi>` | Crossref | `**Title** :: authors :: journal :: year` |
| `.isbn` | `.isbn <isbn>` | Open Library | `**Title** :: authors :: year :: publishers` |
| `.so` | `.so <query>` | Stack Exchange API | `**Title** :: score N :: answered/unanswered :: link` |
| `.rfc` | `.rfc <number\|title>` | rfc-editor + IETF datatracker | `**RFC n**: title :: status :: month year` |
| `.rtfm` | `.rtfm <command>` | tldr-pages (raw.githubusercontent.com) | `**name** (platform): desc :: up to 3 examples` |
| `.arxiv` | `.arxiv <id\|query>` | arXiv ATOM API | `**Title** :: authors :: date :: link` |
| `.element` | `.element <name\|symbol\|Z>` | offline table | `**Name** (Sym) :: Z= :: mass :: group :: period :: category` |

No aliases. Every handler follows the same skeleton: rate-limit gate
(`RefLookupModule._gate()`), usage line on empty arg, input clipped to
`_MAX_INPUT` (200 chars), blocking worker offloaded via `asyncio.to_thread`,
single `privmsg` reply (`.element` skips the thread - it is pure in-memory).

## Integration

All JSON endpoints go through `base.fetch_json` (default 256 KB cap, streamed);
the two non-JSON paths use inline stream + cap fetchers. Timeout is 10 s
everywhere except the datatracker search (12 s). The shared `User-Agent` comes
from `cred(cfg, "weather_user_agent", ...)` (see Configuration).

| Worker | Endpoint | Notes |
|---|---|---|
| `_wiki_summary()` | `GET https://en.wikipedia.org/api/rest_v1/page/summary/<title>` | `allow_404=True`; exact-title first |
| `_wiki_search_title()` | `GET https://en.wikipedia.org/w/api.php` `action=opensearch&limit=1` | fallback when the exact title 404s (case/punctuation forgiveness) |
| `_doi_sync()` | `GET https://api.crossref.org/works/<doi>` | DOI quoted with `safe='/'` |
| `_isbn_sync()` | `GET https://openlibrary.org/api/books` `bibkeys=ISBN:<n>&jscmd=data` | hyphens/spaces stripped from the ISBN first |
| `_so_sync()` | `GET https://api.stackexchange.com/2.3/search/advanced` `sort=relevance&site=stackoverflow` | first item only |
| `_rfc_by_number()` | `GET https://www.rfc-editor.org/rfc/rfc<n>.json` | `allow_404=True` |
| `_rfc_search_number()` | `GET https://datatracker.ietf.org/api/v1/doc/document/` `name__startswith=rfc&title__icontains=<q>&limit=20` | title-to-number resolution |
| `_http_text()` (rtfm) | `GET https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/<platform>/<name>.md` | raw text, 64 KB inline cap, `None` on 404 |
| `_arxiv_fetch_xml()` | `GET http://export.arxiv.org/api/query` | ATOM XML, 256 KB inline cap (`_MAX_XML_BYTES`), parsed with defusedxml |

Non-obvious behavior per backend:

- **wiki** (`_wiki_sync()`): tries the REST summary for the literally-typed title
  (spaces to underscores, fully percent-encoded). On a 404 it resolves the query
  via opensearch and retries the summary with the resolved title. Disambiguation
  pages (`type == "disambiguation"`) get an explicit "be more specific" reply
  instead of the extract. Extract clipped to 300 chars, URL taken from
  `content_urls.desktop.page` with a constructed `/wiki/<title>` fallback.
- **rfc** (`_rfc_sync()`): a purely numeric arg goes straight to rfc-editor.
  Anything else runs the datatracker title search; `_rfc_search_number()` ranks
  candidates by the tuple `(exact title match, prefix match, substring match,
  -len(title))` and takes the max, extracting the number from the `rfc` field or
  the `rfcNNNN` document name. The winner is then re-fetched from rfc-editor for
  canonical metadata.
- **rtfm** (`_rtfm_sync()`): the command name is lowercased, spaces become `-`,
  and it must match `_RTFM_NAME_RE` (`^[a-z0-9][a-z0-9.+_-]*$`, max 40 chars)
  before any fetch - the only user input spliced into the URL path. Platforms
  are probed in order `common, linux, osx, freebsd, openbsd, netbsd`; the first
  hit wins (up to 6 sequential requests on a total miss). The Markdown page is
  parsed line-wise: first non-boilerplate `>` line becomes the description,
  `- text` + following backtick line become example pairs; `{{placeholders}}`
  braces and backticks stripped; output is the header plus the first 3 examples,
  whole line capped at 420 chars.
- **arxiv** (`_arxiv_sync()` / `_arxiv_fetch_xml()`): an id-vs-query heuristic -
  contains `/` (old-style `hep-th/9901001`) or is digits-plus-dot (optionally
  with `v`) - selects `id_list=<q>&max_results=1` versus
  `search_query=all:<q>&max_results=1`. The raw body is size-capped before
  `defusedxml.ElementTree.fromstring` parses it (XXE / billion-laughs defense);
  fields are read namespace-qualified from the first `entry`. The `fetch=`
  parameter on `_arxiv_sync()` exists for test injection
  (`tests/test_reflookup.py - TestArxiv`).
- **element** (`element_lookup()`): pure function over the 118-entry `_ELEMENTS`
  table with three prebuilt indexes (`_BY_SYMBOL`, `_BY_NAME`, `_BY_Z`). Digit
  input tries Z first, then symbol/name case-insensitively. Group `0` (interior
  lanthanides/actinides, which have no IUPAC group) renders as `group -`.

## Configuration

None required. `is_configured()` returns `True` unconditionally. `on_load()`
resolves the outbound `User-Agent` via
`cred(cfg, "weather_user_agent", "weather", "user_agent", "Internets/1.0")` -
the same UA credential the weather stack uses (env override
`INTERNETS_WEATHER_USER_AGENT`, then `config.ini` `[weather] user_agent`,
placeholder-guarded, default `Internets/1.0`). There is no keyless degradation
because there are no keys.

## State and concurrency

No persistent or cached state; the only instance field is `self._ua`. The
element table is module-level immutable data built at import. All network
workers are synchronous `requests` code run in `asyncio.to_thread`, one thread
per invocation, no shared mutable state.

## Failure behavior

Every worker is wrapped in try/except and returns a string on every path -
handlers never see an exception. Misses return a specific message
(`no Wikipedia article for '...'`, `no RFC 99999`, `no arXiv result ...`,
`no tldr page ... (try the full page: man <cmd>)`); transport errors, size-cap
hits (`ResponseTooLarge`), parse errors, and shape surprises all log a warning
and return the generic `lookup failed`. 404s are expected misses
(`allow_404=True` / explicit 404 check in the raw fetchers), not errors.
Behavioral evidence: `tests/test_reflookup.py` covers happy, miss, malformed,
and exception paths for element/wiki/doi/isbn/so/rfc/arxiv.

## Security notes

- All upstream-derived text is routed through `strip_ctrl` before splicing into
  the bot-attributed IRC line (per-field, with per-field length caps), so
  upstream cannot inject IRC formatting/CR-LF.
- User input reaches URL paths only percent-encoded (`quote(..., safe="")` for
  wiki titles, `safe='/'` for DOIs) or charset-whitelisted (`.rtfm` name regex);
  everything else travels as `params=` which requests encodes.
- The two non-`fetch_json` fetchers both stream and cap the body before
  buffering (64 KB tldr, 256 KB arXiv); arXiv XML is parsed by defusedxml only.
- Hosts are hardcoded - no user-controlled destination, so no SSRF surface and
  no `_netsafe` involvement.
- Privacy: only the query text and the shared UA leave the machine. Nick and
  channel are never sent upstream.

## Findings

- questionable | reflookup.py - `_arxiv_fetch_xml()` | The arXiv endpoint is
  fetched over plain `http://export.arxiv.org`, the only cleartext URL in the
  module; arXiv serves the same API over HTTPS, and cleartext allows an
  on-path attacker to substitute the ATOM body (damage bounded by size cap,
  defusedxml, and strip_ctrl, but the fix is one character-class cheap).
- doc-drift | reflookup.py - module docstring | The docstring lists seven
  commands and omits `.rtfm`, which the module registers and documents in
  `help_lines()`.
- questionable | reflookup.py - `_wiki_sync()` (and every `_*_sync` worker) |
  Each worker has two except blocks with identical bodies (specific-tuple then
  bare `Exception`, both log + return "lookup failed"); the first clause is
  redundant dead weight since the second catches everything anyway.
- test-gap | reflookup.py - `_rtfm_sync()` / `_http_text()` | The only
  reflookup workers with no test coverage; the Markdown line-parser and the
  platform-probe loop are exactly the kind of format-sensitive code the other
  workers get canned-response tests for.
