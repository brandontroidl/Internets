# scholar.py - keyless scholarly search (OpenAlex works + ORCID researchers)

Added 2026-08 (232 lines). Three commands over two keyless public APIs:
OpenAlex for works and dissertations, ORCID expanded search for researchers.
The two compose: `.scholar` finds a researcher's ORCID iD, which feeds back
into `.papers` for their publication list. Base contract in [base](base.md).

## Commands

| Command | Usage | Backend | Reply shape |
|---|---|---|---|
| `.papers` | `.papers <orcid\|query> [-oa]` | OpenAlex `/works` | header + up to 5 numbered work lines |
| `.thesis` | `.thesis <query> [-oa]` | OpenAlex `/works` (`type:dissertation`) | same |
| `.scholar` | `.scholar <name\|topic>` | ORCID expanded search | header + up to 3 numbered researcher lines |

Multi-line replies: one header
(`:: <label> - <count> works, top <n> ::`) then one `privmsg` per result.
Work line format (`_format_work()`):
`  N. **title** (year)[ [OA]] :: FirstAuthor[ et al.] :: venue :: link`
(title capped 130, author 40, venue 50, link 200 chars). Researcher line:
`  N. **Given Family** :: inst1; inst2 :: https://orcid.org/<iD>` (max 2
institutions, evidence `tests/test_scholar.py - TestScholar.test_happy`).

### The -oa flag

`split_flags()` pulls every `-word` token out of the arg regardless of
position (`-oa quantum`, `quantum -oa`, and `deep -oa learning` all work -
`TestSplitFlags.test_flag_anywhere`). `oa` is the only recognized flag: on
`.papers`/`.thesis` it appends the OpenAlex filter `open_access.is_oa:true`
and tags the header label `[open access]`. `.scholar` parses flags for the
empty-query check but uses none.

### Link preference

`_work_link()`: open-access PDF (`best_oa_location.pdf_url`) over OA landing
page (`best_oa_location.landing_page_url`) over DOI (`doi`, already a
`https://doi.org/...` URL in OpenAlex) over the OpenAlex record `id`.
Evidence: `TestPapers.test_result_line_formatting` (PDF wins) and
`test_link_fallback_to_doi`.

## Integration

Both endpoints via `base.fetch_json` (default 256 KB cap, 10 s timeout, shared
UA); both keyless, no auth headers.

**OpenAlex** - `GET https://api.openalex.org/works`, params built by
`_works_query()` (`.papers`) or inline in `_thesis_sync()`:

- `per-page=5` (`_N_WORKS`), and `select=` trimmed to `_WORK_FIELDS`
  (`display_name,publication_year,doi,open_access,best_oa_location,authorships,primary_location`).
  The in-code comment gives the rationale: a full work record carries abstract
  inverted indexes and reference lists, and 5 of them would blow the
  `fetch_json` 256 KB cap - the `select=` trim is what keeps the default cap
  viable.
- `.papers` with an ORCID arg: `parse_orcid()` accepts a bare iD or an
  `orcid.org` URL (`_ORCID_RE`, `\d{4}-\d{4}-\d{4}-\d{3}[\dXx]`, X checksum
  uppercased) and builds `filter=author.orcid:<iD>` with
  `sort=publication_date:desc` (newest first) and no `search`
  (`TestPapers.test_orcid_arg_builds_author_filter`).
- `.papers` with free text: `search=<query>` - deliberately the top-level
  `search=` parameter, not `filter=default.search:...`, so commas in the query
  cannot be parsed as OpenAlex filter separators (in-code comment).
- `.thesis`: always `filter=type:dissertation` (plus the OA filter, comma
  joined - `TestThesis.test_oa_flag_appends_filter`), `search=<query>`.
- Header count from `meta.count`, falling back to `len(results)`.

**ORCID** - `GET https://pub.orcid.org/v3.0/expanded-search/` with
`q=<arg>&rows=3` and `Accept: application/json` (the ORCID API defaults to
XML; the header is asserted in `TestScholar.test_happy`). Results come from
`expanded-result`; total from `num-found`. The raw arg is passed as the Solr
query, so ORCID field syntax (e.g. `family-name:...`) works implicitly.

## Configuration

None. `is_configured()` returns `True`; keys do not exist for either service.
`on_load()` resolves the shared `weather_user_agent` credential for the UA.
OpenAlex's "polite pool" (mailto identification) is not used - requests land in
the anonymous pool with its lower rate priority.

## Concurrency and dispatch

All three handlers funnel through `ScholarModule._run()`: rate-limit gate
first, then `split_flags()` on the arg just to detect an empty query (usage
reply), then the sync worker via `asyncio.to_thread` with the raw arg clipped
to `_MAX_INPUT` (200). Workers return `list[str]`; `_run()` sends one privmsg
per line. No state beyond `self._ua`; nothing cached or persisted.

## Failure behavior

Each worker wraps everything in one `except Exception` (deliberate `BLE001`
suppression - requests, JSON, and size-cap errors all land there): log warning,
return `["lookup failed"]`. Non-dict payloads return `["lookup failed"]`; empty
result sets return a specific `no results for <label>` /
`no ORCID researchers matching '...'` line. Evidence: the
malformed/exception tests in all three test classes.

## Security notes

- Every upstream string (titles, authors, venues, institution names, links,
  iDs) passes `strip_ctrl` with a field-appropriate cap before splicing.
- User input reaches upstream only as `params=` values (requests-encoded);
  the ORCID iD spliced into the `.papers` filter is regex-validated first, and
  the iD spliced into the reply URL comes from upstream but is strip_ctrl'd.
- Hardcoded hosts, no SSRF surface. Privacy: query text and UA only; no
  nick/channel.
- Multi-line replies are bounded by `per-page`/`rows` (5 and 3), so a hostile
  upstream cannot make the bot flood the channel beyond header+5 lines.

## Findings

- questionable | scholar.py - `split_flags()` | Every `-word` token is
  consumed as a flag and unknown flags are silently discarded, so a query term
  that legitimately starts with `-` (e.g. a quoted negative term or `-19`
  typed after a broken word wrap) silently vanishes from the search text with
  no feedback.
- test-gap | scholar.py - `ScholarModule._run()` | The handler layer (gate
  ordering, usage replies, per-line privmsg fan-out, the `arg[:_MAX_INPUT]`
  clip) has no test; `tests/test_scholar.py` covers workers and pure helpers
  plus wiring only.
