# twitch.py - Twitch stream/channel/game lookup (Helix + app OAuth)

Keyed wrapper around the Twitch Helix API. Unlike the single-key modules in
this batch it needs a client id + client secret pair and runs the OAuth2
client-credentials flow itself, caching the app access token. Two classes:
`_TwitchAPI` (token management + HTTP) and `TwitchModule` (command surface) on
the shared [base](base.md) contract; `_dispatch_sync()` routes subcommands in
a `to_thread` worker.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.tw` (alias `.twitch`) | `twitch.py - TwitchModule.cmd_twitch()` | no arg | Top 5 live streams: `[i] <game> - <user> (N viewers)` on one line |
| `.tw <query>` or `.tw -s <query>` | same | `.tw shroud` | Up to 3 channel matches with LIVE/offline state and `https://twitch.tv/<login>`; 1-2 results joined on one line, 3 sent as separate lines |
| `.tw -c <channel>` | same | `.tw -c esl_csgo` | `<name> | Game ... | Title ... | Views N | link` |
| `.tw -g <game>` | same | `.tw -g tetris` | Up to 5 category matches with directory links; up to 3 on one line, more as separate lines |

Flag parsing in `cmd_twitch()`: a leading `-` token has its hyphens stripped
and becomes the subcommand (`-s`/`-stream`, `-c`/`-channel`, `-g`/`-game`;
anything else prints usage); a bare argument implies `-s`. Multi-line results
are split on `\n` and sent as separate `privmsg` calls.

## OAuth token management (`_TwitchAPI`)

- `_refresh_token()` POSTs `https://id.twitch.tv/oauth2/token` with
  `grant_type=client_credentials`. `fetch_json` is GET-only, so this inlines
  the stream + size-cap pattern with a 16 KB cap (token responses are ~200
  bytes; in-code comment). The client id and secret are sent as URL query
  params (see Findings).
- The token is cached in `self._token` with expiry
  `now + expires_in - 60` (60 s safety margin). `_headers()` checks
  expiry-or-empty and refreshes under `self._token_lock` - a
  `threading.Lock`, because concurrent `.tw` invocations run in separate
  `to_thread` workers and the check-then-refresh would otherwise race
  (in-code comment states exactly this). The lock is held across the network
  POST, so during a refresh other lookups block up to the 10 s timeout -
  serialization is the intent.
- `get()` calls `fetch_json` against `https://api.twitch.tv/helix/<endpoint>`
  with `Client-ID` + `Authorization: Bearer` headers, 256 KB cap, 10 s
  timeout. The UA is popped out of the header dict and passed as
  `fetch_json`'s dedicated `ua=` parameter.

## Convenience methods

`search_channels()`, `get_streams()`, `get_channel_info()` (resolves login ->
user id via `/users`, then `/channels`, and grafts the user object in as
`ch["_user"]`), `search_games()` - all thin wrappers returning `data` lists.
`search_streams()` (category -> game_id -> live streams) is defined but no
caller exists (see Findings).

## Configuration

- Credentials: `cred(cfg, "twitch_client_id", "twitch", "twitch_client_id")`
  and `twitch_client_secret` - env overrides `INTERNETS_TWITCH_CLIENT_ID` /
  `INTERNETS_TWITCH_CLIENT_SECRET`, then secret_store, then legacy `[twitch]`.
- Keyless (either credential missing): `self._api` stays `None`,
  `is_configured()` False, `.tw` replies "Twitch API not configured".

## Failure behavior

`_dispatch_sync()` wraps every route in one `except Exception` -> `lookup
failed` (this also catches OAuth refresh failures raised out of
`_headers()`). Empty result sets get per-route messages (`no channels found`,
`no live streams`, `channel '<x>' not found`, `no games found`). Rate limit
check before the thread spawn.

## Security notes

- All Helix GETs go through the size-capped `fetch_json`; the OAuth POST uses
  the sanctioned inline cap.
- The client secret never appears in IRC output (`lookup failed` is generic).
- Display names/titles from Helix are spliced into IRC lines without
  `strip_ctrl`; the sender strips CR/LF/NUL at the wire and Twitch constrains
  these fields upstream, but this is looser than the batch norm.

## Findings

- questionable | twitch.py - `_TwitchAPI._refresh_token()` | client id and
  secret are sent as URL query parameters on the POST instead of a form body,
  exposing the secret to intermediary/edge request-URL logging; Twitch's
  documented flow posts them in the body [unverified - current Twitch docs
  not re-checked from this session].
- defect | twitch.py - `_TwitchAPI.search_streams()` | dead code - no caller
  anywhere in the module or repo (`_dispatch_sync` routes `-g` to
  `search_games`, not to game streams), so the category-to-live-streams
  feature it implements is unreachable.
- questionable | twitch.py - `_dispatch_sync()` | on an OAuth failure the
  logged exception text includes the token-endpoint URL with
  `client_secret=<secret>` in the query string (log-only leak; consequence of
  the query-param choice above).
- questionable | twitch.py - `_dispatch_sync()` (channel route) | Helix
  removed `view_count` from the `/users` payload, so `Views` likely renders 0
  for every channel [unverified - based on Twitch API deprecation knowledge,
  not a live check].
- test-gap | twitch.py - `_TwitchAPI` | no tests cover token caching/expiry,
  refresh locking, or subcommand parsing.
