# qdb.py - bash.org-style quote lookup (bash-org-archive.com scraper)

## Purpose

`QdbModule` serves random or specific quotes in the classic `[qdb #N]` output
shape. The original QDB endpoints are dead (module docstring records the 2026
state: qdb.us repurposed, bash.org offline), so this scrapes the read-only
HTML archive at bash-org-archive.com. Base contract: [base](base.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.qdb` | `modules/qdb.py - QdbModule.cmd_qdb()` | `.qdb [id]` - random (`?random1`) or numeric id (`?<id>`); output `[qdb #N] <line>` per quote line |

Argument validation: the id must be all digits (`arg.isdigit()`), else
"invalid quote ID". Rate-limited.

## Integration

- Endpoint: `https://bash-org-archive.com/?random1` or `/?<id>`
  (`modules/qdb.py - _lookup_sync()`, via `asyncio.to_thread`). Override:
  `[qdb] api_url`; empty/absent falls back to the baked-in default, so
  `is_configured()` is effectively always True.
- HTTP: `requests.get(stream=True)`, 10 s timeout, UA from the shared
  `weather_user_agent` cred. Body read is the sanctioned inline stream+cap
  pattern: `r.raw.read(_MAX_BODY_BYTES + 1)` with a 256 KiB refuse-to-parse
  cap - no bare `r.text`/`r.json()`.
- Parsing: regex extraction anchored on the archive's stable markup -
  `<p class="quote">` header (permalink carries the id, `_RE_QUOTE_ID`) and
  `<p class="qt">` body, split on `<br>`/newlines, tags stripped, entities
  unescaped. A missing id degrades to a plain `[qdb]` tag rather than a
  placeholder.

## Failure behavior

404 is treated as "quote N not found" (a clean miss, not an outage - the
comment explains why); transport errors report "QDB endpoint unavailable";
oversized bodies and parse surprises get their own distinct messages. Quotes
longer than 5 lines (`_MAX_LINES`) are replaced by a "long quote - view at
<url>" link to avoid channel floods.

## Security notes

Every output line passes `base.strip_ctrl` (400-char cap), defending against
vandalized archive quotes injecting CR/LF or IRC formatting into
bot-attributed output. The endpoint is operator-configured (trusted config),
so `_netsafe` is not involved. No secrets, no user PII sent (the request
carries only the quote id).

## Findings

- test-gap | qdb.py - QdbModule | no `tests/test_qdb*` exists; the HTML
  extraction regexes have no fixture coverage, so an archive markup change
  would surface only in production.
