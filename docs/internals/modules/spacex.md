# spacex.py - next SpaceX launch (`.spacex`)

Keyless wrapper around Launch Library 2 (thespacedevs), with a short module-level
result cache. The old community api.spacexdata.com endpoint is dead (HTTP 525, per
the module docstring), which is why this shares the LL2 source that astro2's
`.launches` uses. Base contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.spacex` | none | Next scheduled SpaceX launch: rocket / mission, countdown + UTC time, pad + location, status abbreviation, one line. |

Handler: `spacex.py - SpacexModule.cmd_spacex()`; rate-limit check, then
`_fetch_sync` in `asyncio.to_thread`.

## Integration

`GET https://ll.thespacedevs.com/2.2.0/launch/upcoming/?search=SpaceX&limit=1` via
`base.fetch_json` - timeout 12 s, cap `_MAX_BODY_BYTES` = 512 KiB (detailed launch
records are large). One request per cache miss; rocket configuration and pad come
nested in the single result, so no follow-up calls. No auth, no user data sent.

## Configuration

Keyless; `is_configured()` unconditionally True. `on_load()` reads only
`weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`).

## State

`spacex.py - _cache` is a **module-level** dict `{"ts", "val"}` holding the last
successful formatted line for `_CACHE_TTL` = 180 s (comment: LL2's anonymous tier
allows ~15 requests/hour, so the cache keeps channel spam from exhausting it). Only
successes are cached - error strings are recomputed each call. A module reload
replaces the module object and therefore resets the cache. Being module-level
rather than instance state is a minor deviation from the batch's pattern but has no
consequence with a single instance.

## Formatting

- `_fmt_when()` parses LL2's ISO `net` (Z normalized), producing
  `T-<d>d <h>h <m>m (<UTC>)` via `_fmt_countdown()`; `T+` when the NET is in the
  past; unparseable NET falls back to the raw string (capped 32); missing NET ->
  "date TBD".
- LL2 names are `Rocket | Mission`; `_fetch_sync()` keeps only the mission half
  (`split(" | ", 1)[-1]`) because the rocket configuration is shown separately.
- Every fragment is `strip_ctrl`-capped, and the assembled line again at 400.

## Failure behavior

`ResponseTooLarge`/`ValueError`/`TypeError` and transport errors (broad second
except, commented as `requests.RequestException`) all map to "SpaceX launch data
unavailable"; an empty or malformed `results` array maps to "no upcoming SpaceX
launch found". Never raises to the dispatcher.

## Security notes

Size-capped fetch through `fetch_json`; fixed URL and parameters, no user input in
the request; all upstream strings control-stripped before IRC.

## Findings

- test-gap | `spacex.py` | No test file exercises this module (cache TTL behavior,
  name splitting, countdown sign, dict-shape tolerance).
