# bored.py - random activity suggestion (bored-api.appbrewery.com)

Keyless one-endpoint wrapper (74 lines) on the [fact](fact.md) template. The
original boredapi.com domain went offline in 2024; this hits the appbrewery
mirror of the same dataset (module docstring). Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.bored` | `.bored` (args ignored) | `bored? try: <activity> \| type: <t> \| participants: <n>` |

## Integration

`_fetch_sync()` (via `asyncio.to_thread`):
`GET https://bored-api.appbrewery.com/random`, shared UA, `timeout=8`, inline
stream-and-cap at 16 KB (pattern in fact.md; this instance imports `json` at
module top instead of inside the function, and its oversize path returns
without logging). Formats the JSON `activity`, `type`, and `participants`
fields, each defaulting to `?` when absent, into one line that is then
`strip_ctrl`-sanitized as a whole.

## Configuration

None. Keyless; `is_configured()` True. `on_load()` resolves the shared
`weather_user_agent` credential (same chain as fact.md).

## Failure behavior

Oversize -> `Bored API response too large` (no warning logged, unlike its
siblings); transport error -> `Bored API unavailable`; parse error ->
`Bored API response parse error`. Missing fields degrade to `?` rather than
an error reply.

## Security notes

Identical posture to fact.py: hardcoded HTTPS host, no user input in the
request, no nick/channel sent, `strip_ctrl` on the composed line,
`bot.rate_limited(nick)` gate first.

## Findings

- defect | bored.py - `_fetch_sync()` | Cosmetic: the reply is built with
  `\x02` IRC bold around "bored?" and then passed through `strip_ctrl`, whose
  `[\x00-\x1f\x7f]` regex (`base.strip_ctrl`) strips those very bytes - the
  intended bold never reaches the channel. Sanitize the upstream fields, not
  the self-authored markup.
- questionable | bored.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | bored.py - `_fetch_sync()` | No tests reference this module.
