# nasapower - NASA POWER daily historical weather (not solar, despite the name)

## Purpose

Serves the `historical` capability from NASA's POWER (Prediction Of Worldwide
Energy Resources) daily point API. Keyless and global, ranked **last** of seven
for `historical` in `_dispatch.py - DEFAULT_RELIABILITY` (rank 7, behind
Open-Meteo's ERA5 reanalysis at rank 1), so it is the backstop that answers when
every keyed historical provider is unavailable.

Package layout: `weather_providers/nasapower/__init__.py` (provider class) and
`weather_providers/nasapower/historical.py` (fetch and parse).

## What it actually returns

The POWER dataset is a solar and meteorological archive built for renewable
energy siting, but this integration requests **only the meteorological daily
means** and returns a plain `HistoricalResult` (`../base.md`). No irradiance,
no solar parameter, no climatology normals reach the bot. The six requested
parameters are:

| POWER parameter | `HistoricalResult` field | Notes |
| --- | --- | --- |
| `T2M` | `avg_c` | daily mean 2 m air temperature |
| `T2M_MAX` / `T2M_MIN` | `high_c` / `low_c` | daily extremes |
| `PRCPTOTCORR` | `precip_mm` | bias-corrected total precipitation |
| `RH2M` | `avg_humidity` | daily mean 2 m relative humidity |
| `WS10M_MAX` | `max_wind_kph` | m/s, multiplied by 3.6 |

`description` is set to the empty string - POWER has no condition text, so
`modules/weather.py - _format_historical()` renders numbers only.

## Registration and credentials

- Registered id `nasapower` via `_reg("nasapower", _f_nasapower)` in
  `weather_providers/__init__.py`; `_f_nasapower(cfg)` ignores `cfg` and always
  returns a provider.
- `NasaPowerProvider.requires_key = False`. Keyless government source: no secret
  name, no `INTERNETS_*` override, no `config.ini` entry.
- User-facing flags `-nasapower` and `-power` (`modules/weather.py`
  `_PROVIDER_FLAGS`), asserted in `tests/test_weather_flags.py`.

## Capabilities implemented

Exactly one: `get_historical(lat, lon, location, target_date="", **kw)`. It is
the only provider method in this group that takes a caller-supplied argument
beyond the position. Discovery asserted in
`tests/test_new_weather_capabilities.py - TestCapabilityDiscovery`.

## Endpoint and request shape

```text
GET https://power.larc.nasa.gov/api/temporal/daily/point
    ?latitude=<lat>&longitude=<lon>
    &start=<YYYYMMDD>&end=<YYYYMMDD>
    &community=RE&format=JSON
    &parameters=T2M,T2M_MAX,T2M_MIN,PRCPTOTCORR,RH2M,WS10M_MAX
```

One request, through `_http.get_json()` with the default 1 MiB cap
(`../http.md`). `start` and `end` are the same day - a single-day window.
`community=RE` selects the Renewable Energy parameter community, which is what
makes the meteorological set above available under these names.

## Date handling and data latency

`fetch()` builds the date two ways:

- **Caller supplied** `target_date` (`YYYY-MM-DD`): hyphens stripped to give
  `ymd`, and the original string is echoed back as `HistoricalResult.date`.
- **Empty**: `datetime.now() - timedelta(days=7)`. The module docstring states
  the reason - the dataset lags real-time by a few days, so a naive "yesterday"
  default would usually return an empty record. Seven days is the code's
  latency margin.

`datetime.now()` is naive host-local time, while POWER indexes days in UTC, so
the default day can be off by one relative to the dataset near midnight. The
value only selects a default, and the resulting date is echoed in the output.

Format validation lives in the **caller**, not here:
`modules/weather.py - cmd_history()` matches the first token against
`_DATE_RE = ^\d{4}-\d{2}-\d{2}$` before passing it as `target_date`. Anything
reaching `fetch()` by another path is passed through `.replace("-", "")` with no
check; it travels as a query parameter, so `_http` still encodes it.

## Parsing and the fill-value contract

POWER encodes missing observations as **-999**. `_clean(v)` maps `-999` and
`-999.0` to `None` (`_FILL`), leaving real values untouched. Two raise sites,
both `HTTPError(..., provider_hint="nasapower")` with the same message
`"NASA POWER: no data for this date"`:

1. `properties.parameter` missing or empty - the shape POWER returns for a date
   outside the archive.
2. Every one of the six values resolved to `None` after `_clean()` - a request
   that parsed but carries no usable record for that point and date.

The second check is what stops the bot printing an all-N/A historical line.
`max_wind_kph` is computed only when `WS10M_MAX` survived cleaning, so the m/s
to km/h conversion never runs on a fill value.

`tests/test_new_weather_capabilities.py - TestNasaPower` pins both behaviours:
`test_parses_date_and_fill` asserts `-999` precipitation becomes `None`, that
5.0 m/s becomes 18.0 kph, and that the echoed date is the caller's; 
`test_all_missing_raises` asserts the all-fill payload raises.

## Coverage, latency, cadence

Global - the request sends a bare lat/lon point and POWER resolves it against
its own grid; no coverage check or distance cutoff exists in this module, unlike
the station-based providers. Daily granularity only - no sub-daily
breakdown is requested and `HistoricalResult` has no hourly field. Update
cadence is upstream ingest, and the practical consequence is captured by the
7-day default described above.

## Failure behavior

Any transport, status, oversize, or decode failure surfaces as `HTTPError` from
`_http`; the two local raises above cover the parsed-but-empty cases. All carry
`provider_hint="nasapower"` so `../dispatch.md` attributes the failure and moves
on. Since this provider sits last in the historical chain, a failure here means
the command reports no data.

## Concurrency, state, lifecycle

Stateless; no instance fields. Imported lazily in `_f_nasapower()` during
`configure()` (`../init.md`). One awaited request per call.

## Security

No credential, HTTPS literal base URL, and every caller-influenced value
(`latitude`, `longitude`, `start`, `end`) travels as a query parameter that
`_http` encodes. No free-form upstream text reaches the output - only numbers
and the echoed date - so the render path handles no untrusted strings from this
provider.

## Findings

- **Questionable | `nasapower/historical.py - fetch()`** - `datetime.now()` is
  naive host-local while POWER indexes UTC days, so the 7-day default can select
  a different day than intended near midnight.
- **Questionable | `nasapower/historical.py - fetch()`** - `target_date` is
  reformatted with a bare `replace("-", "")` and never validated in this module;
  the only format guard is `_DATE_RE` in `modules/weather.py - cmd_history()`.
- **Questionable | `nasapower/historical.py - _clean()`** - equality against
  `(-999, -999.0)` only. POWER's other documented fill conventions (for example
  a `-999` embedded as a string) would pass through as real data.
