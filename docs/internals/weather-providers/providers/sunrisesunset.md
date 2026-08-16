# sunrisesunset - SunriseSunset.io sun and moon ephemeris

## Purpose

Serves the `astronomy` capability from api.sunrisesunset.io. Keyless, global,
and rank 1 of four for `astronomy` in `_dispatch.py - DEFAULT_RELIABILITY`.
`tests/test_dispatcher.py - test_sunrisesunset_leads_astronomy` asserts that
rank and records the rationale: ephemeris is deterministic, so all providers are
equally accurate and the ranking is by **data completeness**. The `_dispatch.py`
comment puts it as "moon-phase + illumination first"; the test comment cites
"the full moon+twilight set".

Package layout: `weather_providers/sunrisesunset/__init__.py` (provider class)
and `weather_providers/sunrisesunset/astronomy.py` (fetch and map). At 33 lines
it is the smallest provider in the package.

## What it actually returns

Sun and moon times for the current day, as **preformatted display strings**, not
timestamps. `AstronomyResult` (`../base.md`) is filled straight from the API's
`results` object with no parsing, no conversion, and no validation:

| Field | Source key | Shape |
| --- | --- | --- |
| `sunrise` / `sunset` | `sunrise` / `sunset` | 12-hour clock strings, e.g. `"6:00:00 AM"` |
| `day_length` | `day_length` | duration string, e.g. `"14:00:00"` |
| `moonrise` / `moonset` | `moonrise` / `moonset` | 12-hour clock strings |
| `moon_phase` | `moon_phase` | phase name, e.g. `"Waxing Gibbous"` |
| `moon_illumination` | `moon_illumination` | coerced to float by `_tof()` |

The API returns illumination as a **string** (`"60.5"` in the test fixture),
which is why `_tof()` exists; every other field is passed through as-is with
`""` as the default. `modules/weather.py - _format_astronomy()` prints each
non-empty field and formats illumination as a whole-number percentage.

No twilight fields (civil, nautical, astronomical dawn/dusk) are read;
`AstronomyResult` has no slots for them, so the dataclass and the mapping are
matched to this narrow set even though the reliability ranking's stated
rationale cites the twilight data.

## Registration and credentials

- Registered id `sunrisesunset` via `_reg("sunrisesunset", _f_sunrisesunset)`;
  `_f_sunrisesunset(cfg)` ignores `cfg` and always returns a provider.
- `SunriseSunsetProvider.requires_key = False`. Keyless: no secret name, no
  `INTERNETS_*` override, no `config.ini` entry.
  `tests/run_tests.py` asserts `name == "SunriseSunset"` and
  `requires_key is False`.
- User-facing flags `-sunrisesunset` and `-ss`, asserted in
  `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_astronomy(lat, lon, location, **kw)`. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoint and request shape

```text
GET https://api.sunrisesunset.io/json?lat=<lat>&lng=<lon>
```

One request through `_http.get_json()` at the default 1 MiB cap and 10 s timeout
(`../http.md`). The longitude parameter is `lng`, not `lon` - flagged by an
in-code `NOTE` because it differs from most providers in the package (and
matches `tidecheck`).

No `date`, `timezone`, or `time_format` parameter is sent, so every optional
behaviour of the API is left at its server-side default.

## Timezone convention (correctness-critical)

The module docstring asserts "Times are 12-hour local strings", but **nothing in
the request selects a timezone** and nothing in the response handling attaches
one. The returned strings carry no offset and no zone name, and
`_format_astronomy()` renders them unlabelled as
`Sunrise 6:00:00 AM :: Sunset 8:00:00 PM`. Whether those are local to the
queried coordinates or UTC is decided entirely by the upstream default, which
this code neither pins nor records. Sending an explicit `timezone` parameter
would remove the ambiguity; as written the correctness of the printed line rests
on an unpinned server-side default. The upstream default itself is not verified
here - only that this code does not set it.

Contrast `noaa_coops`, which explicitly requests `time_zone=lst_ldt`, and
`tidecheck`, whose strings arrive with a `Z` suffix.

## Failure behavior

The single guard is an envelope check:

```python
if not isinstance(data, dict) or data.get("status") != "OK":
    raise HTTPError("SunriseSunset: bad status in response",
                    status=None, provider_hint="sunrisesunset")
```

The API answers errors with HTTP 200 and a non-`OK` `status`, so a status-code
check alone would not catch them - the same pattern as `nifc`'s ArcGIS `error`
object and `firms`' plain-text body. `tests/test_new_weather_capabilities.py -
TestSunriseSunset.test_bad_status_raises` covers it. Beyond that, `_http`
failures propagate with the same provider hint, and a `results` object missing
individual keys degrades field by field to `""` / `None` rather than raising -
the resulting near-empty `AstronomyResult` still counts as a dispatch success.

## Coverage, latency, cadence

Global; the API computes ephemeris from the supplied coordinates, so there is no
station lookup, no coverage cutoff, and no fall-through path of the kind
`noaa_coops` and `tidecheck` implement. Values are computed rather than
observed, so there is no data latency; they change once per day per location and
nothing here caches them.

## Concurrency, state, lifecycle

Stateless; no instance fields. Imported lazily in `_f_sunrisesunset()` during
`configure()` (`../init.md`). One awaited request per call.

## Security

No credential. The base URL is an HTTPS literal and `lat`/`lng` travel as query
parameters that `_http` encodes. Every returned field is upstream free text that
reaches an IRC line, so the render-side defence is
`modules/weather.py - _sanitize()`, which strips C0/DEL control bytes and caps
each field at 20 chars (times), 30 (phase), or 200 by default - that cap is what
prevents a hostile response from flooding a channel line.

## Tests

`tests/test_new_weather_capabilities.py - TestSunriseSunset` covers the full
happy path (including the string-to-float illumination coercion) and the
non-`OK` status raise. `TestCapabilityDiscovery` pins the capability set to
`{"astronomy"}`, `test_get_methods_accept_kwargs` guards the dispatcher
contract, `tests/test_dispatcher.py` pins the rank-1 position, and
`tests/run_tests.py` checks the provider's declared name and key flag.

## Findings

- **Questionable | `sunrisesunset/astronomy.py - fetch()`** - no `timezone`
  parameter is sent, yet the module docstring claims the returned times are
  local. The printed line carries no zone marker either way, so a reader cannot
  tell which convention applies. This is the one correctness question in the
  file and it is settled nowhere in the repository.
- **Questionable | `sunrisesunset/astronomy.py - fetch()`** - no `date`
  parameter is sent, so the result is implicitly "today" as the server reckons
  it, which may differ from the caller's local day near midnight.
- **Doc-drift | `_dispatch.py - DEFAULT_RELIABILITY["astronomy"]`** - the rank-1
  rationale (and `tests/test_dispatcher.py` comment) cites the "full
  moon+twilight set", but `astronomy.fetch()` reads no twilight field and
  `AstronomyResult` cannot carry one.
- **Questionable | `sunrisesunset/astronomy.py - fetch()`** - a response with
  `status == "OK"` but an empty `results` object produces an
  `AstronomyResult` with every field blank, which the dispatcher records as a
  success and the formatter renders as a bare `[SunriseSunset]`.
