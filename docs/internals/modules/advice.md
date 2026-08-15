# advice.py - random advice slip (api.adviceslip.com)

Keyless one-endpoint wrapper (73 lines) on the [fact](fact.md) template. Base
contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.advice` | `.advice` (args ignored) | the advice text, single line |

## Integration

`_fetch_sync()` (via `asyncio.to_thread`):
`GET https://api.adviceslip.com/advice`, shared UA, `timeout=8`, inline
stream-and-cap at 16 KB (pattern in fact.md). Upstream quirk noted in the
module docstring: the server labels the response `Content-Type: text/html`
even though the body is JSON, so the module parses the raw bytes as JSON
explicitly instead of trusting the header (which the manual
`json.loads(body.decode(...))` path does anyway for the whole template
family). Reads the nested `slip.advice` field (`d.get("slip", {}).get(
"advice", "")`), `strip_ctrl`-sanitized (400-char cap).

## Configuration

None. Keyless; `is_configured()` True. `on_load()` resolves the shared
`weather_user_agent` credential (same chain as fact.md).

## Failure behavior

Same ladder as fact.py: oversize -> `advice too long for IRC`; transport
error -> `advice slip API unavailable`; parse error -> `advice slip parse
error`; empty/missing `slip.advice` -> `no advice received`. Warnings logged;
nothing propagates.

## Security notes

Identical posture to fact.py: hardcoded HTTPS host, no user input in the
request, no nick/channel sent, `strip_ctrl` on third-party text,
`bot.rate_limited(nick)` gate first.

## Findings

- questionable | advice.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | advice.py - `_fetch_sync()` | No tests exercise the module
  (tests/test_help.py:77 uses the literal string "advice" to test
  `base.help_row`, not this module).
