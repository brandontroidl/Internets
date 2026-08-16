# satpass.py - satellite visible passes via N2YO (`.passes`)

The only key-required module in the news/space batch. Wraps N2YO's `visualpasses`
endpoint; without `n2yo_api_key` the module is hidden from `.help` and replies with
a how-to-get-a-key message if invoked anyway. Base contract: [base](base.md).

## Commands

| Command | Args | Behavior |
|---|---|---|
| `.passes` | `<sat> <lat,lon>` | Next visible pass of the satellite from that location within 5 days: start time (UTC), max elevation, duration. |

`<sat>` is a NORAD id or one of 14 common names in `satpass.py - _SATS`
(iss/zarya, hst/hubble, css/tiangong, noaa-15/18/19, terra, aqua, landsat-8/9,
envisat). Handler: `satpass.py - SatpassModule.cmd_passes()`.

Input validation order: rate-limit gate, key presence, arg count (usage reply),
satellite name/id resolution (unknown -> list of the first 6 names), `lat,lon`
parse (two floats split on comma), range check (-90..90 / -180..180). Only then is
the network touched. Behavior pinned in `tests/test_satpass.py`
(`test_bad_location`, `test_unknown_sat`, `test_named_sat_resolves`,
`test_no_key_message`).

## Integration

`GET https://api.n2yo.com/rest/v1/satellite/visualpasses/{satid}/{lat}/{lon}/0/5/30/?apiKey=<key>`
via `base.fetch_json` - timeout 12 s, default 256 KiB cap. Path segments are
observer altitude 0 m, 5-day window, minimum 30 s visibility. The response's
`info.satname` and the first entry of `passes` are formatted; `startUTC` is a unix
timestamp rendered as UTC.

## Location flow and privacy

The location is **explicitly typed by the invoking user** as `lat,lon` - the module
never derives, geolocates, stores, or defaults a location. Whatever coordinates the
user types are transmitted to N2YO in the URL path (alongside the operator's API
key as a query parameter and the shared weather UA). Consequences worth knowing:

- A user who types their real home coordinates in a public channel has already
  disclosed them to the channel; the module additionally forwards them to N2YO
  (and into N2YO's logs). Nothing is cached or persisted bot-side.
- The invoking nick is never sent upstream.

## Configuration

- `n2yo_api_key` (env `INTERNETS_N2YO_API_KEY`, config fallback
  `[satpass] n2yo_api_key`) - required. `SatpassModule.is_configured()` returns
  False without it, which hides the command from `.help` (dispatch still works per
  the `BotModule.is_configured` contract, so the in-command keyless message is
  reachable, not dead code).
- `weather_user_agent` (env `INTERNETS_WEATHER_USER_AGENT`) - UA.

Key gating verified by `tests/test_satpass.py - test_is_configured_gating`.

## Failure behavior

Transport/`ResponseTooLarge` -> "satellite pass lookup failed"; any parse error
(broad except, commented never-raise) -> "satellite pass data unavailable"; empty
`passes` -> "<name>: no visible passes in the next 5 days from here". Never raises
to the dispatcher (`test_fetch_error_friendly`, `test_fetch_no_passes`).

## Security notes

- The API key travels as a query parameter - that is N2YO's documented interface;
  the key is a free-tier token. It is never echoed to IRC or logged by the module.
- lat/lon are validated floats interpolated into the URL path, so no path-injection
  surface; satid is an int from a fixed map or `isdigit()` input.
- Upstream strings (`satname`) are `strip_ctrl`-capped before IRC.

## Findings

None.
