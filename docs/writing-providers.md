# Writing a weather provider

How to add a new upstream weather API to `weather_providers/`. Read
[providers](providers.md) first for the architecture this fits into.

A provider is a Python package that answers one or more capabilities by calling
an upstream API and returning a normalized frozen dataclass. It knows nothing
about other providers, the chain it sits in, IRC, or the user. Everything it
needs arrives as arguments.

## The contract in full

A provider must:

- Live in `weather_providers/<id>/`, one sub-package per upstream.
- Expose a class with `name: str` and `requires_key: bool` class attributes.
- Define one `async def get_*` method per capability it supports, with the exact
  method name from `weather_providers/_dispatch.py - CAPABILITY_METHODS`.
- Accept `(self, lat, lon, location, **kw)` on every capability method.
- Return the normalized dataclass for that capability, or `None` when the
  location is outside its coverage.
- Raise on a genuine failure: outage, auth, rate limit, malformed response.
- Route every HTTP request through `weather_providers/_http.py - get_json`.
- Have a factory and a `_reg()` call in `weather_providers/__init__.py`.

It must not:

- Import provider modules at `weather_providers/__init__.py` top level.
- Call `requests` or `aiohttp` directly, or use `r.json()`.
- Return fabricated, converted-from-another-provider, or default-filled values.
- Perform blocking I/O on the event loop.
- Store non-metric units in a result.

## Before you write anything

Answer four questions.

**Which capabilities?** Pick from the 14 in `CAPABILITY_METHODS`. A provider
supporting only `air_quality` is normal and fine. Do not add a capability method
you cannot populate meaningfully.

**Keyed or keyless?** Keyless providers always register. Keyed providers
register only when a credential resolves, and need entries in the secret store.

**What is the coverage?** US only, Europe only, marine only, global. Coverage
determines what the provider must return for an out-of-area request, which is
the single most commonly-got-wrong part of this contract.

**Does the upstream publish a rate or volume cap?** If so it belongs in the
quota table.

## Sub-package layout

One directory per provider, one module per endpoint, plus `__init__.py` holding
the provider class. Shared lookup tables and per-provider helpers go in
`_codes.py`.

```
weather_providers/myprovider/
    __init__.py       provider class, delegates to endpoint modules
    current.py        fetch() -> WeatherResult
    forecast.py       fetch() -> WeatherResult with forecast days
    hourly.py         fetch() -> HourlyResult
    _codes.py         condition-code tables, shared parsing helpers
```

The endpoint split is not cosmetic. Each module is small enough to read in one
sitting, the diff for a change to one endpoint touches one file, and the
provider class stays a thin readable index of what the provider can do. Look at
`weather_providers/openmeteo/` (ten endpoint modules) or
`weather_providers/nws/` (five plus `_scope.py`) for the shape at scale.

`_codes.py` also re-exports the `base.py` helpers the endpoint modules need, so
each endpoint module has a single local import:

```python
# weather_providers/myprovider/_codes.py
from ..base import deg_to_card, ms_to_kph, km_to_m  # noqa: F401

CONDITIONS = {
    1: "Clear",
    2: "Partly Cloudy",
    3: "Overcast",
}
```

## The endpoint module

Every endpoint module exposes one `async def fetch(...)` that performs the HTTP
call and maps the response into a result dataclass.

### HTTP is mandatory through `_http.get_json`

```python
from .._http import get_json

data = await get_json(url, params={...}, headers={...},
                      timeout=10, max_bytes=None)
```

`get_json` is not a convenience wrapper, it is a security control. It streams
the response body and enforces a **1 MiB size cap incrementally**, raising
`ResponseTooLargeError` the moment cumulative bytes exceed the limit, before an
oversize body can be buffered into memory. This is tagged SEC-WP-001 in the
source. A bare `r.json()` or `await resp.json()` buffers the whole body first
and defeats the cap entirely. Do not introduce one.

`get_json` also gives you, for free:

- Uniform `HTTPError` with `.status` and `.is_rate_limit`, which is what the
  dispatcher branches on to classify a failure and trip the breaker.
- A 10 second per-request transport timeout.
- aiohttp when installed, `requests` in a worker thread otherwise, same
  interface either way.
- Per-event-loop session reuse, so a long chain does not repeat TLS setup.

Raise `max_bytes` explicitly only when the upstream genuinely returns a large
payload, and say why in a comment. `noaa_coops` raises it to 8 MB for the NOAA
station list.

### Mapping into the dataclass

Populate what the upstream actually measured. Leave everything else `None`.

```python
from .._http import get_json
from ..base import WeatherResult
from ._codes import CONDITIONS, deg_to_card, ms_to_kph

_BASE = "https://api.example.com/v1"


async def fetch(key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(
        f"{_BASE}/observation",
        params={"key": key, "lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
    )
    obs = data.get("observation") or {}
    return WeatherResult(
        source="MyProvider",
        temperature=obs.get("temp_c"),
        description=CONDITIONS.get(obs.get("code"), ""),
        location=location,
        humidity=obs.get("rh"),
        wind_kph=ms_to_kph(obs.get("wind_ms")),
        wind_dir=deg_to_card(obs.get("wind_deg")),
        pressure_mb=obs.get("pressure_hpa"),
    )
```

Field rules:

- **A missing value is `None`, never a substitute.** The formatter renders
  `None` as `N/A`, and for `current` the dispatcher can gap-fill it from the
  next provider. A fabricated value is worse than a missing one and blocks the
  fill.
- **Empty `description` is `""`, not `"Unknown"`.** `""` counts as a gap and
  gets filled; `"Unknown"` reads as a real value and blocks the fill.
- **`feels_like_c` and `dewpoint_c` only when the upstream reports them for the
  same observation.** Never compute them yourself from another provider's
  numbers. `WeatherResult.derive_missing()` computes them from the primary
  observation when they are absent, which is the only correct source. See
  [the single-source rule](providers.md#the-single-source-rule).
- **Beware truthiness guards.** `if value:` drops a legitimate `0`. Use
  `if value is not None:`. Verified live instances of this bug exist in `nws`
  (pressure), `worldweatheronline` (visibility), `weatherbit` (high/low) and
  `tomorrowio` (weather code). Do not add another.
- **Every upstream string is hostile.** Text fields reach an IRC line. Do not
  pass raw HTML through; `gdacs` prefers an HTML description field and that is a
  known defect, not a pattern to copy.

### Units

Results are metric and SI. Convert at the provider boundary, once.

| Quantity | Result unit | Helper |
| --- | --- | --- |
| Temperature | Celsius | none, convert inline |
| Wind speed | km/h | `ms_to_kph` |
| Wind direction | 16-point cardinal string | `deg_to_card` |
| Pressure | millibars (hPa) | none |
| Visibility | metres | `km_to_m` |
| Precipitation | millimetres | none |
| Wave and swell height | metres | none |
| Pollutant concentration | micrograms per cubic metre | none |
| Distance | kilometres | `haversine_km` |

`modules/units.py` handles display conversion for the user. A provider that
stores Fahrenheit or mph produces a wrong number downstream with no warning.

### Timezone handling

Time strings in `HourlyEntry.time`, `AstronomyResult.sunrise` and similar fields
are rendered to the user verbatim. They must be **local to the queried
location**, not UTC and not the bot host's zone.

This is the most common latent defect in the existing providers. Verified
instances: `metno/hourly.py` parses the ISO timestamp into a UTC-aware datetime
and formats it directly, so its hour labels are UTC while every neighbouring
provider's are location-local. `currentuvindex` treats "today" as the UTC day,
so today's peak UV is wrong for users at a large offset. `sunrisesunset` sends
neither a timezone nor a date parameter while presenting the times as local.

The correct pattern is `weather_providers/openmeteo/hourly.py`: request
`timezone=auto` from the upstream, read back the `utc_offset_seconds` the
upstream reports, and align any "now" comparison to that offset rather than to
`datetime.now()` on the host. When the upstream cannot return local time, do the
offset arithmetic yourself from a coordinate-derived offset; do not label a UTC
time as local.

### The no-data contract

**Out of coverage returns `None`. It does not raise.**

The dispatcher treats `None` as "this provider has nothing here", falls through
to the next provider, and records *nothing* against health. Raising records a
failure, penalizes the latency EMA, and five failures inside 60 seconds opens
the circuit breaker for 60 seconds. A provider that raises on every out-of-area
query breaks itself for the area it *does* cover.

This was observed live: `.al cirus cirus` geocoded to Spain, NWS raised, and
`dispatch_fail` was logged for a provider behaving correctly.

Compliant examples to copy:

- `weather_providers/nws/_scope.py` is the reference implementation. It maps the
  two upstream statuses that mean "no data for this point" (400 with an
  out-of-bounds message, 404 Data Unavailable) plus the 200-with-empty-payload
  case into an internal `OutOfCoverage` exception, and `none_if_uncovered()`
  converts that to a `None` return. Every *other* status (401, 403, 429, 5xx)
  still raises, so a real outage stays a failure the breaker can act on. The
  module docstring explains why a hardcoded US bounding box was rejected:
  upstream stays the authority on its own coverage.
- `openmeteo/pollen.py`, `pollendotcom/pollen.py` and `google_pollen/pollen.py`
  all return `None` for a region their data does not cover.

Non-compliant examples, do not copy: `worldweatheronline/astronomy.py`,
`marine.py` and `historical.py` raise `ValueError` on no data;
`airnow/air_quality.py` raises `HTTPError` for a location outside US AQI
coverage. Both self-inflict health failures for correct behaviour.

The distinction to hold in your head:

| Situation | Do |
| --- | --- |
| Location outside coverage | `return None` |
| Upstream returned 200 with no usable record | `return None` |
| Upstream 401 / 403 | raise (breaker trips immediately, by design) |
| Upstream 429 | raise (dispatcher records it as rate limiting) |
| Upstream 5xx, timeout, DNS failure | raise |
| Response missing a field you require | raise |

## The provider class

`__init__.py` holds the class. It is a thin delegation layer: no HTTP, no
parsing.

```python
"""MyProvider package - current conditions and forecast.

https://example.com/docs  - free tier, 500 calls/day, global coverage.
"""
from __future__ import annotations

from ..base import WeatherResult, ForecastDay  # noqa: F401
from . import current, forecast


class MyProvider:
    name: str = "MyProvider"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kw):
        return await forecast.fetch(self._key, lat, lon, location, days)
```

Requirements:

- **`name`** is what appears in the `[source]` tag on the IRC line. Use the
  upstream's own product name. For some upstreams this is a licence obligation
  (`currentuvindex` serves CC-BY data and the source tag is the credit).
- **`requires_key`** is declarative. The factory is what actually gates
  registration, but the attribute is part of the `WeatherProvider` protocol and
  is read by status output.
- **`**kw` on every capability method, without exception.** The dispatcher
  forwards caller kwargs verbatim. `get_alerts` receives `area=` from
  `.alerts` state-widening, `get_forecast` receives `days=`, `get_hourly`
  receives `hours=`, `get_historical` receives `target_date=`. A method missing
  `**kw` raises `TypeError` on an unrelated caller's kwarg, and the dispatcher
  logs that as `dispatch_bug` because a `TypeError` is classified as a provider
  code defect rather than an upstream outage.
- **Import endpoint modules at package top level** (`from . import current`).
  That is inside the provider package, which is itself imported lazily, so it
  costs nothing until the provider is actually configured.
- Use explicit imports from `..base`, never `import *`. The star form was
  removed from this package deliberately.

Keyless providers omit `__init__` entirely; see
`weather_providers/sunrisesunset/__init__.py` for the minimal case.

## Capability discovery and partial implementations

`Dispatcher.register()` builds the capability set by calling
`hasattr(provider, method)` for every entry in `CAPABILITY_METHODS`, then
checking the attribute is callable. There is no registration list of
capabilities anywhere.

What follows from that:

- **Partial implementations are the normal case.** Implement three of fourteen
  capabilities and the dispatcher will only ever route those three to you.
- **Never stub a method you cannot serve.** A `get_marine` that always returns
  `None` costs a chain slot, a dispatch attempt and a quota tick on every
  marine query, forever. Delete the method instead.
- **Adding a capability later is one method.** No registration to update, no
  list to edit, other than the ranking table below.
- **A typo in a method name silently disables the capability.**
  `get_airquality` is not `get_air_quality`; discovery finds nothing and no
  error is raised anywhere. Copy the name from `CAPABILITY_METHODS`.

## Factory and registration

Both go in `weather_providers/__init__.py`.

```python
def _f_myprovider(cfg):
    key = _cred(cfg, "myprovider_key", "myprovider_key")
    if not key:
        log.info("myprovider: skipped (no myprovider_key)")
        return None
    from .myprovider import MyProvider  # lazy: keeps the import graph light
    return MyProvider(key)
```

```python
_reg("myprovider", _f_myprovider)
```

Rules:

- **Import the provider class inside the factory.** A top-level import pulls
  every provider's dependencies on startup, including for providers that are
  never configured.
- **Return `None` when unconfigured, do not raise.** `configure()` catches
  exceptions and logs a warning, but a clean `None` plus one INFO line naming
  the missing credential is the diagnostic an operator actually wants.
- `_cred(cfg, secret_name, ini_key)` reads the secret store first
  (`INTERNETS_<NAME>` environment variable, then `[secrets]` in `config.ini`),
  then falls back to `[weather_providers] <ini_key>`. The ini fallback exists so
  the bot keeps working before `python -m secret_store migrate` runs.
- A keyless factory is two lines: import and construct.
- Place the `_reg()` call in the block with the other `_reg()` calls. Its
  position sets the default registration order, which is the third dispatch
  sort key.
- Update `tests/test_dispatcher.py - test_factory_count_is_32` and
  `test_known_provider_set`. They pin the count and the id set deliberately.

## Credential declaration

A keyed provider needs its secret registered in **two** places in
`secret_store.py`, plus the example config.

1. `KNOWN_SECRETS` - the tuple of every secret name the bot understands. This
   drives `python -m secret_store list`, `status()`, and the env-var lookup
   surface.
2. `CONFIG_LOCATIONS` - maps the secret name to its `(section, key)` in
   `config.ini`. This drives `python -m secret_store migrate`, which relocates a
   plaintext ini value into `[secrets]` and blanks the original.

```python
# secret_store.py - KNOWN_SECRETS
    "myprovider_key",

# secret_store.py - CONFIG_LOCATIONS
    "myprovider_key":  ("weather_providers", "myprovider_key"),
```

Then add the key to `config.ini.example` under `[weather_providers]` with a
blank value and a comment naming the signup URL.

**Verified counter-example, do not repeat it:** `nasa_api_key` is read by
`modules/apod.py` and `modules/astro2.py` but appears in **neither**
`KNOWN_SECRETS` nor `CONFIG_LOCATIONS`. It works, because `secret_store.get()`
resolves `INTERNETS_NASA_API_KEY` from the environment regardless. But it is
invisible to `secret_store list` and to `status()`, so an operator auditing
configured credentials will not see it, and `migrate` will never relocate it out
of plaintext. Registering a secret is what makes it discoverable and
migratable; skipping it produces a credential nobody can find.

## Quota declaration

If the upstream publishes a cap, add it to
`weather_providers/__init__.py - _DEFAULT_QUOTA_LIMITS`:

```python
    "myprovider":  500,   # 500 calls/day free tier
```

`None` means no published cap. The counter resets at UTC midnight and is
**visibility only** - nothing throttles on it.

Two things to get right, because the existing table gets both wrong in places:

- The field is **calls per day**. Several entries store a monthly cap
  (`weatherapi`, `weatherstack`, `pirateweather`) or an hourly one
  (`airnow`, 500/hour stored per-day, understating usage by roughly 24x). If
  the upstream publishes a non-daily cap, convert it and say so in the comment,
  or use `None` rather than storing a number that means something else.
- `record_call()` fires once per dispatch **attempt**, not per HTTP request. A
  provider that makes several sequential hops per call under-counts. Note the
  hop count in the comment so an operator can scale the number mentally.

## Reliability ranking entry

Add the provider id to `weather_providers/_dispatch.py - DEFAULT_RELIABILITY`
for **every capability it implements**, at a rank reflecting the scientific
quality of the underlying model or observation network.

```python
    "current": {"nws": 1, ..., "weatherstack": 14, "myprovider": 15},
```

**Verified hazard: a provider that implements a capability but is missing from
that capability's rank table silently sorts at rank 99.** No warning, no log
line, no test failure. It goes to the back of the chain and stays there
regardless of how healthy or accurate it is.

There is a live instance in the tree right now: `stormglass` implements
`get_weather`, so it is eligible for `current`, but it does not appear in
`DEFAULT_RELIABILITY["current"]`. With a Stormglass key configured it is last in
the `current` chain permanently.

`tests/test_dispatcher.py - test_every_registered_capability_is_ranked` exists
to catch exactly this and does not, because it calls `configure()` with an empty
`ConfigParser`. Every keyed provider declines to register without a credential,
so the test only ever iterates keyless providers. **Do not rely on it. Check the
table by hand against your provider's `get_*` methods.**

Two invariants the tests do enforce, so your entry must satisfy both:

- Ranks are positive integers.
- Ranks are unique within a capability. A duplicate silently demotes one of the
  two providers to a health tie-break the author did not intend.

## Adding a new capability

Adding a provider is one package. Adding a whole new *capability* touches nine
places:

1. `_dispatch.py - CAPABILITY_METHODS` - the capability name and its method
   name.
2. `_dispatch.py - DEFAULT_RELIABILITY` - a rank map for the new capability.
3. `base.py` - a new frozen slotted result dataclass.
4. `base.py - WeatherProvider` docstring - the optional-method list.
5. `weather_providers/__init__.py` - a public `get_<capability>()` coroutine.
6. `weather_providers/__init__.py - __all__` - export the result type and the
   coroutine.
7. `modules/weather.py` - a `_format_*` function and a command entry in
   `WeatherModule.COMMANDS`.
8. `modules/weather.py - help_lines()` - the command grouping.
9. Tests for the new capability's discovery, ranking and formatting.

### The `is_empty()` requirement

**A new result dataclass must define `is_empty()`.**

`Dispatcher.dispatch()` decides whether a returned result is a real answer with
`hasattr(result, "is_empty") and result.is_empty()`. A result type without the
method can never be judged empty, so a hollow result counts as success, ends the
chain, records a health success, and the user sees a row of `N/A` while
lower-ranked providers that had the data are never tried.

This is not hypothetical. Eleven of the thirteen existing result types lack
`is_empty()` and the consequences are documented at
[providers - dispatcher fallback defect](providers.md#known-defect-fallback-is-disabled-for-11-of-14-capabilities),
including suppressed severe-weather alerts. Do not add a twelfth.

`is_empty()` returns `True` when the result carries no usable payload. Define
"usable" as the field the formatter cannot render a meaningful line without:

```python
@dataclass(frozen=True, slots=True)
class MyResult:
    source: str
    location: str
    value: float | None = None

    def is_empty(self) -> bool:
        """True when the provider responded but carries no usable payload."""
        return self.value is None
```

One deliberate exception exists and it is worth understanding before you copy
the pattern blindly: `AlertsResult` with zero alerts is a *valid* answer ("no
active alerts"), not an empty one, so its `is_empty()` would have to be
something other than "the list is empty". That nuance is why the method was
never added, and why the defect above exists. Resolve the semantics for your
type explicitly rather than omitting the method.

## Testing conventions

Two disjoint suites, both must pass:

```
pytest tests/
python tests/run_tests.py
```

Provider tests mock HTTP; nothing in the suite makes a network call. The
established patterns:

- **Capability discovery**, parametrized over provider module path, class name,
  constructor argument and expected capability set. See
  `tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`. Add a row
  for your provider; it catches a misspelled `get_*` method immediately.
- **Registration**, asserting your id is in `_PROVIDER_FACTORIES` and updating
  the pinned count and id set in `tests/test_dispatcher.py`.
- **Ranking**, asserting your provider appears in `DEFAULT_RELIABILITY` for each
  capability it implements. Write this one explicitly for your provider rather
  than trusting the generic test, for the reason in
  [Reliability ranking entry](#reliability-ranking-entry).
- **Fetch behaviour**, with `get_json` monkeypatched to return a fixture dict.
  Cover three payloads at minimum: a full response, a sparse response with the
  secondary fields absent, and the no-coverage response that must produce
  `None`. See `tests/test_provider_fixes.py` for the shape.

Make the fixtures discriminating. A fixture whose values give the correct
mapping and a plausible wrong mapping the same answer proves nothing: use
distinct values per field so a transposed pair fails.

## Complete worked example

A keyed provider serving `current` and `uv` for a fictional global API, showing
the full set of files.

### `weather_providers/example_api/_codes.py`

```python
"""ExampleAPI condition codes and shared helpers."""
from ..base import deg_to_card, ms_to_kph, km_to_m  # noqa: F401

CONDITIONS = {
    0: "Clear",
    1: "Partly Cloudy",
    2: "Overcast",
    3: "Rain",
    4: "Snow",
}


def condition(code) -> str:
    """Map an upstream condition code to a display string.

    Returns "" for an unknown or missing code so the dispatcher's
    current-conditions gap-fill can supply one from the next provider.
    """
    try:
        return CONDITIONS.get(int(code), "")
    except (TypeError, ValueError):
        return ""
```

### `weather_providers/example_api/current.py`

```python
"""ExampleAPI - current conditions."""
from __future__ import annotations

from .._http import get_json, HTTPError
from ..base import WeatherResult
from ._codes import condition, deg_to_card, ms_to_kph, km_to_m

_BASE = "https://api.example.com/v2"


async def fetch(key: str, lat: float, lon: float, location: str) -> WeatherResult:
    data = await get_json(
        f"{_BASE}/observations",
        params={"apikey": key, "lat": f"{lat:.4f}",
                "lon": f"{lon:.4f}", "units": "metric"},
    )
    if not isinstance(data, dict):
        raise HTTPError("example_api: unexpected response shape",
                        status=None, provider_hint="example_api")

    # Upstream returns an empty station list for points it does not
    # cover.  That is a coverage answer, not a failure: returning None
    # falls through to the next provider without a health penalty.
    stations = data.get("stations")
    if not stations:
        return None

    obs = stations[0].get("current") or {}
    vis_km = obs.get("visibility_km")
    return WeatherResult(
        source="ExampleAPI",
        temperature=obs.get("temperature_c"),
        description=condition(obs.get("condition_code")),
        location=location,
        humidity=obs.get("relative_humidity"),
        wind_kph=ms_to_kph(obs.get("wind_speed_ms")),
        wind_dir=deg_to_card(obs.get("wind_bearing_deg")),
        pressure_mb=obs.get("pressure_hpa"),
        visibility_m=km_to_m(vis_km) if vis_km is not None else None,
        # dewpoint_c / feels_like_c omitted: this upstream does not report
        # them, so derive_missing() computes them from the temperature
        # above rather than importing another provider's.
    )
```

Note the `vis_km is not None` guard rather than `if vis_km:`. A reported
visibility of 0 metres in dense fog is a real reading and must survive.

### `weather_providers/example_api/uv.py`

```python
"""ExampleAPI - UV index now plus today's peak."""
from __future__ import annotations

from .._http import get_json
from ..base import UVResult, uv_category

_BASE = "https://api.example.com/v2"


async def fetch(key: str, lat: float, lon: float, location: str) -> UVResult:
    data = await get_json(
        f"{_BASE}/uv",
        params={"apikey": key, "lat": f"{lat:.4f}",
                "lon": f"{lon:.4f}", "tz": "local"},
    )
    uv = (data or {}).get("uv") or {}
    now = uv.get("now")
    if now is None:
        return None
    return UVResult(
        source="ExampleAPI",
        location=location,
        uv_index=now,
        uv_max=uv.get("peak_today"),
        category=uv_category(now),
    )
```

`tz=local` is what makes "today" the location's day rather than the UTC day.
Without it, `peak_today` is wrong for users at a large offset, which is a live
defect in `currentuvindex`.

### `weather_providers/example_api/__init__.py`

```python
"""ExampleAPI provider package - current conditions and UV index.

https://example.com/api/docs  - key required, 500 calls/day free tier,
global coverage, empty station list outside covered regions.
"""
from __future__ import annotations

from ..base import WeatherResult, UVResult  # noqa: F401
from . import current, uv


class ExampleAPIProvider:
    name: str = "ExampleAPI"
    requires_key: bool = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kw):
        return await current.fetch(self._key, lat, lon, location)

    async def get_uv(self, lat, lon, location, **kw):
        return await uv.fetch(self._key, lat, lon, location)
```

No `get_forecast`. The upstream does not serve one, so the method does not
exist and the dispatcher never routes `forecast` here.

### `weather_providers/__init__.py`

```python
def _f_example_api(cfg):
    key = _cred(cfg, "example_api_key", "example_api_key")
    if not key:
        log.info("example_api: skipped (no example_api_key)")
        return None
    from .example_api import ExampleAPIProvider
    return ExampleAPIProvider(key)
```

```python
_reg("example_api", _f_example_api)
```

```python
# _DEFAULT_QUOTA_LIMITS
    "example_api":  500,   # 500/day free tier, 1 HTTP hop per dispatch
```

### `weather_providers/_dispatch.py`

```python
DEFAULT_RELIABILITY = {
    "current": {..., "weatherstack": 14, "example_api": 15},
    "uv":      {"openmeteo": 1, "currentuvindex": 2, "example_api": 3},
}
```

Both capabilities the provider implements get an entry. Missing either one
sends that capability to rank 99 with no warning.

### `secret_store.py`

```python
# KNOWN_SECRETS
    "example_api_key",

# CONFIG_LOCATIONS
    "example_api_key":  ("weather_providers", "example_api_key"),
```

### `config.ini.example`

```ini
[weather_providers]
; ExampleAPI - free key at https://example.com/signup (500 calls/day)
example_api_key =
```

### `modules/weather.py`

```python
_PROVIDER_FLAGS = {
    ...
    "example_api": "example_api", "ex": "example_api",
}
```

Optional, but it is what lets an operator force the provider with `.w -ex` to
verify it in isolation. Check for an alias collision first: aliases are global
across all providers and `-aw` already means Apple, not AccuWeather.

## Pre-merge checklist

- [ ] Package under `weather_providers/<id>/`, one module per endpoint.
- [ ] Every HTTP call goes through `_http.get_json`.
- [ ] Capability method names copied verbatim from `CAPABILITY_METHODS`.
- [ ] `**kw` on every capability method.
- [ ] Metric and SI units throughout.
- [ ] Times are local to the queried location.
- [ ] Out-of-coverage returns `None`; outage, auth and rate limit raise.
- [ ] Missing values are `None`; `if x is not None` guards, not `if x`.
- [ ] Factory imports the provider class lazily and returns `None` when
      unconfigured.
- [ ] `_reg()` call added; pinned count and id-set tests updated.
- [ ] Secret in both `KNOWN_SECRETS` and `CONFIG_LOCATIONS`.
- [ ] `config.ini.example` entry with a signup URL comment.
- [ ] Quota limit added, in calls per day, with the hop count noted.
- [ ] `DEFAULT_RELIABILITY` entry for **every** capability implemented, checked
      by hand.
- [ ] Any new result dataclass defines `is_empty()`.
- [ ] Discovery, registration, ranking and fetch tests added.
- [ ] Both suites green: `pytest tests/` and `python tests/run_tests.py`.
- [ ] Provider page added under `docs/internals/weather-providers/providers/`
      and the capability matrix in [providers](providers.md) updated.

## Related documents

- [providers](providers.md) - the architecture this plugs into.
- [writing-modules](writing-modules.md) - adding an IRC command module.
- [contributing](contributing.md) - branch, review and test workflow.
- [internals/weather-providers/index](internals/weather-providers/index.md) -
  line-level implementation reference.
