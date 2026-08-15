# steam.py - Steam status/games lookup with per-nick ID registry

Keyed wrapper around the Steam Web API plus the batch's only persistent
per-user store: a nick-to-steamid64 JSON registry so `.steam` with no argument
resolves the caller. One class `SteamModule` on the shared [base](base.md)
contract; blocking helpers (`_resolve_vanity`, `_get_status`, `_get_games`,
`_status_sync`, `_register_sync`) run via `asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.steam` | `steam.py - SteamModule.cmd_steam()` | no arg | Caller's registered account: `<persona> [STATE]` plus `Last seen <ago>` when offline or `Playing <game> [on ip:port]` when in game |
| `.steam <target>` | same | `.steam gaben` | Target resolved as registered nick first, else raw steamid64/vanity name |
| `.steam -n <nick>` | same | `.steam -n bob` | Force registered-nick lookup; errors if `<nick>` has no registration |
| `.steam -g [target]` | same | `.steam -g` | Owned-games summary: game count, total playtime hours, most-played title |
| `.regsteam <id-or-vanity>` (alias `.register_steam`) | `steam.py - SteamModule.cmd_regsteam()` | `.regsteam 7656119...` | Resolves, stores `nick.lower() -> steamid64`, confirms via notice with current persona name |

Flag parsing in `cmd_steam()`: a leading `-g` is stripped first (so
`.steam -g -n bob` works); the remaining target is checked against the
registry before being treated as a raw id/vanity name.

## Integration

Three Steam Web API endpoints, all via `modules.base - fetch_json()`,
timeout 10 s, key in query string:

| Helper | Endpoint | Cap |
|---|---|---|
| `_resolve_vanity()` | `ISteamUser/ResolveVanityURL/v0001` | 256 KB default |
| `_get_status()` | `ISteamUser/GetPlayerSummaries/v0002` | 256 KB default |
| `_get_games()` | `IPlayerService/GetOwnedGames/v0001` (`include_appinfo=1`, free games included) | 1 MB - raised because large libraries with appinfo exceed 256 KB (in-code comment) |

- `_register_sync()` treats a 10+ digit token as a steamid64 (verified via
  `_get_status`), otherwise attempts vanity resolution; both paths return the
  canonical `steamid` from the API response, not the raw input.
- `_PERSONA_STATES` maps persona codes 0-6 to colored labels; unknown codes
  render `UNKNOWN` grey.
- `-g` failure (private profile) degrades to status plus
  `game data unavailable`; `game_count == 0` gets its own message.
- Privacy: what leaves the host is the queried id/vanity plus the API key.
  What is displayed can include `gameserverip` (the target's current game
  server) - public via Steam anyway. The registry maps IRC nicks to Steam
  identities, which is PII linking two namespaces; it is stored locally only.

## State

- `self._ids`: in-memory dict `nick.lower() -> steamid64`, loaded in
  `on_load()` from `steamids_file` (`[steam] steamids_file`, default
  `steamids.json`); unreadable/corrupt file degrades to `{}`.
- `SteamModule._save_ids()` persists atomically: `tempfile.mkstemp` in the
  target directory, write, `chmod 0600`, `os.replace`. Serialized by
  `self._lock` (a `threading.Lock`, since saves run in `to_thread` workers).
- `SteamModule.forget()` implements the `.forgetme` right-to-erasure hook:
  pops the nick under the lock, persists, returns the removed count.

## Configuration

- Key: `cred(cfg, "steam_key", "steam", "steam_key")` - `INTERNETS_STEAM_KEY`
  env var, then secret_store, then legacy `[steam] steam_key`.
- Keyless: `is_configured()` False; both commands reply
  "Steam API key not configured".

## Concurrency

Command handlers run on the event loop; lookups and saves run in
`asyncio.to_thread` workers. `_lock` serializes `_save_ids()` against
`forget()`. However `cmd_regsteam()` mutates `self._ids` on the loop thread
without taking the lock (see Findings), and `cmd_steam()` reads it lock-free
(single dict `get`, atomic under the GIL - acceptable).

## Failure behavior

`_status_sync` / `_register_sync` wrap API calls broadly: transport and parse
errors reply `lookup failed`; an empty players list raises
`ValueError("no user found")` inside the same net. `_resolve_vanity` swallows
its own errors to `None` (logged at debug). Save failures log a warning and
leave the previous file intact (atomic replace never half-writes).

## Security notes

Registry file written 0600; all persona/game names pass `strip_ctrl` before
hitting IRC; HTTP exclusively via `fetch_json`.

## Findings

- defect | steam.py - `SteamModule._save_ids()` | if `tempfile.mkstemp` itself
  raises, the cleanup path `os.unlink(tmp)` references an unbound local -> the
  `UnboundLocalError` (not an `OSError`) escapes the handler and masks the
  original failure; low likelihood (mkstemp fails only on permissions/disk)
  but the error branch is wrong.
- questionable | steam.py - `SteamModule.cmd_regsteam()` | `self._ids[...] = sid`
  happens outside `self._lock` while another registration's `_save_ids()` may
  be iterating the same dict in `json.dump` inside a worker thread; a
  concurrent mutation can raise "dict changed size during iteration", which
  `_save_ids` swallows as a failed (skipped) save.
- questionable | steam.py - `_get_status()` / `_register_sync()` |
  `log.warning` on failure embeds the request URL including `key=<steam_key>`
  in the bot log (log-only leak; batch-wide pattern, see imdb.md).
- test-gap | steam.py - `SteamModule` | no tests cover the registry
  (load/save/forget round-trip, atomic-write path, flag parsing of
  `-g`/`-n`); the module has the most state in the batch and zero coverage.
