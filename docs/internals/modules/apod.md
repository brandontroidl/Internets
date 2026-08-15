# apod.py - NASA Astronomy Picture of the Day (`.apod`)

One-endpoint wrapper around api.nasa.gov, functional keyless via NASA's public
`DEMO_KEY`. Base contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.apod` | none | Today's APOD: date, title, explanation (truncated to 220 chars), image URL (`hdurl` preferred over `url`), one line. |

Handler: `apod.py - ApodModule.cmd_apod()`; rate-limit check, then `_fetch_sync` in
`asyncio.to_thread`.

## Integration

`GET https://api.nasa.gov/planetary/apod?api_key=<key>` via the module-local inline
stream+cap in `apod.py - _fetch_sync()`: timeout 10 s, cap `_MAX_BODY_BYTES` =
32 KiB. HTTP 429 is intercepted before `raise_for_status` and mapped to the
actionable message "APOD rate-limited - set nasa_api_key in secret_store" (DEMO_KEY
has strict quotas: this is the expected keyless failure mode). Only the key and the
shared UA are sent; no user data.

## Configuration

`is_configured()` is unconditionally True (DEMO_KEY makes the command always
usable). `on_load()` reads:

- `weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`) - UA.
- `nasa_api_key` (env `INTERNETS_NASA_API_KEY`, config fallback `[apod] api_key`,
  default `DEMO_KEY`) - same key and lookup chain the astro2 module's `.neo` uses.

The key travels as a query parameter, which is api.nasa.gov's documented interface;
with DEMO_KEY nothing secret is in transit, and a real key is a free-tier
rate-limit token, not an account credential.

## Failure behavior

Transport errors -> "APOD unavailable"; over-cap -> "APOD response too large";
JSON/shape errors (broad except) -> "APOD response parse error"; 429 -> the
rate-limit hint above. Never raises to the dispatcher.

## Security notes

Streamed size-capped read; all output fields (`title`, `explanation`, `url`) pass
`strip_ctrl` (cap 400) so a compromised upstream cannot inject IRC control bytes.
HTTPS endpoint. No user input in the request.

## Findings

- test-gap | `apod.py` | No test file exercises this module (429 branch, over-cap,
  hdurl-over-url preference).
