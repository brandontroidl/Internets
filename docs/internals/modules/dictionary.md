# dictionary.py - English dictionary definitions (Free Dictionary API)

Thin keyless wrapper (92 lines) around dictionaryapi.dev with `/N` result
pagination. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.dict` / `.dictionary` | `.dict <word> [/N]` e.g. `.dict ephemeral /2` | `[N/total] **word** (part-of-speech) - definition` |

The optional `/N` suffix (parsed by `_IDX_RE`, `^(.+?)\s*/(\d+)$`) selects the
N-th definition; N is clamped to `1..total`, defaulting to 1.

## Integration

`_lookup_sync()` (run via `asyncio.to_thread`) calls
`GET https://api.dictionaryapi.dev/api/v2/entries/en/<word>` through
`base.fetch_json` (word fully percent-encoded, `ua=` shared UA, 10 s timeout,
default 256 KB cap, `allow_404=True` - a 404 is the API's "no such word" and
becomes `no definition found for '...'`). English only - the `/en/` segment is
hardcoded. All definitions across all entries/meanings are flattened into a
`(partOfSpeech, definition)` list before indexing, so `/N` pages across the
whole set; the chosen definition is clipped to 400 chars with an ellipsis.

## Configuration

None. Keyless; no `is_configured()` override (base default `True`). `on_load()`
resolves the shared `weather_user_agent` credential for the UA, as in
[reflookup](reflookup.md).

## Failure behavior

Single catch-all: any exception (transport, size cap, shape) logs a warning and
returns `lookup failed`. Miss paths (`None`, empty list, no definitions in the
payload) return the specific not-found message.

## Security notes

Word travels percent-encoded in the URL path; response text is sanitized with
`strip_ctrl` (word, part of speech, and definition separately) before hitting
IRC. Only the word and UA are sent upstream - no nick, no channel. Hardcoded
host, no SSRF surface.

## Findings

- questionable | dictionary.py - `DictionaryModule.cmd_dict()` | The
  rate-limit check runs after the usage reply and argument parsing, so
  empty-arg invocations bypass rate limiting entirely (reflookup/pkginfo/
  scholar/ghinfo gate before replying; dictionary, urbandictionary, translate,
  and search gate after).
- test-gap | dictionary.py - `_lookup_sync()` | No tests exist for this module
  (no `tests/test_dictionary.py`); the flatten-and-paginate logic and the
  404-miss path are untested.
