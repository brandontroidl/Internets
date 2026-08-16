# httpcode.py - HTTP status-code reference (.http)

## Purpose

Static lookup of a 3-digit HTTP status code against a bundled table of ~60
codes (`_CODES`): common 1xx-5xx plus the WebDAV / RFC 7540-era extras and
418. No API, no key, no state. Module class: `modules/httpcode.py -
HttpcodeModule`, built on [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.http` | `.http <code>` | `**404** Not Found - Resource could not be found.` |

## Integration / Configuration

None; `is_configured()` returns `True`.

## Failure behavior

`cmd_http()` validates in order: rate gate, empty arg -> usage, then a
strict `len(s) == 3 and s.isdigit()` shape check (so `.http 40x`, `.http
1000`, and negative input are rejected with `code must be 3 digits`),
then table miss -> `unknown status code N`. Nothing can raise.

## Security notes

Input never leaves the process; the only reply content is the bot's own
table text, passed through the local `_strip_ctrl()` alias of
`base.strip_ctrl` as a formality (the table is trusted). Per-nick rate
limit checked first in the handler (no `_gate()` helper here; inline).

## Findings

- questionable | `httpcode.py - _CODES` | Reason phrases for 413/422 use
  the RFC 7231/4918 names (`Payload Too Large`, `Unprocessable Entity`);
  RFC 9110 (2022) renamed them `Content Too Large` and `Unprocessable
  Content`. Both remain in wide use; trivial staleness, not an error.
