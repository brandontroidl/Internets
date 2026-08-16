# poke.py - Pokemon lookup via PokeAPI

Keyless wrapper around pokeapi.co. One class `PokeModule` on the shared
[base](base.md) contract; blocking helper `_fetch_sync()` via
`asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.poke <name-or-id>` (alias `.pokemon`) | `poke.py - PokeModule.cmd_poke()` | `.poke pikachu`, `.poke 25` | `<Name> #<id> [Type1/Type2] | HP/Atk/Def/SpA/SpD/Spe stats (BST total) | <h>m <w>kg | ability: <first ability>` |

## Integration

- Endpoint: `GET https://pokeapi.co/api/v2/pokemon/<name-lowercased>`
  (`poke.py - _fetch_sync()`), timeout 10 s.
- Inline `requests.get(stream=True)` with read cap `_MAX_BODY_BYTES = 1 MB` -
  deliberately larger than the 256 KB default because full Pokemon payloads
  (moves + sprites) run 270-430 KB (sizes cited in the module's own comment).
  404 maps to `no Pokemon called '<name>'`. Sanctioned inline stream+cap form.
- Unit conversion: height decimetres to metres, weight hectograms to
  kilograms (`/ 10.0`).
- BST is summed from the six base stats; the displayed ability is the first
  entry of `abilities` with `-` replaced by spaces.
- Privacy: sends only the Pokemon name/id.

## Input validation

`cmd_poke()` takes the first whitespace token, requires every character to be
alphanumeric or `-` (rejects otherwise before any network call - this also
prevents path traversal into other API routes since the token is interpolated
into the URL path), and normalizes pure-digit input through `str(int(...))`
because PokeAPI 404s on leading zeros (comment: `"06"`).

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`requests.RequestException` maps to `PokeAPI unavailable`; oversize body to
`PokeAPI response too large`; anything else to `PokeAPI response parse error`.
Rate limit check before the thread spawn.

## Security notes

Alphanumeric-or-hyphen validation is the URL-path injection defense; body
capped before parse; final reply passed once through `_strip_ctrl()` (400-char
cap).

## Findings

None beyond the batch-wide `_strip_ctrl` alias duplication (see mtg.md).
