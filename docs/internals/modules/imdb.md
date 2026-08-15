# imdb.py - movie/TV lookup via OMDb

Thin keyed wrapper around the OMDb API. One class `ImdbModule` on the shared
[base](base.md) contract; one blocking helper `_lookup_sync()` run via
`asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.imdb <title>` | `imdb.py - ImdbModule.cmd_imdb()` | `.imdb The Matrix` | One bold-formatted line: title [year], rating, votes, genre, director, actors, runtime, plot, `https://www.imdb.com/title/<id>/` link |

No aliases. Missing arg replies with a usage line built from the configured
command prefix.

## Integration

- Endpoint: `GET https://www.omdbapi.com/` with `t=<title>&plot=short&r=json&apikey=<key>`
  (`imdb.py - _lookup_sync()`).
- Transport: `modules.base - fetch_json()` (streamed, default 256 KB cap), timeout 10 s.
- OMDb signals a miss with `Response != "True"` in a 200 body; that maps to
  `nothing found for '<title>'`.
- Every field spliced into the IRC line passes through `modules.base - strip_ctrl()`
  (control-byte strip + 400-char cap per field).
- Privacy: only the user-supplied title and the API key leave the host. No nick,
  channel, or location is sent.

## Configuration

- Key: `cred(cfg, "omdb_key", "imdb", "omdb_key")` - resolution order
  `INTERNETS_OMDB_KEY` env var, then secret_store (`config.ini [secrets]`), then
  legacy `[imdb] omdb_key`.
- User-Agent: shared `weather_user_agent` credential, default `Internets/1.0`
  (same pattern in every module of this batch).
- Keyless: `is_configured()` returns False, so `.help` hides the module; a direct
  `.imdb` invocation replies "OMDb API key not configured". `on_load()` logs a
  warning once.

## Failure behavior

`_lookup_sync()` wraps everything in one `except Exception`: transport errors,
oversized bodies (`ResponseTooLarge`), JSON errors, and missing `Title`
(`d['Title']` is a direct index) all collapse to the reply `lookup failed`
with a `log.warning`. Per-nick API rate limiting via `bot.rate_limited()` is
checked before the thread is spawned; a limited caller gets a private notice.

## Security notes

- HTTP goes through `fetch_json` (size-capped) - compliant with the repo-wide cap rule.
- Output sanitization is per-field `strip_ctrl`; the sender additionally strips
  CR/LF/NUL at the wire (`sender.py`).

## Findings

- questionable | imdb.py - `_lookup_sync()` | `log.warning(f"OMDb lookup: {e}")` -
  requests exception text embeds the full request URL including `apikey=<key>`,
  so a transport/HTTP failure writes the OMDb key into the bot log (log-only,
  not IRC; same pattern in lastfm/youtube/steam).
