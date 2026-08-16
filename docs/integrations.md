# External integrations

Every third-party service the bot talks to, what unlocks it, what it costs when
it fails, and what user-derived data leaves the machine to reach it.

Two independent integration layers exist and they do not share a transport:

- **Modules** (`modules/*.py`) - one or more commands per file, HTTP through
  `modules.base.fetch_json` or an inlined stream-and-cap equivalent.
- **Weather providers** (`weather_providers/*/`) - 32 capability-based
  providers behind a ranking dispatcher, HTTP through
  `weather_providers/_http.py`.

Line-level detail per module lives under
[internals/modules/](internals/modules/index.md); the provider transport,
dispatch, and health machinery are documented in
[internals/weather-providers/](internals/weather-providers/http.md). The
credential mechanics are in [internals/secret_store.md](internals/secret_store.md)
and [configuration.md](configuration.md).

## Transport

### modules.base.fetch_json

`modules/base.py - fetch_json()` is the sanctioned outbound HTTP path for
modules. It streams the body, caps at `max_bytes + 1` raw bytes and raises
`ResponseTooLarge` **before** the body is decoded or parsed, so a JSON bomb from
a compromised upstream stays bounded. Defaults: `timeout=10`,
`max_bytes=256 KiB`. `allow_404=True` gives lookup-or-miss semantics where a 404
is an expected miss rather than an error.

A module that needs POST, XML, or a non-JSON body inlines the same
stream-and-cap pattern by hand (`requests.get(..., stream=True)` then
`r.raw.read(cap + 1)`). Both forms are size-capped. Bare `r.json()` or an
unbounded `r.text` is a defect; see [writing-modules.md](writing-modules.md).

Byte caps are set per endpoint against the payload the endpoint actually
returns, from 16 KiB (novelty JSON APIs) to 4 MiB (Wikipedia's OnThisDay feed in
`numberfact`) and 12 MiB (the RSS aggregate in `scinews`).

### weather_providers/_http.py

`weather_providers/_http.py - get_json()` uses `aiohttp` when installed and
falls back to `requests` plus `asyncio.to_thread()` otherwise, presenting the
same interface either way. Defaults: `_TIMEOUT = 10` seconds and
`_MAX_RESPONSE_BYTES = 1 MiB`, enforced incrementally in 64 KiB chunks on both
paths so an oversized body aborts before it is buffered. One
`aiohttp.ClientSession` is cached per running event loop.

No provider overrides the 10 s timeout. There is **no per-request retry** by
design: the dispatcher treats one provider failure as reason to try the next
provider, so a retry inside the transport would fight the fallback chain.

Every transport, status, and decoding failure surfaces as `HTTPError` carrying
`status` (int, or `None` for timeouts, DNS errors, JSON decode failures, and
oversized bodies), `provider_hint` (URL host only), and `is_rate_limit` (forced
true on HTTP 429), so the dispatcher branches on exception type rather than
sniffing strings. `ResponseTooLargeError` subclasses it.

One provider bypasses this helper entirely: **FIRMS** returns CSV, so
`weather_providers/firms/wildfire.py` runs its own
`urllib.request.urlopen(timeout=10)` on a worker thread with its own 1 MB cap,
importing only `HTTPError` from `_http` for consistent exception typing.

## Credentials

All credentials resolve through the two-tier secret store
(`secret_store.py - get()`): the `INTERNETS_<NAME>` environment variable wins,
then `config.ini [secrets]`, which must be mode 0600 or the store refuses to
read it. Placeholder values (`changeme`, `your-key-here`, `TODO`, ...) are
filtered at both tiers, so a template value never reaches an outbound request.

Modules reach the store through `modules.base.cred(cfg, name, section, key)`;
providers through `weather_providers.__init__._cred(cfg, name, ini_key)`. Both
fall back to a plaintext `config.ini` location, which exists only for upgrades
from 2.4.0 and earlier where keys sat directly in the ini file.

The environment variable name for any secret is `INTERNETS_` plus the canonical
name upper-cased: `nasa_api_key` becomes `INTERNETS_NASA_API_KEY`.

```{note}
The shipped `config.ini.example` defines only `[irc] [bot] [admin] [weather]
[weather_providers] [steam] [idlerpg] [qdb] [metrics] [logging] [secrets]`. The
per-module ini sections named in the tables below (`[imdb]`, `[lastfm]`,
`[youtube]`, `[stocks]`, `[twitch]`, `[search]`, `[ipintel]`, `[satpass]`,
`[apod]`) do **not** exist in the template. On a new install those keys are
reachable only through `[secrets]` or the environment; the ini path is a legacy
migration surface, not a supported place to put a new key.
```

### Weather provider credentials

| Canonical secret | Env override | config.ini fallback |
| --- | --- | --- |
| `weatherapi_key` | `INTERNETS_WEATHERAPI_KEY` | `[weather_providers] weatherapi_key` |
| `tomorrowio_key` | `INTERNETS_TOMORROWIO_KEY` | `[weather_providers] tomorrowio_key` |
| `openweathermap_key` | `INTERNETS_OPENWEATHERMAP_KEY` | `[weather_providers] openweathermap_key` |
| `visualcrossing_key` | `INTERNETS_VISUALCROSSING_KEY` | `[weather_providers] visualcrossing_key` |
| `pirateweather_key` | `INTERNETS_PIRATEWEATHER_KEY` | `[weather_providers] pirateweather_key` |
| `weatherstack_key` | `INTERNETS_WEATHERSTACK_KEY` | `[weather_providers] weatherstack_key` |
| `accuweather_key` | `INTERNETS_ACCUWEATHER_KEY` | `[weather_providers] accuweather_key` |
| `worldweatheronline_key` | `INTERNETS_WORLDWEATHERONLINE_KEY` | `[weather_providers] worldweatheronline_key` |
| `weatherbit_key` | `INTERNETS_WEATHERBIT_KEY` | `[weather_providers] weatherbit_key` |
| `stormglass_key` | `INTERNETS_STORMGLASS_KEY` | `[weather_providers] stormglass_key` |
| `meteomatics_username` | `INTERNETS_METEOMATICS_USERNAME` | `[weather_providers] meteomatics_username` |
| `meteomatics_password` | `INTERNETS_METEOMATICS_PASSWORD` | `[weather_providers] meteomatics_password` |
| `weatherkit_team_id` | `INTERNETS_WEATHERKIT_TEAM_ID` | `[weather_providers] weatherkit_team_id` |
| `weatherkit_service_id` | `INTERNETS_WEATHERKIT_SERVICE_ID` | `[weather_providers] weatherkit_service_id` |
| `weatherkit_key_id` | `INTERNETS_WEATHERKIT_KEY_ID` | `[weather_providers] weatherkit_key_id` |
| `weatherkit_key_file` | `INTERNETS_WEATHERKIT_KEY_FILE` | `[weather_providers] weatherkit_key_file` |
| `airnow_key` | `INTERNETS_AIRNOW_KEY` | `[weather_providers] airnow_key` |
| `purpleair_key` | `INTERNETS_PURPLEAIR_KEY` | `[weather_providers] purpleair_key` |
| `waqi_token` | `INTERNETS_WAQI_TOKEN` | `[weather_providers] waqi_token` |
| `openaq_key` | `INTERNETS_OPENAQ_KEY` | `[weather_providers] openaq_key` |
| `iqair_key` | `INTERNETS_IQAIR_KEY` | `[weather_providers] iqair_key` |
| `tidecheck_key` | `INTERNETS_TIDECHECK_KEY` | `[weather_providers] tidecheck_key` |
| `firms_key` | `INTERNETS_FIRMS_KEY` | `[weather_providers] firms_key` |
| `google_pollen_key` | `INTERNETS_GOOGLE_POLLEN_KEY` | `[weather_providers] google_pollen_key` |

### Module credentials

| Canonical secret | Env override | config.ini fallback |
| --- | --- | --- |
| `omdb_key` | `INTERNETS_OMDB_KEY` | `[imdb] omdb_key` |
| `lastfm_key` | `INTERNETS_LASTFM_KEY` | `[lastfm] lastfm_key` |
| `youtube_key` | `INTERNETS_YOUTUBE_KEY` | `[youtube] youtube_key` |
| `finnhub_key` | `INTERNETS_FINNHUB_KEY` | `[stocks] finnhub_key` |
| `alphavantage_key` | `INTERNETS_ALPHAVANTAGE_KEY` | `[stocks] alphavantage_key` |
| `twelvedata_key` | `INTERNETS_TWELVEDATA_KEY` | `[stocks] twelvedata_key` |
| `steam_key` | `INTERNETS_STEAM_KEY` | `[steam] steam_key` |
| `twitch_client_id` | `INTERNETS_TWITCH_CLIENT_ID` | `[twitch] twitch_client_id` |
| `twitch_client_secret` | `INTERNETS_TWITCH_CLIENT_SECRET` | `[twitch] twitch_client_secret` |
| `brave_key` | `INTERNETS_BRAVE_KEY` | `[search] brave_key` |
| `abuseipdb_key` | `INTERNETS_ABUSEIPDB_KEY` | `[ipintel] abuseipdb_key` |
| `n2yo_api_key` | `INTERNETS_N2YO_API_KEY` | `[satpass] n2yo_api_key` |
| `nasa_api_key` | `INTERNETS_NASA_API_KEY` | `[apod] api_key` |
| `weather_user_agent` | `INTERNETS_WEATHER_USER_AGENT` | `[weather] user_agent` |

```{warning}
**`nasa_api_key` is not registered.** `modules/apod.py - ApodModule.on_load()`
and `modules/astro2.py - Astro2Module.on_load()` read the secret name
`nasa_api_key`, but that name appears in neither
`secret_store.py - KNOWN_SECRETS` nor `secret_store.py - CONFIG_LOCATIONS`.
`secret_store.get()` does not gate on the registry,
so setting the value works and the env override resolves - but the key is
invisible to `python -m secret_store list` and to `status()`, and
`python -m secret_store migrate` will not relocate it out of `config.ini`. An
operator auditing which credentials the bot holds will not see it. Adding the
name to both tuples is the fix; `CONTRIBUTING.md` already states the rule.
```

### The weather_user_agent naming drift

`weather_user_agent` is not a weather setting. It is the bot's **global
outbound User-Agent contact identifier**, read by 49 of the 75 files under
`modules/` (`grep -l weather_user_agent modules/*.py`) and spliced into the `User-Agent`
header of essentially every outbound request the modules layer makes, from
`.catfact` to `.rdns`.

The name is a historical artifact of where the requirement came from: Nominatim
and several weather APIs demand a real contact URL or email in the User-Agent
and reject generic ones. It is classified in `secret_store.KNOWN_SECRETS` under
"PII / contact identifier sent in HTTP User-Agent" precisely because its value
is a real address that reaches every third party in this document.

Two consequences worth knowing before changing it:

1. **It is a policy gate, not just a label.** `modules/geocode.py -
   _ua_has_contact()` requires the configured value to embed an `@domain` or an
   `http(s)://`. If it does not, `geocode()` refuses to call Nominatim at all
   and logs a warning. Location resolution then fails closed and every command
   that depends on it (`.w`, `.regloc`, `.myloc`) reports a not-found result
   with no obvious cause. This is the only integration in the bot that can be
   disabled by a compliance check rather than a missing key.
2. **The ini fallback path resolves to two different places.** Modules read the
   legacy location `[weather] user_agent` (via `modules.base.cred`), while
   `weather_providers/__init__.py - _f_pollendotcom()` reads
   `[weather_providers] weather_user_agent`. Both consult the same secret-store
   name first, so a store-resident value is consistent; only the pre-migration
   ini fallback diverges.

In the providers layer the value is **not** a global User-Agent.
`weather_providers/_http.py` sets no default `User-Agent` at all - it forwards
whatever `headers=` the caller passes. Providers that need a distinctive agent
hardcode their own string (`weather_providers/nws/*.py`,
`weather_providers/metno/*.py`, the latter with the comment that api.met.no
rejects a missing or generic User-Agent with 403). `weather_user_agent` reaches
exactly one provider: `pollendotcom`, for its Nominatim reverse-geocode hop.

### Plaintext HTTP

Four integrations use `http://` rather than TLS. Everything on those
connections - the query, the response, and any credential in the URL - is
visible to the network path.

| Integration | Endpoint | Why |
| --- | --- | --- |
| `modules/ipinfo.py - _lookup_sync()` | `http://ip-api.com/json/<target>` | Free tier is HTTP-only; TLS requires the paid `pro.ip-api.com` |
| `modules/iss.py - _NOW` / `modules/iss.py - _PEOPLE` | `http://api.open-notify.org/iss-now.json`, `/astros.json` | Upstream offers no TLS endpoint |
| `modules/idlerpg.py - IdlerpgModule.on_load()` | `http://idlerpg.rizon.net/xml.php` | Default endpoint; overridable via `[idlerpg] api_url` |
| `modules/reflookup.py - _arxiv_fetch_xml()` | `http://export.arxiv.org/api/query` | The only cleartext URL in `reflookup`; the other seven backends are HTTPS |

None of the four carries a credential, so no key is exposed by the scheme
itself. What is exposed is the query: the IP or hostname a user asked about
(`ipinfo`), the IdleRPG player nickname (`idlerpg`), and the arXiv identifier or
free-text search (`reflookup`). `iss` sends no user data.

`weather_providers/weatherstack/` carried the same problem with a credential
attached and was fixed: the base-URL constants
`weather_providers/weatherstack/current.py - _B`,
`weather_providers/weatherstack/forecast.py - _B`, and
`weather_providers/weatherstack/historical.py - _B` each carry the comment
`# fix: was http:// - leaked access_key in plaintext query string`, and each is
now `https://api.weatherstack.com`. The module
docstring in `weather_providers/__init__.py` still describes Weatherstack as
"basic, plaintext HTTP - least preferred", which is stale; the transport is
HTTPS and only the accuracy ranking still applies.

## Weather and environment providers

32 provider packages. The dispatcher discovers capabilities by method presence
(`hasattr(provider, "get_<capability>")`), orders candidates by a hand-curated
accuracy rank, then live health score, then registration order, and falls
through the chain on failure. Only `nws` and `openmeteo` are guaranteed present
on a keyless install; every keyed provider's factory returns `None` and logs
`<id>: skipped (no <key>)` when its credential is absent, so an unconfigured
provider is simply not in the chain.

### Keyless providers

| Provider and host | Capabilities | Notes |
| --- | --- | --- |
| `nws` - api.weather.gov | current, forecast, hourly, alerts, marine | Rank 1 for current. US only; unlimited calls. Mandatory contact User-Agent hardcoded per file |
| `openmeteo` - api.open-meteo.com and 3 sibling hosts | current, forecast, hourly, air_quality, astronomy, historical, marine, nowcast, uv, pollen | Widest capability set of any provider. ECMWF/ICON/GFS multi-model, CAMS air quality, ERA5 archive |
| `metno` - api.met.no | current, forecast, hourly, alerts, nowcast | Requires a distinctive User-Agent; api.met.no answers 403 to a generic one |
| `sunrisesunset` - api.sunrisesunset.io | astronomy | Parameter is `lng`, not `lon` |
| `currentuvindex` - currentuvindex.com | uv | Data is CC-BY; the credit line is a licence obligation |
| `gdacs` - www.gdacs.org | alerts | 1000 km epicentre radius, capped at 8 alerts |
| `eccc` - api.weather.gc.ca | alerts | OGC API Features GeoJSON, roughly 0.5 degree bounding box. Canada |
| `nasapower` - power.larc.nasa.gov | historical | Fill value `-999` is mapped to `None` |
| `nifc` - services3.arcgis.com | wildfire | ArcGIS FeatureServer, 80-mile radius |
| `swpc` - services.swpc.noaa.gov | space_weather | Planetary K index plus OVATION aurora nowcast |
| `noaa_coops` - api.tidesandcurrents.noaa.gov | tides | US only; raises outside coverage so the chain falls through |
| `pollendotcom` - www.pollen.com | pollen | Keyless, but reverse-geocodes through Nominatim first. See below |

### Keyed providers

| Provider and host | Auth (secret) | Capabilities | Free-tier quota per source docstring |
| --- | --- | --- | --- |
| `meteomatics` - api.meteomatics.com | HTTP Basic (`meteomatics_username` + `meteomatics_password`) | current, forecast, hourly | "Free tier: limited, professional weather data" |
| `weatherkit` - weatherkit.apple.com | ES256 JWT (4 `weatherkit_*` values) | current, forecast, hourly, alerts | No published cap |
| `visualcrossing` - weather.visualcrossing.com | query `key` (`visualcrossing_key`) | current, forecast, hourly, alerts, historical | 1000 calls/day |
| `accuweather` - dataservice.accuweather.com | query `apikey` (`accuweather_key`) | current, forecast, hourly, alerts | 50 calls/day; each lookup costs an extra geoposition call |
| `openweathermap` - api.openweathermap.org | query `appid` (`openweathermap_key`) | current, forecast, hourly, alerts, air_quality | 60 calls/min |
| `weatherbit` - api.weatherbit.io | query `key` (`weatherbit_key`) | current, forecast, hourly, alerts, air_quality, historical | 500 calls/day |
| `weatherapi` - api.weatherapi.com | query `key` (`weatherapi_key`) | current, forecast, hourly, alerts, air_quality, astronomy, historical | 1M/month, registered as a 1,000,000/day quota limit |
| `pirateweather` - api.pirateweather.net | key in URL **path** (`pirateweather_key`) | current, forecast, hourly, alerts, nowcast | 20,000 calls/month |
| `stormglass` - api.stormglass.io | header `Authorization: <key>` (`stormglass_key`) | current, hourly, marine | 10 requests/day. Marine specialist |
| `tomorrowio` - api.tomorrow.io | query `apikey` (`tomorrowio_key`) | current, forecast, hourly, alerts, air_quality | 500/day; alerts is paid-tier only |
| `worldweatheronline` - api.worldweatheronline.com | query `key` (`worldweatheronline_key`) | current, forecast, hourly, astronomy, historical, marine | 500 calls/day |
| `weatherstack` - api.weatherstack.com | query `access_key` (`weatherstack_key`) | current, forecast, historical | 250 calls/month, current only |
| `airnow` - www.airnowapi.org | query `API_KEY` (`airnow_key`) | air_quality | 500 requests/hour. US EPA official AQI |
| `purpleair` - api.purpleair.com | header `X-API-Key` (`purpleair_key`) | air_quality | Points budget, no fixed cap. Crowdsourced, ranked below regulatory monitors |
| `waqi` - api.waqi.info | query `token` (`waqi_token`) | air_quality | None published |
| `openaq` - api.openaq.org | header `X-API-Key` (`openaq_key`) | air_quality | v3 API, 25 km search radius (the v3 maximum) |
| `iqair` - api.airvisual.com | query `key` (`iqair_key`) | air_quality | None published |
| `tidecheck` - tidecheck.com | header `X-API-Key` (`tidecheck_key`) | tides | None published |
| `firms` - firms.modaps.eosdis.nasa.gov | key in URL path (`firms_key`) | wildfire | CSV endpoint outside the shared transport |
| `google_pollen` - pollen.googleapis.com | query `key` (`google_pollen_key`) | pollen | Returns `None` where the API has no data |

None of these quota figures is enforced client-side. The bot has no per-provider
request budget; the protection against burning a 10-per-day allowance is the
dispatcher's accuracy ranking (a low-quota specialist sits low in the chain and
is reached only when better providers fail) plus the circuit breaker.

### Non-obvious providers

**WeatherKit** authenticates with a self-signed ES256 JWT rather than an API
key. `weather_providers/weatherkit/__init__.py` builds the token from
`team_id` / `service_id` / `key_id` plus a local Apple `.p8` private key, valid
55 minutes, sent as `Authorization: Bearer`. All four values are required
together and PyJWT must be installed (`pip install internets-irc[weatherkit]`);
any missing piece logs how many of the four are absent and the factory returns
`None`. Two deliberate hardening choices: the provider **refuses to load** a key
file whose POSIX mode is not 0600 or 0400, and it re-reads the key from disk on
every token refresh rather than caching the private material in memory.

**Pirate Weather** puts the API key in the URL path, not a query parameter, so
the key would appear inside any `HTTPError` message that echoes the URL. The
provider therefore routes every call through
`weather_providers/pirateweather/_codes.py - safe_get_json()`, which redacts the
key from error messages before they reach a log or the dispatcher. This is the
same failure class as the stocks defect below, caught and handled.

**Pollen.com** is keyless but needs two hops: reverse-geocode lat/lon to a US
ZIP via `nominatim.openstreetmap.org/reverse` at `zoom=18`, then query
`pollen.com/api/forecast/current/pollen/<zip5>`. The zoom level is load-bearing
and commented as such - Nominatim's default `zoom=10` returns city-level detail
with no postcode, which silently broke the provider. It reads
`weather_user_agent` for the Nominatim hop because Nominatim's usage policy
requires a distinctive agent, falling back to `"InternetsBot/1.0 (weather)"`.
Non-US locations return `None` so the chain falls through to Open-Meteo's
CAMS-based pollen.

**NIFC** queries an Esri ArcGIS FeatureServer (WFIGS Incident Locations,
Current) with a point-and-radius spatial filter rather than a weather API. The
code deliberately reads `IncidentSize` instead of `DiscoveryAcres`: the latter
reports the *initial* report size, often a placeholder `0.01`, which produced a
"Largest 0 acres" line for a 2,690-acre fire.

**FIRMS** is the only provider outside the shared transport, because NASA's area
endpoint returns CSV. An invalid key returns a plain-text error body rather than
a CSV header, which the parser detects (no `latitude` in the first line) and
raises on.

### Dispatch, fallback, and health

`weather_providers/_dispatch.py - Dispatcher.dispatch()` walks the sorted chain
under a whole-chain deadline of 45 s with a 30 s per-call cap, skipping any
provider whose circuit breaker is open. A `None` or empty result counts as "no
data" and falls through without penalizing health. For the `current` capability
specifically the dispatcher **gap-fills**: it merges partial results from up to
three providers rather than returning an all-N/A answer.

`weather_providers/_health.py` tracks per-provider EMA success rate (alpha 0.1),
EMA latency, and a time-decayed rate-limit count (300 s half-life), combined as
`0.70 * success + 0.20 * latency + 0.10 * rate-limit` and interpolated from a
0.90 cold-start default until three samples exist. On top sits a discrete
circuit breaker: five consecutive failures inside a 60 s window opens it for a
60 s cooldown, then one half-open probe decides. A 401 or 403 trips it
immediately rather than after five failures, because a bad key fails
deterministically and there is no point spending the chain budget on it.

## Lookup and reference

| Service and endpoint | Command | Auth | Request behavior |
| --- | --- | --- | --- |
| Wikipedia REST - en.wikipedia.org/api/rest_v1 | `.wiki`, `.numberfact` | none | 10 s. `reflookup` 256 KiB; `numberfact` 4 MiB for the OnThisDay feed |
| Crossref - api.crossref.org/works | `.doi` | none | 10 s, `fetch_json` |
| Open Library - openlibrary.org/api/books | `.isbn` | none | 10 s, `fetch_json` |
| Stack Exchange - api.stackexchange.com/2.3 | `.so` | none | 10 s, `fetch_json` |
| RFC Editor + IETF datatracker | `.rfc` | none | 10 s, `fetch_json` |
| tldr-pages - raw.githubusercontent.com | `.rtfm` | none | 10 s, 64 KiB raw text |
| arXiv - **http://**export.arxiv.org/api/query | `.arxiv` | none | 10 s, 256 KiB, parsed with `defusedxml` |
| OpenAlex - api.openalex.org/works | `.papers`, `.thesis`, `.scholar` | none | 10 s, `fetch_json` |
| ORCID - pub.orcid.org/v3.0/expanded-search | `.scholar` (iD lookup) | none | 10 s, `fetch_json` |
| dictionaryapi.dev | `.dict` | none | 10 s, `allow_404=True` |
| Urban Dictionary - api.urbandictionary.com/v0 | `.u` | none | 10 s, 256 KiB |
| D&D 5e SRD - dnd5eapi.co | `.dnd` | none | 10 s, up to 2 sequential calls (spells then monsters) |
| Scryfall - api.scryfall.com/cards/named | `.mtg` | none | 10 s, 256 KiB |
| PokéAPI - pokeapi.co/api/v2 | `.poke` | none | 10 s, 1 MiB |
| PyPI / npm / crates.io | `.pypi` / `.npm` / `.crates` | none | 10 s; 256 KiB / 1 MiB / 2 MiB |
| GitHub REST - api.github.com/repos | `.gh` | none (unauthenticated) | 10 s, `allow_404=True`. GitHub's 60/hour per-IP ceiling is not handled locally |
| Nominatim - nominatim.openstreetmap.org | (library, `geocode.py`) | none, but contact-UA required | 10 s. 24 h TTL, 1000-entry LRU cache |
| Zippopotam - api.zippopotam.us | (library, `geocode.py`) | none | `fetch_json`, 128 KiB |
| Google Translate (unofficial) - translate.googleapis.com | `.t` | none | 10 s, 256 KiB |
| TheMealDB / TheCocktailDB | `.recipe` / `.cocktail` | shared public test key `1` in the path | 10 s, 256 KiB |
| ECB rates via api.frankfurter.dev | `.fx` | none | 8 s, 16 KiB |

`.element`, `.http`, `.moon`, `.sky`, `.hashid`, `.cvss`, `.cipher`, and the
whole of `calc`, `mathx`, `physcalc`, `encode`, `devtools`, `units`, `netcalc`,
`dice`, `cowsay`, and `bofh` perform **no network I/O at all**. `.qr` is a
near-miss worth naming: it builds an `api.qrserver.com` URL and returns it as
text, so the bot itself never contacts the service - the user's IRC client does,
if they follow the link.

**geocode.py** is the most involved of these and has no command of its own; it
backs `location.py` (`.regloc` / `.myloc` / `.delloc`) and `weather.py`. Three
backends, a 24 h / 1000-entry TTL cache that deliberately caches negative
results so a repeated bad query does not re-hammer Nominatim, and a word-drop
retry loop capped at 4 attempts to stay inside Nominatim's 1 request/second
policy. Every upstream string is routed through `strip_ctrl` before reaching
IRC, because Nominatim `display_name` values are OSM user-editable data.

## Media, finance, and social

| Service and endpoint | Command | Auth (secret) | Request behavior |
| --- | --- | --- | --- |
| OMDb - www.omdbapi.com | `.imdb` | query `apikey` (`omdb_key`) | Required. 10 s, `fetch_json` |
| YouTube Data API v3 - googleapis.com/youtube/v3 | `.yt` | query `key` (`youtube_key`) | Required. 10 s, 2 sequential calls (search then videos) |
| YouTube oembed - youtube.com/oembed | (passive `linktitle`) | none | 8 s. Free path; `youtube_key` only upgrades the result |
| Last.fm - ws.audioscrobbler.com/2.0 | `.lastfm` | query `api_key` (`lastfm_key`) | Required. 10 s, 2 calls (getinfo then getrecenttracks) |
| Twitch Helix - api.twitch.tv/helix | `.tw` | OAuth2 client credentials (`twitch_client_id`, `twitch_client_secret`) | Required. 10 s. Token cached until `expires_in - 60` |
| Steam Web API - api.steampowered.com | `.steam`, `.regsteam` | query `key` (`steam_key`) | Required. 10 s; 256 KiB status, 1 MiB owned-games |
| Finnhub / Alpha Vantage / Twelve Data | `.stock`, `.crypto` | query `token` / `apikey` / `apikey` (`finnhub_key`, `alphavantage_key`, `twelvedata_key`) | At least one required; tried in order as failover, not retry. 10 s each |
| CoinGecko - api.coingecko.com/api/v3 | `.gecko` | none | 10 s. 2 calls (search then price); FIFO symbol cache, 512 entries |
| Brave Search - api.search.brave.com | `.sw`, `.si` | header `X-Subscription-Token` (`brave_key`) | Optional for web, required for images. 10 s |
| DuckDuckGo HTML - html.duckduckgo.com/html | `.sw` fallback | none | 10 s, POST, 512 KiB, scraped |
| Hacker News - hacker-news.firebaseio.com/v0 | `.hn` | none | 10 s, 2 calls, 64 KiB |
| Reddit - old.reddit.com/r/*/top.json | `.reddit` | none | 10 s, 512 KiB, `allow_redirects=False` |
| xkcd - xkcd.com/info.0.json | `.xkcd` | none | 8 s, 64 KiB (the smallest cap in the bot) |
| NASA APOD - api.nasa.gov/planetary/apod | `.apod` | query `api_key` (`nasa_api_key`), defaults to `DEMO_KEY` | Optional. 10 s, 32 KiB. HTTP 429 produces an actionable "set nasa_api_key" reply |
| NASA NeoWs - api.nasa.gov/neo/rest/v1 | `.neo` | same `nasa_api_key` / `DEMO_KEY` | Optional. 12 s |
| NOAA SWPC - services.swpc.noaa.gov | `.solar` | none | 10 s x2; the sunspot sub-call is best-effort |
| Launch Library 2 - ll.thespacedevs.com/2.2.0 | `.launches`, `.spacex` | none | 12 s. `spacex` caches 180 s against the roughly 15 req/hour anonymous tier |
| N2YO - api.n2yo.com/rest/v1 | `.passes` | query `apiKey` (`n2yo_api_key`) | Required. 12 s |
| is.gd - is.gd/create.php | `.shorten` | none | 10 s, `fetch_json` |
| IdleRPG - **http://**idlerpg.rizon.net/xml.php | `.irpg` | none | 10 s, 256 KiB, `defusedxml` |
| ~173 RSS/Atom feeds | `.sci` | none | 6 s per feed, 12 MiB. See below |

**Twitch** is the only OAuth integration. `_TwitchAPI._refresh_token()` POSTs
`client_id` / `client_secret` / `grant_type=client_credentials` to
`id.twitch.tv/oauth2/token`, caches the token until 60 seconds before expiry,
and serializes refresh through a `threading.Lock` so concurrent
`asyncio.to_thread` calls cannot stampede the endpoint. Helix calls then send
`Authorization: Bearer` plus `Client-ID`.

**scinews** aggregates 173 hardcoded feed URLs across 12 topic tags. Feeds are
operator constants, not user input, so they bypass the SSRF guard by design and
are fetched concurrently under an `asyncio.Semaphore(8)`. Results are merged,
deduped by normalized title, capped at 3 items per source for diversity, and
cached per topic for 120 s; the per-channel "last list" backing `.sci read <N>`
holds for 600 s. `.sci read` is the one place a scinews fetch touches a URL that
did not come from the operator's list, so it goes through `_netsafe.safe_open`.

```{note}
`docs/internals/modules/scinews.md` describes roughly 130 feeds. The source
count is 173 (`modules/scinews.py`, counted by URL literal). The source is
authoritative.
```

```{warning}
**Known defect: `.stock` and `.crypto` can publish a finance API key to the
channel.** `modules/stocks.py - _try_providers()` catches each provider failure
with `except Exception as e` and appends `f"{name}: {e}"` to an error list,
which is returned as `all providers failed for '<symbol>' (...)` and sent
straight to IRC by `cmd_stock` / `cmd_crypto`. The keys are passed as query
parameters (`token=`, `apikey=`), and `requests.HTTPError.__str__` embeds the
full request URL including the query string. Any state where every configured
provider fails on the same call - a network outage, a 401, a 429 - publishes
every configured finance key to the channel and to the logs.
`sender.redact_secrets` is log-only and does not scrub PRIVMSG.

Verified and reproduced; recorded in [known issues](known-issues.md). The fix shape
is to append the provider name and exception class only, never `str(e)` - the
same discipline `weather_providers/pirateweather/_codes.py` already applies.
The same URL-bearing `log.warning` pattern exists in `imdb`, `lastfm`,
`youtube`, `steam`, and `twitch`, which is a log-only leak and lower severity.
```

## Network and security

These commands exist to reach a target the user names, which makes them the
highest-risk surface in the bot. Every one of them is gated.

| Service and endpoint | Command | Auth (secret) | Request behavior |
| --- | --- | --- | --- |
| Cloudflare DoH - cloudflare-dns.com/dns-query | `.dns`, `.rdns`, `.caa`, `.ip` | none | 10 s (6 s for DNSBL zones), `fetch_json` |
| RDAP - rdap.org | `.whois`, `.asn` | none | 12 s, 512 KiB, `allow_404=True` |
| ip-api.com - **http://**ip-api.com/json | `.ipinfo` | none | 10 s, 32 KiB, `fields=` allowlist |
| SANS ISC / DShield - isc.sans.edu/api/ip | `.ip`, `.rep` | none | 8 s, 64 KiB |
| GreyNoise - api.greynoise.io/v3/community | `.ip`, `.rep` | none | 8 s, 16 KiB |
| AbuseIPDB - api.abuseipdb.com/api/v2/check | `.ip`, `.rep` | header `Key` (`abuseipdb_key`) | Optional; the segment is omitted without it. 8 s, 32 KiB |
| Tor exit list - check.torproject.org | `.ip`, `.rep` | none | 10 s, 4 MiB, cached 1 h |
| NVD - services.nvd.nist.gov/rest/json/cves/2.0 | `.cve` | none | 12 s, 512 KiB. NVD's ~5 req/30 s unkeyed limit is not enforced locally |
| HIBP range API - api.pwnedpasswords.com/range | `.pwn` | none | 12 s, 1 MiB. See below |
| Arbitrary user-supplied host or URL | `.headers`, `.ssl`, `.tcp`, `.down`, `.expand`, passive `linktitle` | none | 7 s HTTP, 5 s raw TCP connect. All SSRF-guarded |

**`.pwn` is k-anonymous.** `modules/secinfo.py - _pwn_sync()` hashes the
password locally with SHA-1 and sends only the **first 5 hex characters** of the
digest as a URL path segment, then matches the returned suffix list client-side.
The password and the full hash never leave the process. SHA-1 is a protocol
requirement of HIBP's range API, not a security choice, and is called with
`usedforsecurity=False` to say so. The command additionally refuses to run
in-channel and notices the user to move to PM.

**`.ip` / `.rep` fan out.** A single validated public IP goes to six sources
concurrently under `asyncio.gather(..., return_exceptions=True)`, so one dead
upstream degrades its own segment of the reply rather than aborting the command.
The target is pre-resolved and validated through `_netsafe.resolve_safe_ip`,
which means a user cannot aim the command at an internal address and have the
bot leak it to five third parties.

**The SSRF guard.** `modules/_netsafe.py` is the shared gate for every fetch of
a user-supplied URL. `ip_is_blocked()` refuses private, loopback, link-local,
multicast, reserved, unspecified, and ULA addresses and unwraps IPv4-mapped
IPv6. `resolve_safe_ip()` requires *every* resolved address for a host to pass,
which defeats a multi-answer rebinding attempt. `safe_open()` re-resolves,
re-validates, and re-pins DNS through a thread-local `getaddrinfo` patch on the
initial request **and on every redirect hop** (up to 5), closing the TOCTOU
window between check and connect. Only `http` and `https` schemes are permitted,
and the cloud metadata literals (169.254.169.254 and friends) are blocked before
resolution.

Routed through `safe_open`: `linktitle.py` (page titles), `scinews.py` (article
reader only), `urls.py` (`.expand`), `probe.py` (`.headers`, `.down`).
`probe.py`'s `.ssl` and `.tcp` use the sibling `modules.base.resolve_public()`
and then open a raw socket to a validated address. `urls.py`'s `.shorten` runs
`url_is_safe()` locally before handing the URL to is.gd, so the bot will not
even ask a third party to shorten an internal address.

Any *public* target a user names is genuinely contacted. That is the feature,
and it is the reason these commands belong on an operator's risk register: the
bot's source IP appears in the target's logs, attributed to whoever asked.

## Fun and novelty

Thin fetch-and-format wrappers, all keyless, all HTTPS, all with an 8-16 s
timeout and a 16-256 KiB cap, none with a cache or retry.

| Service | Command | Cap |
| --- | --- | --- |
| api.adviceslip.com | `.advice` | 16 KiB |
| bored-api.appbrewery.com | `.bored` | 16 KiB |
| catfact.ninja | `.catfact` | 16 KiB |
| api.chucknorris.io | `.chuck` | 16 KiB |
| icanhazdadjoke.com | `.dadjoke` | 16 KiB |
| uselessfacts.jsph.pl | `.fact` | 16 KiB |
| fmylife.com (HTML scrape) | `.fml` | 512 KiB |
| bash-org-archive.com (HTML scrape) | `.qdb` | 256 KiB |

The two scrapers have no contract with their upstream and break on a layout
change. `fml` is coupled to a Tailwind class string and reports "site layout may
have changed" when its regex finds nothing, which is honest but means a silent
upstream redesign disables the command until someone notices.

## Privacy: what leaves the machine

The bot is a relay between an IRC channel and the services above. Everything a
user types into a networked command reaches a third party. Grouped by
sensitivity:

| Class | What leaves | Which integrations |
| --- | --- | --- |
| Operator contact identifier | The `weather_user_agent` value (a real email or URL) in the `User-Agent` header | Every module HTTP call; Nominatim; `pollendotcom` |
| Real-world location | Free-text place names, postal codes, and lat/lon typed by a user | Nominatim, Zippopotam, every weather provider, `.passes` |
| Network identifiers | An IP or hostname a user asks about, and the bot's own source IP at the target | `.ipinfo`, `.ip`, `.rep`, `.dns`, `.whois`, `.asn`, `.headers`, `.ssl`, `.tcp`, `.down` |
| URLs pasted in channel | The full URL, fetched automatically with no command | passive `linktitle`; `.expand`; `.shorten` (to is.gd) |
| Free-text queries | Whatever the user typed: search terms, translation text, titles, symbols | `.sw`, `.si`, `.t`, `.yt`, `.imdb`, `.u`, `.wiki`, `.papers`, `.mtg`, `.recipe`, `.gecko` |
| Third-party account handles | A Last.fm username, Steam vanity name, Twitch channel, IdleRPG player | `.lastfm`, `.steam`, `.tw`, `.irpg` |
| Password material | **Only** the first 5 hex characters of a SHA-1 digest | `.pwn` |

Four things deliberately do **not** leave:

- The `.fx` conversion amount. Only the two currency codes go over the wire; the
  multiplication happens locally.
- The full password or hash in `.pwn`.
- The `.qr` payload. No request is made; a link is returned.
- The IRC nick and channel. No integration sends them. `linktitle` uses the
  channel name as a local cooldown key only, and `steam` persists the
  nick-to-SteamID mapping to a local 0600 file.

Every user's identity is nonetheless attached to their request at the *bot's*
end: a shared source IP and one shared User-Agent, per-nick rate limiting via
`bot.rate_limited(nick)`, and audit-log entries for admin actions.
`.forgetme` erases what modules persist locally (`BotModule.forget`); it cannot
recall anything already sent upstream.

## Failure and degradation

Three distinct models, worth telling apart when diagnosing an outage.

**Weather providers fail over.** A failure moves to the next provider in the
chain and updates the health score. Repeated failure opens the circuit breaker
and removes the provider from consideration for 60 seconds. An auth failure
(401/403) opens it immediately. The user sees a result from a lower-ranked
provider, or a not-available message only when the whole chain is exhausted or
the 45 s budget expires.

**Multi-source modules degrade by segment.** `.ip` / `.rep`, `.caa`, `.solar`,
and `.iss` fetch several independent things and wrap each in its own exception
handler, so one dead source omits its own segment rather than failing the
command.

**Single-source modules fail closed with a static string.** Every remaining
networked command catches once and replies with a fixed message: "lookup failed",
"<service> unavailable", or a parse-error variant. No module in the bot retries
a failed HTTP call. `bot.rate_limited(nick)` is a per-nick command throttle for
channel abuse, not an upstream quota mechanism, and nothing implements backoff
against a 429 other than the weather dispatcher's health decay.

Missing-credential behavior splits three ways:

- **Refuse with a hint.** `is_configured()` returns `False`, `.help` hides the
  module, and invoking it anyway replies with the config location:
  `.imdb`, `.yt`, `.lastfm`, `.tw`, `.steam`, `.passes`, `.si`.
- **Degrade.** `.sw` falls back from Brave to DuckDuckGo; `.ip` omits the
  AbuseIPDB segment; `linktitle` falls back from the YouTube Data API to the
  free oembed endpoint; `.apod` and `.neo` fall back to NASA's shared
  `DEMO_KEY` and its stricter quota.
- **Disappear from the chain.** Every keyed weather provider: the factory
  returns `None` and logs `skipped (no <key>)`.

`.stock` and `.crypto` sit across two of these: no keys at all produces a clean
refusal, but one or more configured keys plus a failure produces the leak
documented above.

## Related documentation

- [configuration.md](configuration.md) - every config.ini key, including the
  provider priority order.
- [security-model.md](security-model.md) - the SSRF guard, secret handling, and
  the trust boundaries these integrations cross.
- [providers.md](providers.md) - the weather aggregation design in full.
- [modules.md](modules.md) - the command inventory these services back.
- [testing.md](testing.md) - how the integrations are stubbed in the suite, and
  which of them have no behavioral test.
- [internals/secret_store.md](internals/secret_store.md) - the two-tier store,
  the migrate command, and the permission checks.
