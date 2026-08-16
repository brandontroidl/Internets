# pollendotcom - Pollen.com / IQVIA US allergy index (keyless, unofficial API)

## Purpose

Serves the IQVIA Pollen.com "current allergy index" (0-12) plus the dominant
allergen names for a US location. Rank 2 in the `pollen` chain
(`_dispatch.py - DEFAULT_RELIABILITY`), behind `google_pollen` and ahead of
`openmeteo`.

## Responsibilities / boundaries

Belongs here: the lat/lon to US-ZIP reverse geocode, the Pollen.com lookup, and
normalization into `PollenResult`. Not here: the bot's own geocoder
(`modules/geocode.py`), category text beyond `base.py - pollen_cat_12()`, or
provider selection (see [../dispatch.md](../dispatch.md),
[../base.md](../base.md)).

Single-capability specialist: implements `get_pollen` **only**.

## Dependencies and dependents

Internal: `_http.py - get_json()`; `base.py - PollenResult`, `pollen_cat_12()`.
External: `https://nominatim.openstreetmap.org/reverse` and
`https://www.pollen.com/api/forecast/current/pollen/{zip}` - the latter is the
site's own **unofficial** XHR endpoint, not a published API.
Dependents: `__init__.py - _f_pollendotcom()`, `_dispatch.py`,
`modules/weather.py - _format_pollen()`.

## Lifecycle

Registered as id `pollendotcom` by `_reg("pollendotcom", _f_pollendotcom)`. It
is the only **keyless** provider in this group that still takes a constructor
argument: the factory resolves a User-Agent through
`_cred(cfg, "weather_user_agent", "weather_user_agent")` and always constructs
the provider, key or not - `requires_key = False`, and there is no
`return None` branch, so `pollendotcom` registers on every install. The
user-facing flags are `-pollendotcom`, `-pollencom`, and `-pc`.

Credential resolution order for the UA: `secret_store.get("weather_user_agent")`
(env `INTERNETS_WEATHER_USER_AGENT`, then `config.ini` `[secrets]
weather_user_agent` at mode 0600), then the legacy ini fallback
`[weather_providers] weather_user_agent`. See the Findings section - the
fallback tier does not match the one `modules/weather.py` uses.

## State

`PollenDotComProvider._ua`. `fetch()` holds only per-call locals. No caching of
the lat/lon to ZIP mapping, so every `.pollen` call re-runs the reverse geocode.

## Concurrency

Two sequential awaits per call; the second URL contains the ZIP produced by the
first, so they cannot be parallelized. Both consume the dispatcher's per-call
and whole-chain budgets (`_dispatch.py - _PER_CALL_BUDGET`, `_CHAIN_BUDGET`).
No locks, no shared state.

## Failure behavior

`fetch()` **returns `None`, never raises**, for every data-shaped problem - the
module docstring states the rationale directly: a coverage gap is not an error.
Four `None` paths:

- `address.country_code` is not `us`
  (`tests/test_new_weather_capabilities.py -
  TestPollenProviders.test_pollendotcom_non_us_returns_none`)
- no five-digit numeric ZIP after splitting off any ZIP+4 suffix
- no `periods` entry whose `Type` is `today` (case-insensitive)
- (implicitly) an `Index` that will not coerce to `float` still returns a
  result, with `overall_index=None`

`_dispatch.py` treats `None` as "responded, no usable data": DEBUG log, move to
the next provider, and record neither a success nor a failure - so serving a
non-US location costs this provider no health score, unlike the air-quality
specialists that raise. HTTP-level failures (status, timeout, oversize, JSON
decode) still propagate as `HTTPError` from `get_json()`.

## Security

No credential of any kind is sent to either upstream. The User-Agent is a
**contact identifier**, not a secret; it typically embeds the operator's URL or
email, which is why the shipped template files it under `[secrets]` alongside
the real credentials - it is PII-adjacent, and it is transmitted to two
third-party hosts on every call.

`zip5` is validated (`len(zip5) == 5 and zip5.isdigit()`) **before** it is
concatenated into the Pollen.com URL and into the `Referer` header, so an
upstream-supplied postcode cannot inject a path segment or a query string. That
check is the trust boundary between Nominatim's response and the second
request. Response size capped by `_http.py`. No filesystem or subprocess
access.

The `Referer` header is synthesized to look like a browser on Pollen.com's own
forecast page (`https://www.pollen.com/forecast/current/pollen/{zip5}`). It is
required for the endpoint to answer [inference from the code, not verified
upstream], and it is worth naming plainly: this provider consumes a private
endpoint by imitating the site's own front end. It carries no usage terms the
bot can point to, and it can change or start refusing non-browser clients
without notice.

## Classes

### `PollenDotComProvider` (`pollendotcom/__init__.py`)

`name = "Pollen.com"`, `requires_key = False`. Constructor takes the
**user_agent** string (not a key) and stores it as `_ua`. Sole method
`get_pollen(lat, lon, location, **kw)` delegates to `pollen.fetch()`; `**kw` is
required because the dispatcher forwards kwargs
(`tests/test_new_weather_capabilities.py` asserts `VAR_KEYWORD`).

## Functions and methods

### `pollen.fetch(user_agent, lat, lon, location)`

Async; returns `PollenResult` or `None`.

1. `ua = user_agent or "InternetsBot/1.0 (weather)"` - a hardcoded fallback when
   the configured UA is empty.
2. Reverse geocode: GET `_REV` with `format=jsonv2`, `lat`, `lon`,
   **`zoom=18`**, `addressdetails=1`, and the UA header. The zoom value is
   load-bearing and carries a long comment plus a dedicated regression test:
   Nominatim only returns a `postcode` at street/building granularity, so the
   earlier `zoom=10` (city) omitted it for most US locations, Pollen.com
   silently returned `None`, and `.pollen` fell through to the Europe-only CAMS
   provider. `tests/test_new_weather_capabilities.py -
   TestPollenProviders.test_pollendotcom_reverse_zoom_is_high_enough_for_a_zip`
   asserts `zoom >= 18` and documents that 3 of 4 sampled US cities had no
   postcode at zoom 10.
3. Country gate (`country_code == "us"`) and ZIP extraction (`postcode` split on
   `-` to drop ZIP+4, then a strict 5-digit check).
4. GET `_API + zip5` with the UA, a synthesized `Referer`, and an explicit
   `Accept` header.
5. Select the `periods` entry whose `Type` is `today`, coerce `Index` to
   `float` inside `try/except`, and take up to **four** `Triggers[].Name`
   values, dropping blanks.
6. Return `PollenResult(source="Pollen.com", overall_index=..., 
   category=pollen_cat_12(idx), triggers=...)`.

`tests/test_new_weather_capabilities.py -
TestPollenProviders.test_pollendotcom_us` pins the happy path: index 4.6 to
category `Low-Med`, triggers `("Oak", "Sagebrush")`.

## Index scale semantics

The value is the **IQVIA allergy index, 0 to 12** - a composite severity score,
not a concentration and not the same scale as either sibling provider. This is
the only pollen provider that populates `PollenResult.category` itself, via
`base.py - pollen_cat_12()`: `Low` below 2.5, `Low-Med` below 4.9, `Medium`
below 7.3, `Med-High` below 9.7, `High` at or above. `modules/weather.py -
_format_pollen()` renders this branch as
`Pollen index 4.6/12 (Low-Med) :: Top: Oak, Sagebrush`.

Contrast: `google_pollen` reports a 0-5 Universal Pollen Index per plant type
and leaves `category` empty; `openmeteo` reports per-species grains/m3.

## Coverage and quota

US only, and only where OpenStreetMap carries a postcode for the point at zoom
18 - a rural point whose nearest addressed feature has no postcode still returns
`None`. No key, no published quota, and no entry in
`__init__.py - _DEFAULT_QUOTA_LIMITS`. The practical ceiling is Nominatim's
usage policy (identifying User-Agent, caching required, roughly one request per
second), which this module does not implement.

## Implementation walk

1. UA defaulting - compatibility shim for an unset credential.
2. Reverse-geocode GET at zoom 18 - external I/O with the failure-derived
   parameter described above.
3. `country_code` gate returning `None` - coverage enforcement without a health
   penalty.
4. ZIP extraction and strict 5-digit validation - input validation and the trust
   boundary before the second request.
5. Pollen.com GET with the synthesized browser-shaped headers - external I/O.
6. `periods` scan for `Type == "today"` - protocol processing.
7. `Index` coercion inside `try/except` and the bounded 4-trigger tuple
   comprehension - defensive parsing and output bounding.
8. `PollenResult` construction with `pollen_cat_12()` - normalization.

## Findings

- defect | `__init__.py - _f_pollendotcom()` versus
  `modules/weather.py - WeatherModule.on_load()` (weather.py:555) | The two User-Agent
  lookups agree on the secret-store name (`weather_user_agent`) but **diverge on
  the ini fallback tier**: the factory calls
  `_cred(cfg, "weather_user_agent", "weather_user_agent")`, whose fallback is
  `cfg.get("weather_providers", "weather_user_agent")`, while `modules/weather.py`
  calls `cred(cfg, "weather_user_agent", "weather", "user_agent")`, whose
  fallback is `cfg.get("weather", "user_agent")`. Verified against
  `config.ini.example`: `weather_user_agent` is defined at line 204 inside
  `[secrets]`, and **neither** fallback key exists in the shipped template - the
  `[weather]` section comment says outright that the UA is not set there. So the
  two paths agree for any install that follows the template, and diverge only
  where the ini fallback is actually in play (a legacy pre-3.0.0 config, or a
  hand-edited one). The consequence when the UA resolves empty is asymmetric,
  and that is the real hazard: `modules/weather.py` **fails closed**, logging
  `no weather_user_agent configured - geocoding is disabled`, whereas
  `pollen.fetch()` **fails open**, substituting the hardcoded
  `"InternetsBot/1.0 (weather)"` and calling Nominatim anyway with a
  non-identifying UA. The same empty result also arises whenever
  `config.ini` is not mode 0600, because `secret_store.get()` refuses to read it
  and returns `""`. Note the originally reported framing - "may send an empty
  UA" - is not what happens: it sends a generic placeholder UA, which is the
  condition Nominatim's usage policy rejects.
- defect | `pollen.fetch()` versus `modules/geocode.py` | The reverse geocode is
  an uncached direct call to Nominatim on every `.pollen` invocation. The bot
  already owns a Nominatim client in `modules/geocode.py` with a TTL/LRU cache
  whose header comment states it exists specifically because "Nominatim's usage
  policy explicitly requires clients to cache results", plus negative caching so
  repeated bad queries cannot hammer the service. This provider bypasses all of
  it, duplicating the reverse-geocode code path and the policy obligation.
- questionable | `pollen.fetch()` - `_API` | The endpoint is Pollen.com's own
  internal XHR route, consumed with a synthesized `Referer` that imitates the
  site's front end. There is no contract behind it, no documented terms, and no
  version guarantee; it can begin refusing non-browser clients at any time. The
  module docstring is candid about this ("unofficial public API"), so it is a
  known and accepted risk rather than an oversight, but it is the single largest
  fragility in the pollen chain.
- questionable | `pollen.fetch()` | An `Index` that fails `float()` coercion
  yields a `PollenResult` with `overall_index=None` and `category=""` rather
  than `None`. `PollenResult` has no `is_empty()` (see [../base.md](../base.md)),
  so `_dispatch.py` scores that as a success and stops the chain; if `triggers`
  is also empty, `modules/weather.py - _format_pollen()` falls through all three
  branches and prints `No pollen data for this location.` even though
  `openmeteo` was still available behind it.
- test-gap | `pollen.fetch()` | No test covers the malformed-ZIP path
  (`postcode` present but not five digits), the missing-`today`-period path, or
  the User-Agent actually sent on either request - which is precisely the
  divergence recorded in the first finding.
