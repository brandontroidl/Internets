# fact.py - random useless fact (uselessfacts.jsph.pl)

Keyless one-endpoint wrapper (71 lines). This file is the cleanest instance of a
template shared nearly verbatim by catfact, chuck, dadjoke, advice, and bored;
their docs reference this one for the shared shape. Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.fact` | `.fact` (args ignored) | the fact text, single line |

## Integration

`_fetch_sync()` (blocking, run via `asyncio.to_thread`) issues
`GET https://uselessfacts.jsph.pl/api/v2/facts/random` with the shared UA and
`Accept: application/json`, `timeout=8`, `stream=True`. The body is read with
the sanctioned inline stream-and-cap pattern: `r.raw.read(16*1024 + 1,
decode_content=True)` inside a `with` block (socket released on every exit
path); anything over 16 KB (`_MAX_BODY_BYTES`) is rejected without parsing.
The JSON `text` field is `strip_ctrl`-sanitized (400-char cap) and returned.

## Configuration

None. Keyless; `is_configured()` returns True. `on_load()` resolves the shared
`weather_user_agent` credential via `base.cred` (secret store,
`INTERNETS_WEATHER_USER_AGENT` env override, config `[weather] user_agent`
fallback, default `Internets/1.0`).

## Failure behavior

Every failure degrades to a human-readable single-line reply, never an
exception into the dispatcher: oversize body -> `fact too long for IRC`;
`requests.RequestException` -> `useless facts API unavailable`; any other
exception (JSON decode, unexpected shape) -> `useless facts parse error`;
empty `text` -> `no fact received`. All paths log a warning.

## Security notes

Hardcoded HTTPS host, no user input reaches the request - no SSRF or
injection surface. Nothing identifying (nick, channel) is sent; only the UA.
Response text is third-party content spliced into a bot-attributed line;
`strip_ctrl` is the control-code defense. `cmd_fact` checks
`bot.rate_limited(nick)` before fetching.

## Findings

- questionable | fact.py - `_strip_ctrl()` | No-op alias of `base.strip_ctrl`
  (identical 400 default); this dead indirection recurs across the whole
  fun-module batch (catfact, chuck, dadjoke, advice, bored, cowsay, qr, games).
- test-gap | fact.py - `_fetch_sync()` | No tests; the size-cap and
  degradation paths are unverified.
