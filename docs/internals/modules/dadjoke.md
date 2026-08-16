# dadjoke.py - random dad joke (icanhazdadjoke.com)

Keyless one-endpoint wrapper (77 lines) on the [fact](fact.md) template. Base
contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.dadjoke` / `.joke` | `.dadjoke` (args ignored) | the joke text, single line |

## Integration

`_fetch_sync()` (via `asyncio.to_thread`): `GET https://icanhazdadjoke.com/`
with `Accept: application/json` - load-bearing here, because the endpoint
content-negotiates and serves HTML without it (module docstring, verified
against the header set at dadjoke.py:32). Shared UA, `timeout=8`, inline
stream-and-cap at 16 KB (pattern in fact.md). Reads the JSON `joke` field,
`strip_ctrl`-sanitized (400-char cap). This is the one template instance whose
oversize log line includes the byte count.

## Configuration

None. Keyless; `is_configured()` True. `on_load()` resolves the shared
`weather_user_agent` credential (same chain as fact.md).

## Failure behavior

Same ladder as fact.py: oversize -> `dad joke too long for IRC`; transport
error -> `dad joke unavailable`; parse error -> `dad joke parse error`; empty
field -> `no joke received`. Warnings logged; nothing propagates.

## Security notes

Identical posture to fact.py: hardcoded HTTPS host, no user input in the
request, no nick/channel sent, `strip_ctrl` on third-party text,
`bot.rate_limited(nick)` gate first.

## Findings

- questionable | dadjoke.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | dadjoke.py - `_fetch_sync()` | No tests reference this module.
