# catfact.py - random cat fact (catfact.ninja)

Keyless one-endpoint wrapper (71 lines), byte-for-byte the [fact](fact.md)
template with a different URL, JSON field, and error strings. Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.catfact` / `.cat` | `.catfact` (args ignored) | the fact text, single line |

## Integration

`_fetch_sync()` (via `asyncio.to_thread`): `GET https://catfact.ninja/fact`,
shared UA, `timeout=8`, inline stream-and-cap at 16 KB (see fact.md for the
pattern). Reads the JSON `fact` field, `strip_ctrl`-sanitized (400-char cap).
No explicit `Accept` header (the endpoint serves JSON by default).

## Configuration

None. Keyless; `is_configured()` True. `on_load()` resolves the shared
`weather_user_agent` credential (same chain as fact.md).

## Failure behavior

Same ladder as fact.py: oversize -> `cat fact too long for IRC`; transport
error -> `cat facts API unavailable`; parse error -> `cat facts parse error`;
empty field -> `no fact received`. All logged as warnings; nothing propagates.

## Security notes

Identical posture to fact.py: hardcoded HTTPS host, no user input in the
request, no nick/channel sent, `strip_ctrl` on the third-party text,
`bot.rate_limited(nick)` gate before fetching.

## Findings

- questionable | catfact.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | catfact.py - `_fetch_sync()` | No tests reference this module.
