# idlerpg.py - IdleRPG player lookup

Keyless wrapper around an IdleRPG game server's XML player endpoint (default:
Rizon's). One class `IdlerpgModule` on the shared [base](base.md) contract;
blocking helper `_lookup_sync()` via `asyncio.to_thread`. The only module in
this batch that parses XML, so it is also the only `defusedxml` consumer here.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.irpg <player>` (alias `.idlerpg`) | `idlerpg.py - IdlerpgModule.cmd_irpg()` | `.irpg SomeName` | `<name> [ON/OFF] | Level n <class> | Next level H:MM:SS | Idled D days, H:MM:SS | Alignment Good/Evil/Neutral` |

Only the first whitespace token is used; player names are case sensitive
upstream (the usage and not-found replies both say so).

## Integration

- Endpoint: `GET <api_url>?player=<name>`, default
  `http://idlerpg.rizon.net/xml.php`, overridable via `[idlerpg] api_url` in
  config (`idlerpg.py - IdlerpgModule.on_load()`). Timeout 10 s.
- Inline `requests.get(stream=True)` with 256 KB read cap, body read inside the
  `with` so the socket is released on every exit path (sanctioned inline form).
- Parsing: `defusedxml.ElementTree.fromstring` - defuses XXE and
  billion-laughs from the third-party endpoint. Before parsing, four IRC
  formatting codes (`\x02 \x03 \x0f \x1f`) are stripped from the whole
  document; after parsing, `username` and `class` additionally pass
  `strip_ctrl` (full control range) because they are spliced into a formatted
  IRC line - the in-code comment states this two-layer rationale explicitly.
- Field handling: `ttl` and `totalidled` go through `int()` and
  `timedelta(seconds=...)`; `online` is compared to `"1"`; `alignment` is
  mapped through the closed `_ALIGNMENTS` dict; an empty `username` means
  player-not-found.
- Privacy: sends only the queried player name (plaintext HTTP by default - see
  Findings).

## Configuration

No key. `is_configured()` is not overridden (always True), so the module is
always visible in `.help`. Shared UA credential; `api_url` is plain config
(admin-controlled), not a secret.

## Failure behavior

One broad `except Exception`: transport, XML, and int-parse failures all reply
`lookup failed`. Oversize replies `IdleRPG response too large`. Rate limit
check before the thread spawn.

## Security notes

- `defusedxml` + pre-parse size cap covers the XML attack surface.
- `api_url` is admin config and is not `_netsafe`/`resolve_public`-validated;
  an admin can point it anywhere, including internal hosts. Config is trusted
  in this codebase, so this is consistent, but it is the only user-visible
  fetch target in the batch that comes from config rather than a constant.
- Residual splice: `level` (see Findings) - bounded by the sender stripping
  CR/LF/NUL at the wire (`sender.py - Sender`, line-assembly sanitization) and
  by XML 1.0 rejecting most control characters at parse time.

## Findings

- questionable | idlerpg.py - `_lookup_sync()` | `level` is spliced into the
  reply raw (no `strip_ctrl`, no `int()` coercion), inconsistent with the
  module's own comment that third-party fields entering the IRC line get the
  full-range strip; practical impact is minimal (XML parse plus sender-side
  CR/LF/NUL stripping bound it) but the stated invariant is not applied.
- questionable | idlerpg.py - `IdlerpgModule.on_load()` | default endpoint is
  plaintext `http://`, so queried player names and responses transit
  unencrypted; upstream may not offer HTTPS, in which case this is an upstream
  limitation worth a config comment rather than a code change.
