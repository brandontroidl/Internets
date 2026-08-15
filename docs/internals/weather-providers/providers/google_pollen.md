# google_pollen - global tree/grass/weed Universal Pollen Index

## Purpose

Serves the Google Maps Platform Pollen API's daily tree/grass/weed Universal
Pollen Index for a lat/lon. Rank 1 in the `pollen` chain
(`_dispatch.py - DEFAULT_RELIABILITY`), ahead of `pollendotcom` (US only) and
`openmeteo` (CAMS, Europe only) - it is the only globally-scoped pollen source.

## Responsibilities / boundaries

Belongs here: one GET and normalization into `PollenResult`. Not here:
geocoding (the API takes native lat/lon, so unlike `pollendotcom` there is no
reverse-geocode step), category text (`base.py - pollen_cat_5()`, applied by the
formatter), or provider selection (see [../dispatch.md](../dispatch.md),
[../base.md](../base.md)).

Single-capability specialist: implements `get_pollen` **only** - no
`get_weather`, no `get_air_quality`, nothing else.

## Dependencies and dependents

Internal: `_http.py - get_json()`; `base.py - PollenResult`, `pollen_cat_5`
(re-exported, not called here). External:
`https://pollen.googleapis.com/v1/forecast:lookup`.
Dependents: `__init__.py - _f_google_pollen()`, `_dispatch.py`,
`modules/weather.py - _format_pollen()`.

## Lifecycle

Registered as id `google_pollen` by `_reg("google_pollen", _f_google_pollen)`.
The factory resolves `_cred(cfg, "google_pollen_key", "google_pollen_key")`:
`secret_store.get("google_pollen_key")` (env `INTERNETS_GOOGLE_POLLEN_KEY`, then
`config.ini` `[secrets] google_pollen_key` at mode 0600), falling back to the
legacy `[weather_providers] google_pollen_key`. The credential is a Google Maps
Platform API key **with the Pollen API enabled** - a valid Maps key without that
product returns an error. No key logs `google_pollen: skipped (no
google_pollen_key)` and returns `None`, leaving the `-googlepollen` /
`-google_pollen` / `-gp` flags inactive.

## State

`GooglePollenProvider._key`. `fetch()` builds one local `by_code` dict per call.
Nothing persists.

## Concurrency

One `await get_json()` per call. No locks, no shared mutable state.

## Failure behavior

This provider **returns `None` rather than raising** on a data gap - the
opposite convention from the air-quality specialists. Two `None` paths:

- `dailyInfo` absent or empty (`tests/test_new_weather_capabilities.py -
  TestPollenProviders.test_google_pollen_no_data_returns_none`)
- `dailyInfo[0]` present but no `pollenTypeInfo` entry yields a usable numeric
  index

`_dispatch.py - Dispatcher.dispatch()` treats a `None` result as "responded, no
usable data": it logs at DEBUG, moves to the next provider, and explicitly does
**not** record a success - so a no-data answer neither resets the circuit
breaker nor masks a brownout. It also records no failure, so unlike `airnow` a
coverage gap costs this provider no health score. HTTP-level failures (status,
timeout, oversize, JSON decode) still propagate as `HTTPError` from
`get_json()`.

Per-entry parsing is defensive: non-dict entries are skipped, and `float()`
failures are swallowed with `pass` so one malformed pollen type cannot lose the
other two.

## Security

The API key travels as the `key` **query parameter** via `get_json(params=...)`,
so it is absent from the URL string `_http.py` embeds in `HTTPError` messages,
and `_dispatch.py - _redact()` scrubs `key=` from warning logs as a second
layer. A Google Maps Platform key is billable, which raises the cost of a leak
above the other providers here - it should carry an API restriction to the
Pollen API and, where possible, a server IP restriction, neither of which the
bot can enforce. Response size capped by `_http.py`. No filesystem or subprocess
access.

## Classes

### `GooglePollenProvider` (`google_pollen/__init__.py`)

`name = "Google Pollen"`, `requires_key = True`. Constructor stores the key as
`_key`. Sole method `get_pollen(lat, lon, location, **kw)` delegates to
`pollen.fetch()`; `**kw` is required because the dispatcher forwards kwargs
(`tests/test_new_weather_capabilities.py` asserts `VAR_KEYWORD` on every
`get_*`).

## Functions and methods

### `pollen.fetch(key, lat, lon, location)`

Async; returns `PollenResult` or `None`. GETs `_API` with `key`,
`location.latitude`, `location.longitude`, `days=1`, and
`plantsDescription="false"` (the last suppresses the verbose per-plant prose the
bot has no room to print). Reads `dailyInfo[0].pollenTypeInfo`, collecting
`indexInfo.value` keyed by `code` (`TREE`, `GRASS`, `WEED`), and maps those onto
`PollenResult.tree_index` / `grass_index` / `weed_index`.
`tests/test_new_weather_capabilities.py -
TestPollenProviders.test_google_pollen` pins the mapping: codes TREE/GRASS/WEED
with values 3/1/0 produce `(3.0, 1.0, 0.0)`.

## Index scale semantics

The values are Google's **Universal Pollen Index (UPI), 0 to 5** - a
cross-species severity scale, not a concentration. That makes it
dimensionally different from the other two pollen providers:

| Provider | Scale | Fields populated |
| --- | --- | --- |
| google_pollen | UPI 0-5 per type | `tree_index`, `grass_index`, `weed_index` |
| pollendotcom | IQVIA index 0-12 overall | `overall_index`, `category`, `triggers` |
| openmeteo | grains/m3 per species | `alder` ... `ragweed` |

`modules/weather.py - _format_pollen()` branches on which group is populated and
renders this one as `Tree High (4/5) :: Grass Low (2/5) ...`, deriving each
label with `base.py - pollen_cat_5()` (`None`, `Very Low`, `Low`, `Moderate`,
`High`, `Very High` at integer positions 0-5). This module therefore leaves
`PollenResult.category` empty on purpose - the label is per-type, not overall,
and cannot be expressed in the single `category` field.

## Coverage and quota

Global, subject to Google's own model coverage - the API reports no data for
locations outside its supported area, which is the `None` path above.
Only one forecast day is requested. There is no entry for `google_pollen` in
`__init__.py - _DEFAULT_QUOTA_LIMITS`, so `quota_status("google_pollen")`
reports `limit=None` even though the upstream is metered and billed per request.

## Implementation walk

1. GET with the key and native lat/lon parameters - external I/O, credential
   injection.
2. `isinstance(data, dict)` guard plus `dailyInfo` extraction, returning `None`
   when empty - fail-soft coverage handling.
3. `pollenTypeInfo` loop with `isinstance` skip, nested `indexInfo.value`
   read, and `float()` inside `try/except (TypeError, ValueError)` - defensive
   parsing.
4. Empty-`by_code` `None` return - fall-through rather than an all-empty result.
5. `PollenResult` construction with `by_code.get(...)` per type, leaving absent
   types as `None` - normalization.

## Findings

- questionable | `google_pollen/pollen.py` | `pollen_cat_5` is imported with a
  `# noqa: F401` and never used in the module; the category is computed by
  `modules/weather.py - _format_pollen()` instead. The same import in
  `google_pollen/__init__.py` is a deliberate re-export, but the one in
  `pollen.py` is dead.
- questionable | `pollen.fetch()` | `plantsDescription` is passed as the
  **string** `"false"` rather than a boolean. That happens to work because the
  API reads a query string, but the neighbouring `days=1` is passed as an int,
  so the file mixes conventions for the same encoder.
- questionable | `__init__.py - _DEFAULT_QUOTA_LIMITS` | No entry for
  `google_pollen`, the only billed-per-request provider in this group, so the
  `.providers` quota view shows `limit=None` for the one credential where
  overspend has a direct monetary cost.
- test-gap | `pollen.fetch()` | The tests cover the happy path and the empty
  `dailyInfo` path. Nothing covers a `pollenTypeInfo` entry with a non-numeric
  `indexInfo.value` (the swallowed `float()` failure) or a partial response
  where only one of the three codes is present.
