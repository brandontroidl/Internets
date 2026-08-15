# mtg.py - Magic: the Gathering card lookup via Scryfall

Keyless wrapper around Scryfall's fuzzy card-name endpoint. One class
`MtgModule` on the shared [base](base.md) contract; blocking helper
`_fetch_sync()` via `asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.mtg <card>` | `mtg.py - MtgModule.cmd_mtg()` | `.mtg black lotus` | `<Name> <mana cost> | <type line> [P/T or [loyalty]] | <oracle text, newlines as " | "> | <set> (<Rarity>)` |

## Integration

- Endpoint: `GET https://api.scryfall.com/cards/named?fuzzy=<name>`
  (`mtg.py - _fetch_sync()`), timeout 10 s.
- Transport is an inline `requests.get(stream=True)` with the same
  read-cap pattern as `fetch_json` (`_MAX_BODY_BYTES = 256 KB`, read
  `cap + 1` then compare) rather than the shared helper, because the module
  wants distinct 404 handling: Scryfall 404 means "no fuzzy match" and maps to
  `no card matched '<name>'`. The `with` block releases the socket on every
  exit path. This is the sanctioned inline stream+cap form, not a bare
  `r.json()`.
- Scryfall asks for a descriptive User-Agent and inter-call delay; the module
  sends the shared UA and relies on the bot's per-nick rate limiter for pacing
  (module docstring states this).
- Power/toughness printed when both present, else loyalty in brackets when
  present (planeswalkers).
- Privacy: sends only the card name.

## Configuration

No key. `is_configured()` returns True unconditionally. UA from the shared
`weather_user_agent` credential.

## Failure behavior

`requests.RequestException` maps to `Scryfall unavailable`; any other exception
(JSON decode, oversize path returns early with `Scryfall response too large`)
maps to `Scryfall response parse error`. Rate limit check before the thread
spawn.

## Security notes

Inline size cap before parse; whole reply passes once through
`_strip_ctrl()` (an alias of `modules.base - strip_ctrl()` with the same
400-char default, so a long oracle text is truncated to 400 chars total).

## Findings

- questionable | mtg.py - `_strip_ctrl()` | pure pass-through alias of
  `strip_ctrl` with identical defaults - dead indirection repeated in
  poke/dnd/recipe/cocktail/crypto/fx.
