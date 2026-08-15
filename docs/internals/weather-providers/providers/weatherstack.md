# weatherstack - Weatherstack provider (200-OK error envelope, 3 capabilities)

## Purpose

Wraps Weatherstack, the lowest-ranked current/forecast provider in the chain
(rank 14 of 14). Its distinguishing trait is that upstream failures arrive as
HTTP 200 with `{"success": false, "error": {...}}`, so the package has to
translate the envelope into an exception itself. Registered id `weatherstack`,
factory `weather_providers/__init__.py - _f_weatherstack()`.

## Responsibilities / boundaries

Belongs here: the envelope check, the three request shapes, and the hourly
precipitation roll-up for historical days. Not here: transport
([_http.py](../http.md)), selection and health ([_dispatch.py](../dispatch.md)),
result shapes ([base.py](../base.md)).

## Registration and credentials

| Aspect | Value |
| --- | --- |
| Registered id | `weatherstack` (`_reg("weatherstack", _f_weatherstack)`) |
| Class | `WeatherstackProvider(api_key)` |
| `name` / `requires_key` | `"Weatherstack"` / `True` |
| Quota limit | `1_000` per day (`_DEFAULT_QUOTA_LIMITS`) - see Findings |

Credential `weatherstack_key` via `__init__.py - _cred()`: `secret_store.get()`
(env `INTERNETS_WEATHERSTACK_KEY`, then `config.ini [secrets]`), else
`config.ini [weather_providers] weatherstack_key`.

## Capabilities

| Capability | Method | Reliability rank |
| --- | --- | --- |
| current | `get_weather` | 14 (last) |
| forecast | `get_forecast` | 14 (last) |
| historical | `get_historical` | 6 |

Forecast and historical are paid-plan endpoints upstream; on a free key they
return the error envelope. Flags: `-weatherstack`, `-ws`.

## Endpoints and request shape

```text
https://api.weatherstack.com/current     ?access_key=&query=lat,lon&units=m
https://api.weatherstack.com/forecast    ?...&forecast_days=min(days,7)
https://api.weatherstack.com/historical  ?...&historical_date=YYYY-MM-DD
```

Each endpoint module carries its own `_B` constant, all three now `https://`
(the `fix:` comments record that they were previously `http://`, which put
`access_key` on the wire in plaintext). The key is passed via `params=`, so it
does not appear in `HTTPError` messages. `historical.fetch()` defaults
`target_date` to yesterday when the caller passes `""`.

## Response parsing

- `current.fetch()`: `current` object. `weather_descriptions[0]` becomes the
  description (`"Unknown"` when the list is empty); `wind_dir` is already a
  cardinal string; `visibility` is kilometres and is multiplied by 1000;
  `dewpoint_c` is explicitly `None` (the API does not supply it), leaving it to
  `WeatherResult.derive_missing()`.
- `forecast.fetch()`: `forecast` is a date-keyed object, so keys are sorted and
  sliced to `days`; each day's description is taken from the *middle* hourly
  entry. `temperature` is `None` and `description` is `""`, so the result is
  usable only for the forecast capability.
- `historical.fetch()`: `historical[target_date]`; high/low/avg map directly,
  and `precip_mm` is the sum of the day's hourly `precip` values (there is no
  daily total upstream). `avg_humidity` and `description` are left empty.

## Lifecycle, state, concurrency

One instance per `configure()`, holding only `_key`. No caches, no disk, no
module state. Each method performs one `await get_json(...)` and is safe to run
concurrently.

## Failure behavior

`current._check_envelope()` (imported and reused by `forecast` and
`historical`) raises `HTTPError` with `status=None` when
`data["success"] is False`, embedding the upstream numeric code and `info`
text, and sets `is_rate_limit=True` for codes 104 (usage limit reached) and 105
(function access restricted). The dispatcher then records a rate-limited
failure through `_health.ProviderHealth.record_failure(rate_limited=True)`.
Sparse-but-successful responses yield a `WeatherResult` whose
`is_empty()` is true, and the dispatcher falls through without recording a
success.

## Security

HTTPS on all three endpoints; key in `params=` and therefore absent from error
strings; no filesystem or user-supplied URLs. Response size bounded by
`_http`'s 1 MiB cap.

## Classes

`WeatherstackProvider` - three `async get_*` delegators over `_key`. Adding a
capability requires the `get_<cap>(self, lat, lon, location, **kw)` signature
from `_dispatch.CAPABILITY_METHODS`.

## Functions and methods

| Symbol | Role |
| --- | --- |
| `current._check_envelope(data, hint)` | 200-OK error envelope to `HTTPError` |
| `current.fetch(key, lat, lon, loc)` | `current` block to `WeatherResult` |
| `forecast.fetch(..., days)` | Date-keyed `forecast` map to `ForecastDay` list |
| `historical.fetch(..., target_date)` | One past day to `HistoricalResult` |

## Tests

`tests/test_dispatcher.py` uses `weatherstack` as the low-rank provider in
`test_accuracy_beats_registration_order` (registered first, still sorted behind
NWS) and asserts it is in the known-provider set. `tests/test_weather_flags.py`
pins `-ws` / `-weatherstack`. No test exercises `_check_envelope()` or any
`fetch()`.

## Findings

- **defect** | `weather_providers/__init__.py - _DEFAULT_QUOTA_LIMITS` - the
  `weatherstack` entry is `1_000` with the comment "1000/mo, use monthly value",
  so a *monthly* cap sits in the per-day limit field. `_quota_entry_locked()`
  rolls `count` to zero at every UTC midnight, so `quota_status()` divides a
  daily counter by a monthly ceiling: usage percentage is understated by roughly
  30x and the limit can never be reached. Confirmed by reading
  `_DEFAULT_QUOTA_LIMITS`, `_quota_entry_locked()`, and `quota_status()`.
- **doc-drift** | `weatherstack/__init__.py` docstring vs
  `__init__.py - _DEFAULT_QUOTA_LIMITS` - the package says "Free tier: 250
  calls/month", the quota table's comment says 1000/mo. The two records of the
  same fact disagree.
- **questionable** | `weatherstack/current.py - _check_envelope()` - an invalid
  or inactive key (Weatherstack codes 101/102) is raised with `status=None`, so
  `_dispatch.Dispatcher.dispatch()`'s `e.status in (401, 403)` branch never
  fires and `mark_auth_failure()` is never called. A dead key keeps costing one
  upstream request per dispatch instead of tripping the breaker.
- **doc-drift** | `weatherstack/historical.py - fetch()` - the `fix:` comment
  says "we average the hourly precip values"; the code sums them
  (`precip_mm = sum(precip_values)`). The sum is the defensible choice for a
  daily total; the comment is wrong.
- **questionable** | `weatherstack/forecast.py - fetch()` - the day description
  is indexed as `d["hourly"][len(d["hourly"]) // 2]` after a truthiness check on
  the same list, and the nested `.get("weather_descriptions", ["N/A"])[0]` will
  raise `IndexError` if the key exists but is an empty list. `IndexError` is in
  `_dispatch._BUG_EXC_TYPES`, so it would be logged as a provider code defect.
- **test-gap** | `weatherstack/*` - the 200-OK error envelope, the rate-limit
  code mapping, and the historical precipitation roll-up are all untested.
