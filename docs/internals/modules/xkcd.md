# xkcd.py - xkcd comic lookup (`.xkcd`)

Keyless wrapper around xkcd.com's official JSON endpoint, with a random mode. Base
contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.xkcd` | `[num]` | With a number: that comic's number, title, alt text, link. Without: a random comic (two requests: latest to learn the max number, then the pick). |

Handler: `xkcd.py - XkcdModule.cmd_xkcd()`. Non-numeric arg replies with usage;
the number must be 1-100000 (loose upper bound; nonexistent numbers surface as
"not found" from the 404 path).

## Integration

`https://xkcd.com/info.0.json` (latest) and `https://xkcd.com/<n>/info.0.json`
(by number) via the module-local inline stream+cap `xkcd.py - _get_json()`:
timeout 8 s, cap `_MAX_BODY_BYTES` = 64 KiB, returns None on 404 or any
transport/JSON/over-cap condition. No auth, no user data sent beyond the shared
weather UA.

Random mode uses `random.SystemRandom` - the inline comment is explicit that this
is not a security decision, just cheaper than per-line `nosec` annotations for
Bandit B311. The nonexistent comic #404 is handled twice: an explicit request for
404 gets a joke explanation, and a random draw of 404 is bumped to 405 (a
negligible 2x bias toward #405).

## Configuration

Keyless; `is_configured()` unconditionally True. `on_load()` reads only
`weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`).

## Failure behavior

Latest-fetch failure -> "xkcd unavailable"; random pick fetch failure -> "xkcd
random fetch failed"; numbered fetch failure/404 -> "xkcd #<n> not found". Never
raises to the dispatcher (all fetch errors collapse to None in `_get_json`).

## Security notes

Streamed size-capped reads; the only user input is a bounds-checked int
interpolated into the URL; title and alt text pass `strip_ctrl` (cap 400) before
IRC, which matters because alt text is long free-form third-party content.

## Findings

- test-gap | `xkcd.py` | No test file exercises this module (404 special-casing,
  random-mode two-step, bounds validation).
