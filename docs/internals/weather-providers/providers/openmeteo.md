# openmeteo - Open-Meteo provider package (keyless, 10 capabilities)

## Purpose

Widest-capability keyless provider in the stack, and the only one the package
guarantees is present: `configure()` in `weather_providers/__init__.py`
registers `OpenMeteoProvider` unconditionally when no other provider survived
factory construction ("No providers configured - falling back to Open-Meteo").
Backed by ECMWF IFS / ICON / GFS model blends, CAMS for air quality and
pollen, ERA5 for the archive, and WaveWatch III / GFS-Wave for marine.

## Registration and credentials

| Item | Value |
| --- | --- |
| Registered id | `openmeteo` (`__init__.py - _reg("openmeteo", _f_openmeteo)`) |
| Factory | `__init__.py - _f_openmeteo()` - constructs with no arguments |
| Credential | None. `OpenMeteoProvider.requires_key = False` |
| Daily quota marker | `None` in `_DEFAULT_QUOTA_LIMITS` (no published cap) |
| User flag | `-openmeteo`, `-om` (`modules/weather.py` alias map) |

No API key, no User-Agent header, no auth of any kind is sent.

## Capabilities

Every method on `openmeteo/__init__.py - OpenMeteoProvider` forwards to one
endpoint module's `fetch()` and adds nothing else. Ten of the fourteen
capabilities in `_dispatch.CAPABILITY_METHODS`; `alerts`, `wildfire`,
`space_weather` and `tides` are not implemented.

| Capability | Method | Module | Host |
| --- | --- | --- | --- |
| current | `get_weather` | `current.py` | `api.open-meteo.com` |
| forecast | `get_forecast` | `forecast.py` | `api.open-meteo.com` |
| hourly | `get_hourly` | `hourly.py` | `api.open-meteo.com` |
| astronomy | `get_astronomy` | `astronomy.py` | `api.open-meteo.com` |
| nowcast | `get_nowcast` | `nowcast.py` | `api.open-meteo.com` |
| uv | `get_uv` | `uv.py` | `api.open-meteo.com` |
| air_quality | `get_air_quality` | `air_quality.py` | `air-quality-api.open-meteo.com` |
| pollen | `get_pollen` | `pollen.py` | `air-quality-api.open-meteo.com` |
| historical | `get_historical` | `historical.py` | `archive-api.open-meteo.com` |
| marine | `get_marine` | `marine.py` | `marine-api.open-meteo.com` |

Paths: `/v1/forecast`, `/v1/air-quality`, `/v1/archive`, `/v1/marine`.

## Request shape

All requests are a single `get_json()` GET (see [http.md](../http.md)) with
`latitude` / `longitude` and `timezone=auto`, so timestamps come back in the
location's local time along with `utc_offset_seconds`. Field selection is by
comma-separated `current=` / `hourly=` / `daily=` / `minutely_15=` lists.

- `current.py` and `hourly.py` pass `wind_speed_unit=kmh` explicitly; the
  other modules rely on Open-Meteo's defaults (Celsius, mm, hPa, metres).
- `forecast.py` sends `forecast_days=min(days, 16)`; `hourly.py` sends
  `forecast_hours=min(hours, 48)`; `astronomy.py` and `uv.py` send
  `forecast_days=1`; `nowcast.py` sends `forecast_minutely_15=48`.
- `historical.py` sends `start_date=end_date=target_date`, defaulting to
  yesterday when the caller passes `""`.

## Response mapping

Into the dataclasses in [base.md](../base.md):

- `current.fetch()` -> `WeatherResult`. `weather_code` is looked up in
  `_codes.WMO_CODES`, falling back to the literal `"Code {n}"`. Populates
  every secondary field including `feels_like_c` (`apparent_temperature`) and
  `dewpoint_c` (`dew_point_2m`), so it rarely triggers the dispatcher's
  gap-fill walk.
- `forecast.fetch()` -> `WeatherResult` with `forecast=[ForecastDay]` zipped
  from the parallel `daily.time / weather_code / temperature_2m_max /
  temperature_2m_min` arrays, day name from `datetime.strftime("%A")`. Also
  carries `current.temperature_2m` so a forecast reply can show "now".
- `hourly.fetch()` -> `HourlyResult`. The window start is found by comparing
  each local `time` against now shifted by the response's
  `utc_offset_seconds`, so the host machine's timezone cannot slide the
  window. `_at()` indexes each parallel array only when it is long enough.
- `air_quality.fetch()` -> `AirQualityResult`; `us_aqi` is coerced with
  `int()` and categorised by `base.aqi_category()`; `aerosol_optical_depth`
  fills `aod`.
- `astronomy.fetch()` -> `AstronomyResult` with sunrise, sunset and a
  `daylight_duration` seconds value rendered as `"{h}h {m}m"`. No moon data.
- `historical.fetch()` -> `HistoricalResult` from the ERA5 archive daily
  arrays (`_first()` takes element 0).
- `marine.fetch()` -> `MarineResult` (wave, swell, wind-wave; no water
  temperature requested).
- `nowcast.fetch()` -> `NowcastResult` over 15-minute steps, with the same
  `utc_offset_seconds` alignment as `hourly`; `_intensity()` buckets mm and
  `_ptype()` derives rain/snow from the WMO description text.
- `uv.fetch()` -> `UVResult` (current index plus today's max), categorised by
  `base.uv_category()`.

## Failure behavior

No module catches `HTTPError`; every transport, status or decode failure
propagates to the dispatcher, which records a provider failure and falls
through ([dispatch.md](../dispatch.md)). Two deliberate no-data paths:

- `pollen.fetch()` returns `None` when every CAMS taxon is null (CAMS is
  Europe-only), so the dispatcher falls through to Pollen.com / Google Pollen
  without recording a failure.
- A sparse `current` response yields a `WeatherResult` whose
  `is_empty()`/`has_gaps()` drive fall-through or gap-fill in the dispatcher.

Rate limiting is not handled here; a 429 becomes `HTTPError.is_rate_limit`
in [http.md](../http.md) and is accounted for by `_health.py`.

## Caching

None. Every call is a fresh HTTP request; the only reuse is the shared
`aiohttp` session in `_http.py`.

## Quirks and upstream limits

- Free and keyless for non-commercial use; the package sends no attribution
  or identifying header.
- CAMS pollen and `aerosol_optical_depth` have real coverage only over
  Europe; pollen handles this explicitly, air quality does not (`aod` simply
  comes back null).
- `forecast_days` is capped at 16 by the API and by the package
  (`_MAX_FORECAST_DAYS` in `__init__.py`); `forecast_hours` at 48.
- The archive endpoint is a reanalysis product and the implementation implies
  nothing about its latency; a "yesterday" default can legitimately return an
  all-null day if ERA5 has not yet been extended that far. [unverified -
  not checked against upstream docs in this session]

## Tests

- `tests/test_new_weather_capabilities.py -
  TestTimezoneWindows.test_openmeteo_hourly_uses_utc_offset` pins the
  `utc_offset_seconds` window selection with a UTC+5 payload.
- `tests/test_new_weather_capabilities.py -
  TestPollen.test_openmeteo_empty_returns_none` pins the all-null CAMS ->
  `None` fall-through.
- `tests/run_tests.py` asserts `WMO_CODES[0] == "Clear"`.
- `tests/test_dispatcher.py` covers the registration and ranking, not the
  parsing.

## Findings

- questionable | `openmeteo/nowcast.py - fetch()` | `steps` defaults to 8 and
  `OpenMeteoProvider.get_nowcast()` never forwards a caller value, yet the
  request asks for `forecast_minutely_15=48` (12 hours); 40 of the 48 slots
  are fetched and discarded on every nowcast call.
- questionable | `openmeteo/astronomy.py - fetch()` | Returns no moonrise,
  moonset, phase or illumination while holding rank 2 in
  `_dispatch.DEFAULT_RELIABILITY["astronomy"]`; `AstronomyResult` has no
  `is_empty()`, so if the rank-1 `sunrisesunset` provider is unavailable the
  dispatcher returns this moon-less result and never reaches `weatherapi`
  (rank 3), which does carry moon data.
- questionable | `openmeteo/air_quality.py - fetch()` | `int(aqi)` is
  unguarded; a non-numeric `us_aqi` raises `ValueError` inside the provider
  rather than yielding a partial result.
- test-gap | `openmeteo/nowcast.py - fetch()` | The 15-minute window
  alignment repeats the hourly timezone logic verbatim but has no test; only
  the hourly variant is pinned.
