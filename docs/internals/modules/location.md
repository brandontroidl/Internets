# location.py - saved-location registration commands

## Purpose

`modules/location.py` owns the write/read/delete commands for a user's saved
default location - the string every weather command falls back to when
invoked with no argument. It is the invoker-only counterpart to the weather
module's consumers: everything here acts on the caller's own record.

## Commands

| Command | Alias | Handler | Behavior |
|---|---|---|---|
| `.regloc <zip\|city>` | `.register_location` | `cmd_regloc` | validate via geocode, then save the RAW string |
| `.myloc` | - | `cmd_myloc` | show saved location (re-geocoded for display) |
| `.delloc` | - | `cmd_delloc` | delete saved location |

Reply shapes: `{nick}: location set to <display>`,
`{nick}: saved location is <display> ('<raw>')`,
`{nick}: saved location removed.` / `{nick}: no saved location.`, plus usage
prompts that interpolate the configured command prefix.

## Store interaction

Persistence is entirely `store.py - Store` via the bot passthroughs
(`internets.py - IRCBot.loc_set/loc_get/loc_del`): a nick-keyed
(lowercased) dict behind `Store._loc_lock`, flushed to `locations.json` by
the store's writer thread. The module itself keeps no state beyond `_ua` and
`_default_country` set in `on_load()`.

The stored value is the user's RAW input string, not the resolved
coordinates: `cmd_regloc()` geocodes `arg` only to prove it resolves and to
echo a human-readable name, then calls `loc_set(nick, arg)`. Consumers
(`weather.py - WeatherModule._resolve()`, `cmd_myloc()`) re-geocode the raw
string at each use - cheap because of geocode.py's 24h cache, and it keeps
one source of truth (the user's words) rather than a coordinate snapshot that
could drift from what they typed.

## Privacy model

The class docstring states the boundary explicitly: every command here acts
on the invoker's own record, keyed by their nick, so the per-user opt-out
flag is deliberately NOT consulted - an opted-out user can still set, view,
and delete their own location. The cross-user path (`.w -n <nick>`) lives in
`modules/weather.py - WeatherModule._resolve()`, where the opt-out check IS
enforced. Full erasure (`.forgetme`) deletes the saved location via
`modules/privacy.py - cmd_forgetme()` (`loc_del`), which is why this module
needs no `forget()` override of its own - the record belongs to the store,
and privacy.py owns the erasure sweep.

## Configuration

`on_load()` mirrors weather.py: `_ua` via
`base.py - cred(cfg, "weather_user_agent", "weather", "user_agent")` (secret
store first, env override `INTERNETS_WEATHER_USER_AGENT`, then
`[weather] user_agent`, else `""` - never a KeyError on a fresh install), and
`_default_country` from `[weather] default_country` (default `"us"`). A blank
UA disables geocoding inside `geocode()` itself, so `.regloc` then reports
"location not found" for everything; `is_configured()` is not overridden and
the module stays visible.

## Failure behavior

- `.regloc` with no argument: usage prompt.
- Geocode miss (or geocode disabled): `location not found: '<sanitized arg>'` -
  nothing is saved, so a saved location is always one that resolved at least
  once.
- `.myloc` when the saved string no longer geocodes (OSM data changed, UA
  removed): degrades to showing the raw string as the display name rather
  than failing.
- `.delloc` distinguishes "removed" from "nothing to remove" via
  `Store.loc_del()`'s boolean.

## Security notes

- Both the echoed user input and the geocoder's display name pass through
  `base.py - strip_ctrl()` before reaching an IRC line (the display name is
  OSM-editable data; matching weather.py's not-found path).
- `cmd_regloc()` logs `regloc {nick} -> {arg!r} ({display})` - the one place
  in the weather stack where a nick-to-location pair lands in the local bot
  log. Same data the store persists, but log-file retention may differ from
  `locations.json` (which `.forgetme` purges); noted below.
- No direct HTTP; all network I/O is inside `geocode()`.

## Notes

The module is small enough that no implementation walk is needed: three
handlers over three store calls, `help_lines()` built from
`base.py - help_row()`, and the standard `setup()` entry point. Store-level
behavior is proven by `run_tests.py - "Store: loc_set / loc_get / loc_del"`;
the no-saved-location prompt on the weather side by
`run_tests.py - "weather: no saved location prompts for regloc..."`.

## Findings

- questionable | location.py - LocationModule.cmd_regloc() / cmd_myloc() |
  Neither handler checks `IRCBot.rate_limited()` before calling `geocode()`
  (weather.py's `_geo()` does); only the global per-nick flood cooldown
  applies, so `.regloc` spam can reach Nominatim faster than the weather
  commands are allowed to.
- questionable | location.py - LocationModule.cmd_regloc() | The
  `log.info` line writes a nick-to-location association into the bot log,
  which `.forgetme` does not purge; the store record is erasable, the log
  line is not.
- test-gap | location.py - LocationModule | No tests exercise the module's
  own handlers (regloc success/miss, myloc raw-string degradation, delloc
  both branches); only the underlying `Store` methods are tested.
