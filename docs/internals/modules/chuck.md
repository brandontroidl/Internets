# chuck.py - random Chuck Norris joke (api.chucknorris.io)

Keyless one-endpoint wrapper (71 lines) on the [fact](fact.md) template. Base
contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.chuck` | `.chuck` (args ignored) | the joke text, single line |

## Integration

`_fetch_sync()` (via `asyncio.to_thread`):
`GET https://api.chucknorris.io/jokes/random`, shared UA, `timeout=8`, inline
stream-and-cap at 16 KB (pattern documented in fact.md). Reads the JSON
`value` field, `strip_ctrl`-sanitized (400-char cap).

## Configuration

None. Keyless; `is_configured()` True. `on_load()` resolves the shared
`weather_user_agent` credential (same chain as fact.md).

## Failure behavior

Same ladder as fact.py: oversize -> `joke too long for IRC`; transport error
-> `Chuck Norris API unavailable`; parse error -> `Chuck Norris response parse
error`; empty field -> `no joke received`. Warnings logged; nothing propagates.

## Security notes

Identical posture to fact.py: hardcoded HTTPS host, no user input in the
request, no nick/channel sent, `strip_ctrl` on third-party text,
`bot.rate_limited(nick)` gate first.

## Findings

- questionable | chuck.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | chuck.py - `_fetch_sync()` | No tests reference this module.
