# iss.py - ISS position and crew (`.iss`)

Thin keyless wrapper around open-notify.org. Base contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.iss` | none | Current ISS latitude/longitude (N/S, E/W formatted) + ISS crew names and count, one line. |

Handler: `iss.py - IssModule.cmd_iss()`; rate-limit check, then `_fetch_sync` in
`asyncio.to_thread`.

## Integration

Two endpoints, both **plain HTTP** (the upstream does not serve the API over TLS):

- `http://api.open-notify.org/iss-now.json` - position (required; failure aborts).
- `http://api.open-notify.org/astros.json` - people in space; filtered to
  `craft == "ISS"` (best-effort; failure degrades to "crew data unavailable").

Fetches via the module-local `iss.py - _get_json()`: streamed, timeout 8 s, cap
`_MAX_BODY_BYTES` = 16 KiB (inline stream+cap, satisfying the repo's no-unbounded-read
rule), returns None on any transport/JSON error or over-cap body. Only the shared
weather User-Agent is sent; no user data.

## Configuration

Keyless; `is_configured()` is unconditionally True. `on_load()` reads only
`weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`) via `cred`.

## Failure behavior

- Position endpoint down/malformed -> "ISS tracker unavailable".
- Crew endpoint down or `message != "success"` -> position still reported, crew
  half says "crew data unavailable".
- Malformed numeric position values raise inside `_fetch_sync` (see Findings) and
  fall through to the dispatcher's generic catch (`internets.py` command wrapper:
  logged, user gets an "internal error" notice).

## Security notes

- Cleartext HTTP: an on-path attacker can substitute the response body. Impact is
  bounded to spoofed display text - output passes `strip_ctrl` (length 400, control
  bytes removed) and the 16 KiB cap, so no IRC injection or memory blowup; but the
  fact content itself is unauthenticated.
- No user input reaches the request at all.

## Findings

- questionable | `iss.py - _fetch_sync()` | `float(pos.get("latitude", 0))` and the
  longitude twin are unguarded: a non-numeric upstream value raises ValueError,
  escaping the module's own "ISS tracker unavailable" degradation and surfacing as
  the dispatcher's generic internal-error notice instead.
- questionable | `iss.py - _NOW/_PEOPLE` | Plain-HTTP endpoints (upstream
  limitation, not a code choice): response content is spoofable in transit;
  mitigations above bound the impact to display text.
- test-gap | `iss.py` | No test file exercises this module (parsing, degradation
  paths, or the cap).
