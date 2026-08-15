# eccc - Environment and Climate Change Canada alerts (keyless, Canada-only)

## Purpose

Wraps ECCC's public OGC API Features service for active Canadian weather
alerts. Single capability, no credential, no quota. Registered id `eccc`,
factory `weather_providers/__init__.py - _f_eccc()`.

## Responsibilities / boundaries

Belongs here: the bounding-box query, the status filter, and the mapping from
ECCC's `alert_type` vocabulary to the CAP-style severity buckets used by
`base.AlertEntry`. Not here: transport ([_http.py](../http.md)), selection
([_dispatch.py](../dispatch.md)), and any current/forecast capability - ECCC's
alert collection is all this package consumes.

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `eccc` (`_reg("eccc", _f_eccc)`) |
| Class | `ECCCProvider()` (no constructor arguments) |
| `name` / `requires_key` | `"ECCC"` / `False` |
| Quota limit | none: absent from `_DEFAULT_QUOTA_LIMITS`, so `quota_status()` reports `limit=None` |

Keyless: `_f_eccc()` imports and instantiates unconditionally, so the provider
is always registered. It still consumes one `record_call()` increment per
dispatch attempt like any other provider.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| alerts | `get_alerts` | 11 of 12 |

Ahead of only `metno` (12) and behind `gdacs` (10), `nws` (1) and the keyed
providers. Flag: `-eccc` (`modules/weather.py - _PROVIDER_FLAGS`).

## Endpoint and request shape

```text
https://api.weather.gc.ca/collections/weather-alerts/items?f=json&bbox=W,S,E,N
```

`alerts.fetch()` builds a 0.5-degree box around the point
(`f"{lon-d},{lat-d},{lon+d},{lat+d}"`, W,S,E,N per OGC/GeoJSON, with `d = 0.5`)
and requests GeoJSON. No `limit` parameter is sent, so the collection's server
default applies [unverified - the upstream default was not checked]. No API
key, no auth header.

## Response parsing

Iterates `features[]`, reading each feature's `properties`:

| Field | Source property |
| --- | --- |
| `event` | `alert_name_en`, then `alert_short_name_en`, else `"Alert"` |
| `severity` | `_SEVERITY[alert_type]` (see below), else `"unknown"` |
| `headline` | `"{event} - {feature_name_en}"`, or just the event |
| `start` | `validity_datetime`, then `publication_datetime` |
| `end` | `event_end_datetime`, then `expiration_datetime` |
| `description` | `alert_text_en`, truncated to 300 characters |

Features whose `status_en` is ended, cancelled or canceled are skipped. Only
the English fields are read; the collection's French equivalents are ignored,
which matches the bot's single-language output.

## Lifecycle, state, concurrency

`ECCCProvider` is constructed once per `configure()` and holds no state at all.
`get_alerts()` is a single `await alerts.fetch(...)`, safe to call
concurrently.

## Failure behavior

There is no error branch of its own: transport, status, decode and oversize
failures come out of `_http.get_json()` as `HTTPError` and are handled by
`_dispatch.Dispatcher.dispatch()` (failure recorded, chain continues). A query
outside Canada returns zero features, which the module's docstring calls
valid "no active alerts" data and returns as an empty `AlertsResult` rather
than raising. Because `AlertsResult` has no `is_empty()`, the dispatcher
returns that empty result as a success and stops the chain - which for ECCC's
rank-11 position means it can only shadow `metno`.

## Security

No credential, no filesystem access, no user-supplied URL. The bbox is built
from float lat/lon, so nothing user-controlled reaches the query string as
text. Response size is bounded by `_http`'s 1 MiB cap; a national-scale alert
day could plausibly approach that for a 1-degree box, in which case
`ResponseTooLargeError` (an `HTTPError` subclass) is raised and the dispatcher
falls through.

## Classes

`ECCCProvider` - one `async get_alerts(self, lat, lon, location, **kw)`
delegating to `alerts.fetch()`. `**kw` is required by
`tests/test_new_weather_capabilities.py - test_get_methods_accept_kwargs`,
which enforces that every new provider's `get_*` accepts the dispatcher's
forwarded keyword arguments.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `ECCCProvider.get_alerts(lat, lon, location, **kw)` | Delegates to `alerts.fetch()` |
| `alerts.fetch(lat, lon, location)` | bbox query, status filter, `AlertsResult` |

## Implementation walk

`alerts.py` is 43 lines: two module constants (`_BASE`, `_SEVERITY`), then one
function that (1) computes the bbox, (2) awaits `get_json`, (3) loops features
defensively (`(f or {}).get("properties") or {}` tolerates a null feature),
(4) skips ended and cancelled entries, (5) builds an `AlertEntry` per surviving
feature with per-slot fallbacks, and (6) returns `AlertsResult` even when the
list is empty. `__init__.py` is a 19-line class wrapper; the
`from ..base import AlertsResult, AlertEntry  # noqa: F401` line exists only to
re-export the types for callers that import them from the package.

## Tests

`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery` parametrizes
`("weather_providers.eccc", "ECCCProvider", None, {"alerts"})`, asserting that
`Dispatcher.register()` discovers exactly the `alerts` capability, and the
`_NEW_PROVIDERS` list includes ECCC in the `**kwargs` conformance test.
`tests/test_dispatcher.py` asserts `"eccc" in DEFAULT_RELIABILITY["alerts"]`
and that the id is in the known-provider set. `tests/test_weather_flags.py`
pins `-eccc`. No test exercises `alerts.fetch()` against a payload.

## Findings

- **questionable** | `eccc/alerts.py - fetch()` - the provider is registered
  globally and queried for any location, but the collection only covers Canada,
  so every non-Canadian dispatch that reaches rank 11 spends an HTTP request to
  learn nothing. A cheap lat/lon bounding check (roughly 41 to 84 N, 141 to 52
  W) would skip the call.
- **questionable** | `eccc/alerts.py - fetch()` - the 0.5-degree box is applied
  uniformly, so its east-west extent shrinks with latitude (about 83 km at 48 N
  but about 22 km at 78 N) while the north-south extent stays at about 111 km.
  Alert polygons are large enough that this rarely matters, but the asymmetry is
  undocumented.
- **questionable** | `eccc/alerts.py - fetch()` - no `limit` is requested, so
  the number of returned features is whatever the service defaults to; a
  busy-weather bbox could be silently truncated with no indication in the
  result.
- **test-gap** | `eccc/alerts.py - fetch()` - the status filter, the severity
  mapping, and the per-slot fallbacks have no payload-level test; coverage stops
  at capability discovery and signature shape.
