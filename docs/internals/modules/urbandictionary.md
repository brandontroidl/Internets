# urbandictionary.py - Urban Dictionary lookups with pagination

Thin keyless wrapper (68 lines) around the Urban Dictionary define API, same
`/N` pagination pattern as [dictionary](dictionary.md). Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.u` / `.urbandictionary` | `.u <word> [/N]` e.g. `.u yolo /2` | `[N/total] definition text` |

`/N` (regex `^(.+?)\s*/(\d+)$`) selects the N-th definition, clamped to the
result count, default 1.

## Integration

`_lookup_sync()` (via `asyncio.to_thread`) calls
`GET https://api.urbandictionary.com/v0/define?term=<term>` through
`base.fetch_json` (term as `params=`, shared UA, 10 s timeout, default 256 KB
cap, no `allow_404` - the API returns 200 with an empty `list` on a miss).
The chosen definition has CR removed and LF flattened to spaces, is clipped to
400 chars, then `strip_ctrl`-sanitized. Unlike `.dict`, the reply does not echo
the term or show a part of speech - just the indexed definition text.

## Configuration

None. Keyless; base-default `is_configured()`. `on_load()` resolves the shared
`weather_user_agent` credential for the UA.

## Failure behavior

Empty `list` returns `No results for '<term>'`; any exception (transport, size
cap, missing `definition` key) logs a warning and returns `lookup failed`.

## Security notes

Term sent as a query parameter (requests-encoded) plus the shared UA - no nick
or channel leaves the machine. Definition text is user-generated content from
Urban Dictionary spliced into a bot-attributed line; `strip_ctrl` is the
defense against embedded IRC control codes. Hardcoded host, no SSRF surface.

## Findings

- questionable | urbandictionary.py - `UDModule.cmd_ud()` | Same rate-limit
  ordering as dictionary.py: the gate runs after the usage reply, so empty-arg
  spam is not rate limited.
- test-gap | urbandictionary.py - `_lookup_sync()` | No tests exist for this
  module; pagination clamping and the empty-list miss path are untested.
