# Changelog

All notable changes to the Internets IRC bot are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [2.5.0] — 2026-05-19

Per-provider weather flags, scientific-accuracy provider ranking, and a
keyring-backed secret store with template-only `config.ini`.

### Added

- **Per-provider weather flags.** Every weather command (`.w`, `.f`,
  `.h`, `.al`, `.air`, `.sun`, `.hist`, `.sea`, `.nc`) accepts a
  per-provider flag anywhere in the line to force a specific source:
  `-nws`, `-mm`/`-meteomatics`,
  `-aw`/`-wk`/`-apple`/`-appleweather`/`-weatherkit`,
  `-om`/`-openmeteo`, `-vc`/`-visualcrossing`, `-acc`/`-accuweather`,
  `-owm`/`-openweathermap`, `-wb`/`-weatherbit`, `-wapi`/`-weatherapi`,
  `-pw`/`-pirate`/`-pirateweather`, `-sg`/`-stormglass`,
  `-tio`/`-tomorrow`/`-tomorrowio`, `-wwo`/`-worldweatheronline`,
  `-ws`/`-weatherstack`. Examples: `.w 67127 -aw`,
  `.w -vc Tokyo`, `.f -nws -n bob`, `.marine -sg`. A `-l` flag lists
  active providers ranked by accuracy for that capability with auth
  state badges (`[OK]` / `[?]` / `[X]`). Forcing a provider that
  isn't active or doesn't support the requested capability fails
  loud instead of silently falling back.
- **`weather_providers/stormglass/`** and **`weather_providers/weatherbit/`**
  wired into the dispatcher (their packages existed but weren't
  registered in 2.4.0). Stormglass leads the marine chain;
  WeatherBit slots into the mid-tier for current/forecast/AQ.
- **`secret_store.py`** — tiered secret store with env → OS keyring →
  0600 gitignored `secrets.ini` lookup. CLI:
  `python -m secret_store {status,list,get,set,delete,migrate,init}`.
  `get` is non-revealing by default — it prints
  `(set, N chars, backend=<env|keyring|file>)` and requires
  `--reveal` to print the actual value. `init` copies
  `secrets.ini.example` → `secrets.ini` with 0600 perms. `migrate`
  pulls plaintext from `config.ini`, stores each value via the most
  secure available backend, scrubs the source, and prints a
  ROTATE-NOW checklist.
- **`secrets.ini.example`** — committed template enumerating every
  supported secret with signup URLs and free-tier limits.
- **`config.local.ini`** overlay — gitignored personal settings
  (server hostname, modes, admin password hash, default location)
  layered on top of the committed `config.ini` template.
- **`BotModule.is_configured()` hook** — modules return False when a
  required credential is missing. `.help` skips them so users only
  see commands they can actually run. Admins still see the hidden
  list via `.help`.
- **`.modules` shows per-module command counts** — output format:
  `Loaded (N): bofh (2), calc (1), …` followed by `Available: …` for
  unloaded modules on disk.
- **`modules/base.cred()`** helper — every module's `on_load` pulls
  its API key + User-Agent through this, so the secret store wins
  with `config.ini` as the legacy fallback.
- **`[project.optional-dependencies] keyring`** in `pyproject.toml`
  for the OS-native encrypted-at-rest backend.

### Changed

- **Provider ranking is now driven by the scientific accuracy of the
  underlying numerical models** before live health scores or the
  user-configured order. New default chain leads with NWS (NDFD +
  HRRR + WaveWatch III), Meteomatics (ECMWF/ICON/GFS), Apple
  WeatherKit (NWS + IBM TWC), Open-Meteo (ECMWF/ICON/GFS multi-model
  + CAMS + ERA5), Visual Crossing (ERA5). Per-capability ranks live
  in `weather_providers/_dispatch.DEFAULT_RELIABILITY` with a
  documented rationale.
- **`config.ini` is now a credential-free committed template.** Real
  values must live in the secret store or in `config.local.ini`. The
  0600 perm check on `secrets.ini` fails closed (`get()` returns
  empty if perms loosen).
- **Outbound credentials are encrypted at rest, not hashed.** Earlier
  internal discussion flagged hashing of NickServ/SASL/server/oper
  passwords + API keys; this isn't possible because the bot has to
  send the literal value on the wire. Encryption-at-rest via OS
  keyring (or 0600 file) is what 2.5.0 implements instead.
- README, `pyproject.toml`, and `__version__` bumped to 2.5.0.
  Provider count updated from 8 → 14 in README + docstrings.

### Security

- **No plaintext credentials ever entered git history.** `config.ini`
  is now a template; real values live in `secrets.ini` (gitignored,
  0600) or the OS keyring. The migrate path is provided for any
  local instance that previously pasted real values into a working
  copy.

### Fixed

- Stormglass and WeatherBit provider packages were dormant — the
  audit caught this and 2.5.0 wires them in.
- `weather_providers/__init__.py` docstring claimed 8 providers when
  12 were registered; now accurately documents all 14.

### Removed

- **28 stray `.git`-internal files** that had been accidentally
  tracked by an earlier commit (loose objects, pack indices, hook
  scripts) — removed from `git ls-files`. No git-history rewrite
  needed since these were never live VCS state for downstream
  consumers.

## [2.4.0] — 2026-04-10

Full Rizon Internets command parity.  Six additional modules: Steam, Twitch,
IdleRPG, QDB, FML, and web/image search.

### Added

- **`modules/steam.py`** — `.steam [user/-g/-n nick]` status and game info,
  `.regsteam <id/vanity>` nick-to-SteamID registration.  Steam Web API,
  key required.  Persists IDs to `steamids.json`.
- **`modules/twitch.py`** — `.tw` / `.twitch` with subcommands: `-s` search
  streams (default: top live), `-c <channel>` channel info, `-g <game>`
  game search.  Twitch Helix API with automatic OAuth token management,
  client_id + client_secret required.
- **`modules/idlerpg.py`** — `.irpg` / `.idlerpg <player>` IdleRPG player
  lookup.  Configurable endpoint (default: Rizon's idlerpg.rizon.net).
- **`modules/qdb.py`** — `.qdb [id]` random or specific quote from a
  configurable QDB-compatible XML endpoint.  Disabled by default (qdb.us
  is defunct).
- **`modules/fml.py`** — `.fml` random FMyLife quote via web scraping.
- **`modules/bofh.py`** — `.bofh` / `.excuse` random Bastard Operator
  From Hell excuse.  Built-in list of 105 excuses, no API or key needed.
- **`modules/search.py`** — `.sw` / `.g` web search, `.si` / `.gi` image
  search.  Primary: DuckDuckGo HTML lite (no key).  Optional upgrade:
  Brave Search API (key, 2,000 queries/month free).  Image search
  requires Brave API key.
- **`[steam]`**, **`[twitch]`**, **`[idlerpg]`**, **`[qdb]`**, **`[search]`**
  config sections.

## [2.3.0] — 2026-04-10

Six new command modules ported from Rizon's Internets: movie lookup, Last.fm,
YouTube search, dictionary definitions, IP geolocation, and URL shortening.

### Added

- **`modules/imdb.py`** — `.imdb <title>` movie/TV lookup via OMDb API
  (omdbapi.com, free 1,000 calls/day, key required).
- **`modules/lastfm.py`** — `.lastfm <user>` Last.fm profile with play
  count, registration date, and now-playing / latest track.  Free API,
  key required.
- **`modules/youtube.py`** — `.yt <search>` / `.youtube <search>` YouTube
  video search with title, duration, view count, and likes.  YouTube Data
  API v3, key required.
- **`modules/dictionary.py`** — `.dict <word> [/N]` / `.dictionary <word>`
  English dictionary definitions via Free Dictionary API (dictionaryapi.dev,
  no key required).  Supports pagination with `/N` suffix.
- **`modules/ipinfo.py`** — `.ipinfo <ip/host>` IP and hostname geolocation
  via ip-api.com (free, no key, 45 requests/min).  Shows city, region,
  country, timezone, ISP, and Google Maps link.
- **`modules/urls.py`** — `.shorten <url>` URL shortener via is.gd (free,
  no key) and `.expand <url>` / `.unshorten <url>` URL expander via
  redirect following.
- **`[imdb]`**, **`[lastfm]`**, **`[youtube]`** config sections for API keys.

## [2.2.0] — 2026-04-10

Stock and cryptocurrency price lookup module with multi-provider failover.
License changed to ISC.  CI lint fix for provider package structure.

### Added

- **`modules/stocks.py`** — Stock and crypto price lookup with three
  free-tier providers (Finnhub, Alpha Vantage, Twelve Data).  Automatic
  failover: first provider with a valid key that returns data wins.
  - `.stock <symbol>` / `.s <symbol>` — stock quote (price, change,
    open/high/low, volume)
  - `.crypto <symbol>` — cryptocurrency price in USD
- **`[stocks]` config section** — API key fields for `finnhub_key`,
  `alphavantage_key`, `twelvedata_key`.
- **`tests/test_stocks.py`** — unit tests for formatting helpers.

### Changed

- **License changed from MIT to ISC** — `LICENSE`, `pyproject.toml`
  (license field + classifier), `README.md`.
- **`stocks` added to default `autoload`** in `config.ini`.

### Fixed

- **CI lint step** (`.github/workflows/tests.yml`) — replaced hardcoded
  flat-file paths (`weather_providers/openmeteo.py`, etc.) with
  `find weather_providers -name '*.py'` to match the package structure
  introduced in 2.0.0.

## [2.1.0] — 2026-03-21

Version bump (undocumented).  No functional changes from 2.0.0.

## [2.0.0] — 2026-03-21

Weather aggregation platform.  Complete architectural redesign of the
provider system from flat files to sub-module packages with a capability-based
dispatcher and provider health scoring.  8 providers, 38 API endpoints,
10 IRC commands.

### Breaking Changes

- **Provider packages replaced flat files.**  `weather_providers/openmeteo.py`
  is now `weather_providers/openmeteo/` with `current.py`, `forecast.py`,
  `hourly.py`, etc.  Direct imports from the old flat modules will break.
  The public API (`weather_providers.get_weather()`, etc.) is unchanged.

- **`get_providers()` returns `list[str]`** (provider IDs) instead of
  provider objects.  Use `dispatcher.get_provider(pid)` for objects.

- **Config key `priority` renamed to `provider_priority`.**  The old
  `priority` key is still read as a fallback for backwards compatibility.

### Added

- **Capability-based dispatcher** (`_dispatch.py`) — auto-discovers
  provider capabilities via `hasattr()`, scores providers by health,
  routes requests to the best available provider for each data type.

- **Provider health tracking** (`_health.py`) — EMA-based scoring of
  success rate, response latency, and rate-limit errors.  Composite
  `health_score` (0.0–1.0) drives provider selection.  `.providers`
  command shows live health status.

- **4 new weather providers:**
  - **OpenWeatherMap** — current, forecast, hourly, alerts, air quality
  - **Weatherstack** — current, forecast, historical
  - **Meteomatics** — current, forecast, hourly (Basic Auth)
  - **AccuWeather** — current, forecast, hourly, alerts (location key lookup)

- **Sub-module package architecture** — each provider is a directory
  with one Python file per API endpoint.  51 endpoint files across
  8 provider packages.

- **`.nowcast` / `.nc`** command — precipitation nowcast (next 1-2 hours).

- **`.providers`** command — admin-only, shows provider health scores
  and capability chain status.

- **`NowcastResult` / `NowcastEntry`** dataclasses for precipitation
  nowcasting data.

- **Static reliability rankings** per capability in `DEFAULT_RELIABILITY`
  dict, used as tie-breaker when health scores are similar.

### Provider Capability Matrix

| Provider | current | forecast | hourly | alerts | AQ | astro | historical | marine |
|----------|:-------:|:--------:|:------:|:------:|:--:|:-----:|:----------:|:------:|
| Open-Meteo | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| WeatherAPI | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Tomorrow.io | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | — |
| OpenWeatherMap | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | — |
| WeatherKit | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| AccuWeather | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| Meteomatics | ✓ | ✓ | ✓ | — | — | — | — | — |
| Weatherstack | ✓ | ✓ | — | — | — | — | ✓ | — |

## [1.6.0] — 2026-03-21

Full API coverage for all weather providers.  Every supported endpoint from
Open-Meteo, WeatherAPI.com, Tomorrow.io, and Apple WeatherKit is now exposed
through a unified fallback chain.  6 new IRC commands, 6 new data types.

### Added

- **Hourly forecast** (`.hourly` / `.h`) — next 12 hours with temperature,
  conditions, precipitation chance, and wind.  Fallback chain:
  WeatherAPI → Tomorrow.io → Open-Meteo → WeatherKit.

- **Weather alerts** (`.alerts` / `.al`) — active warnings and advisories
  with severity, event type, and headline.  Fallback chain:
  WeatherAPI → Tomorrow.io → WeatherKit.

- **Air quality** (`.aqi` / `.air`) — US EPA AQI index, PM2.5, PM10, ozone,
  NO₂, SO₂, CO concentrations.  Fallback chain:
  Open-Meteo → Tomorrow.io → WeatherAPI.

- **Astronomy** (`.astro` / `.sun`) — sunrise, sunset, day length, moonrise,
  moonset, moon phase, illumination percentage.  Fallback chain:
  WeatherAPI → Open-Meteo.

- **Historical weather** (`.history` / `.hist [YYYY-MM-DD] [location]`) —
  conditions for a specific past date (high/low/avg temp, precip, wind,
  humidity).  Fallback chain: Open-Meteo → WeatherAPI.

- **Marine weather** (`.marine` / `.sea`) — wave height, wave period, wave
  direction, swell data, wind waves, water temperature.  Fallback chain:
  Open-Meteo → WeatherAPI.

- **Capability-based provider dispatch** — each data type has its own
  reliability-ordered fallback chain.  Providers that don't support a given
  data type are automatically skipped.  The `_CAPABILITY_ORDER` dict in
  `weather_providers/__init__.py` controls the ordering.

- **New data types** in `weather_providers/base.py`:
  `HourlyResult`, `HourlyEntry`, `AlertsResult`, `AlertEntry`,
  `AirQualityResult`, `AstronomyResult`, `HistoricalResult`, `MarineResult`.

- **`aqi_category()` helper** — converts US EPA AQI 0–500 to human-readable
  category (Good, Moderate, Unhealthy, etc.).

- **New formatting helpers** in `modules/units.py`:
  `aqi_fmt()`, `wave_fmt()`, `swell_fmt()`.

### Provider API Coverage

| Feature | Open-Meteo | WeatherAPI | Tomorrow.io | WeatherKit |
|---------|:----------:|:----------:|:-----------:|:----------:|
| Current | ✓ | ✓ | ✓ | ✓ |
| Daily forecast | ✓ | ✓ | ✓ | ✓ |
| Hourly forecast | ✓ | ✓ | ✓ | ✓ |
| Alerts | — | ✓ | ✓ | ✓ |
| Air quality | ✓ | ✓ | ✓ | — |
| Astronomy | ✓ (partial) | ✓ (full) | — | — |
| Historical | ✓ | ✓ | — | — |
| Marine | ✓ | — | — | — |

## [1.5.0] — 2026-03-21

Security hardening, concurrency safety, and code quality improvements.
Addresses all findings from the eighth audit pass (code analysis review).

### Security

- **Hostmask-verified admin sessions.**  Admin sessions now track the
  `nick!user@host` hostmask recorded at authentication time.  If a nick's
  hostmask changes after auth (e.g. someone else takes the nick after a
  netsplit), the session is automatically invalidated on the next
  `is_admin()` check.  Previously, sessions were tracked by nickname only.

- **SSL certificate verification enabled by default.**  `ssl_verify` in
  `config.ini` now defaults to `true`.  Users connecting to servers with
  self-signed certificates must explicitly set `ssl_verify = false`.
  Previously defaulted to `false`, which allowed MITM attacks.

### Fixed

- **`threading.Lock` for auth state.**  `_authed` and `_auth_fails` are now
  protected by a dedicated `_auth_lock` (`threading.Lock`).  Previously
  relied on CPython's GIL for atomicity, which is not guaranteed under
  GIL-free Python (PEP 703 / Python 3.13t+).

- **`Store.prune_users()` public method.**  Added a public thread-safe
  `prune_users()` wrapper around the private `_prune_users()`.  The test
  suite (`test_store.py`) called the public name which did not exist,
  causing `AttributeError` at test time.  `_prune_users()` now returns an
  `int` (number of entries removed) for testability.

- **Named constant for auth cleanup threshold.**  The hard-coded `50` in
  `cmd_auth()` fail-dict cleanup is now `_AUTH_CLEANUP_THRESHOLD` class
  constant for clarity and maintainability.

### Changed

- **`modules/__init__.py`** now has a docstring and `__all__` export list.

- **`pyproject.toml`** — Added `weatherkit` optional dependency extra
  (`PyJWT` + `cryptography`).  Added weatherkit deps to `all` extra.

### Documentation

- **README.md** — Updated version references in all example output blocks
  (previously showed `1.3.0`).  Expanded Security section to describe
  hostmask-verified sessions and the `ssl_verify` default change.  Updated
  Testing section to cover both standalone and pytest suites.  Added Apple
  WeatherKit to the intro, architecture diagram, design decisions,
  requirements, and configuration sections.  Added full "Configuring Apple
  WeatherKit" setup guide.  Documented `default_location`, `services_nick`,
  and `user_max_age_days` config keys.

- **CHANGELOG.md** — Added this entry.  Retroactively documented WeatherKit
  provider and `weatherkit` optional dependency in the 1.4.0 entry.

- **CONTRIBUTING.md** — Updated test instructions and module development
  guidance.  Added `threading.Lock` requirement to code style.

- **config.ini** — Added all four WeatherKit config keys (`weatherkit_team_id`,
  `weatherkit_service_id`, `weatherkit_key_id`, `weatherkit_key_file`) with
  setup instructions in comments.  Changed `ssl_verify` default to `true`.

## [1.4.0] — 2026-03-04

Multi-provider weather refactor.  Replaces the NWS + Open-Meteo dual-source
system with a pluggable provider architecture supporting automatic fallback.
Security-hardened per the Final Security Hardening Directive (eighth audit pass,
10 findings, all resolved).  154 automated tests.

### Added

- **`weather_providers/` package** — standalone multi-provider weather system
  with `WeatherResult` / `ForecastDay` normalized dataclasses, a
  `WeatherProvider` protocol, and ordered fallback registry.

- **Open-Meteo provider** (`openmeteo.py`) — free, no API key required.

- **WeatherAPI.com provider** (`weatherapi.py`) — requires API key, free tier
  available (1M calls/month).

- **Tomorrow.io provider** (`tomorrowio.py`) — requires API key, free tier
  available (500 calls/day).

- **Apple WeatherKit provider** (`weatherkit.py`) — requires Apple Developer
  Program membership.  Uses JWT/ES256 authentication with a `.p8` private
  key.  Optional: `pip install internets-irc[weatherkit]`.  Not included in
  default provider priority — must be explicitly enabled in config.

- **Async HTTP helper** (`_http.py`) — uses `aiohttp` when available for true
  non-blocking I/O, falls back to `requests` + `asyncio.to_thread()`.

- **`[weather_providers]` config section** — configurable provider priority
  order and API keys.  Open-Meteo always available as last-resort fallback.

- **Provider source attribution** — weather output now includes `[Open-Meteo]`,
  `[WeatherAPI]`, `[Tomorrow.io]`, or `[Apple Weather]` tag showing which
  provider returned data.

- **`aiohttp` optional dependency** — `pip install internets-irc[async]` for
  true async HTTP.

- **`weatherkit` optional dependency** — `pip install internets-irc[weatherkit]`
  for Apple WeatherKit support (PyJWT + cryptography).

### Security

- **SEC-WP-001: Response size cap** — All HTTP responses capped at 1 MB to
  prevent OOM from malicious or misconfigured API endpoints.

- **SEC-WP-002: API key redaction** — Exception logging uses `type(e).__name__`
  instead of the full message, which could contain URL query parameters with
  API keys.

- **SEC-WP-003: Atomic provider swap** — `configure()` builds into a local
  list then atomically assigns.  `get_weather()`/`get_forecast()` snapshot the
  list before iterating to prevent TOCTOU races during module reloads.

- **SEC-WP-004: IRC control char sanitization** — All API-sourced strings
  (description, source, wind direction, day names) are stripped of C0 control
  characters and DEL before reaching IRC output.

- **SEC-WP-005: Type guard survives `-O`** — Replaced `assert isinstance()`
  with explicit `if not isinstance(): raise TypeError()`.

- **SEC-WP-006: Forecast days clamped** — Hard cap at 16 days prevents abuse
  of paid API tiers.

- **SEC-WP-010: Defensive response parsing** — `data.get("current")` with
  `isinstance()` validation instead of bare `data["current"]` KeyError.

### Changed

- **Weather commands simplified** — `.weather`/`.w` and `.forecast`/`.f` now
  query the provider chain instead of hard-coded NWS/Open-Meteo routing.

### Removed

- **NWS module** (`modules/nws.py`) — replaced by the weather_providers system.

- **NWS-only commands** — `.hourly`/`.fh`, `.alerts`/`.wx`, `.discuss`/`.disc`
  removed.  These depended on NWS-specific API features.

- **`_merge_current` / `_om_current` / `_om_forecast`** — legacy helper
  functions replaced by the provider abstraction.

### Fixed

- **PLATFORM: Windows cp1252 test crash** — Test runner used Unicode markers
  (`✓` / `✗`) that can't encode in Windows cp1252 console encoding. All Python
  versions on Windows CI (3.10–3.13) failed identically. Replaced with ASCII
  `[PASS]` / `[FAIL]` markers and added `sys.stdout.reconfigure(errors="replace")`
  fallback. Added `PYTHONIOENCODING=utf-8` to GitHub Actions workflow as a
  belt-and-suspenders defense.

- **CI: Lint step now covers all source files** — Added `weather_providers/`
  and `modules/` glob to syntax check in GitHub Actions.

### Testing

- 154 automated tests (up from 142).  Added: `WeatherResult` dataclass tests,
  provider protocol compliance, registry configuration (priority ordering, key
  filtering, unknown provider handling), format function tests, async coroutine
  verification, and security hardening tests (SEC-WP-001 through SEC-WP-010).

## [1.3.0] — 2026-03-03

Security hardening release. Full zero-trust line-by-line audit per the Final
Security Hardening Directive.  84 total findings across seven audit passes,
all resolved.  119 automated tests.  Cross-platform validation for UNIX, BSD,
Linux, macOS, Windows, WSL/WSL2, Cygwin, MinGW, and MSYS2.

### Added

- **Semantic versioning** — `__version__` constant in `internets.py` as the
  single authoritative source of truth.  Displayed via `--version` CLI flag,
  `.version` IRC command, `.help` output, startup log, and console `status`.

- **`.version` command** — reports bot version and repository URL.

### Security

- **TLS 1.2 minimum enforced** — `ssl_ctx.minimum_version = TLSv1_2` blocks
  deprecated TLS 1.0/1.1 connections.  See AUDIT.md SEC-009.

- **Log injection prevented** — `_SafeFormatter` sanitizes both `record.msg`
  and `record.args` (tuple and dict forms) to strip CR/LF/NUL.  Works on a
  record copy to avoid mutating shared state.  See AUDIT.md SEC-007, BUG-032.

- **Error info disclosure eliminated** — `_run_cmd`, `load_module`,
  `unload_module`, `cmd_rehash`, and `cmd_auth` all send generic "see log for
  details" messages to IRC instead of raw Python exception text.
  See AUDIT.md SEC-008, SEC-013, SEC-014.

- **Config path resolved at startup** — `config.ini` is resolved to an
  absolute path (`_CONFIG_PATH`) once, preventing CWD-change attacks from
  redirecting to a malicious config.  See AUDIT.md SEC-017.

- **Nick collision uses cryptographic RNG** — Replaced `random.randint` with
  `secrets.randbelow` in the 433 nick-in-use handler.  See AUDIT.md SEC-018.

- **NWS SSRF prevention** — All URLs derived from NWS API grid responses are
  validated against `https://api.weather.gov/` before fetching.
  See AUDIT.md SEC-021.

- **PRIVMSG/NOTICE target validation** — Rejects empty or space-containing
  targets to prevent protocol parameter injection.  See AUDIT.md BUG-027.

- **Symlink traversal blocked (cross-platform)** — Module loader uses
  `Path.relative_to()` instead of string comparison.  See AUDIT.md BUG-028,
  BUG-035.

- **IRC 512-byte line limit enforced** — Sender truncates outgoing lines with
  UTF-8-safe boundary detection.  See AUDIT.md BUG-026.

- **Concurrent task cap** — Active command tasks capped at 50 to prevent
  resource exhaustion.  See AUDIT.md BUG-030.

- **Command argument length cap** — Arguments exceeding 400 chars rejected
  before reaching handlers.  See AUDIT.md BUG-031.

- **Sender queue bounded** — `PriorityQueue(maxsize=200)` prevents OOM during
  prolonged disconnects.  See AUDIT.md BUG-056.

- **INVITE rate limiting** — 5-second cooldown between accepting INVITEs to
  prevent flood abuse.  See AUDIT.md BUG-038.

- **Channel name validation** — All channel names (from saved state, INVITEs,
  and user commands) validated against `_CHAN_RE` before use in JOIN.
  See AUDIT.md BUG-047, BUG-049.

- **PING payload capped** — Reflected PONG payload limited to 400 bytes.
  See AUDIT.md BUG-050.

- **Stream reader buffer limit** — `asyncio.open_connection(limit=8192)`
  prevents oversized line attacks.  See AUDIT.md BUG-042, BUG-033.

- **Config permission warning (POSIX only)** — Warns on startup if
  `config.ini` is world-readable.  Guarded for POSIX; no-op on Windows.
  See AUDIT.md BUG-029, PLATFORM-001.

### Fixed

- **Store type validation on load** — `_read` validates that loaded JSON
  matches the expected container type, falling back to defaults on mismatch.
  See AUDIT.md BUG-051.

- **Store file size limit** — Data files exceeding 10MB are rejected at load
  time to prevent OOM.

- **Store I/O uses explicit UTF-8** — `read_text(encoding="utf-8")` and
  `os.fdopen(fd, "w", encoding="utf-8")` prevent platform-dependent encoding.
  See AUDIT.md PLATFORM-003.

- **Store temp file cleanup is exception-safe** — `os.unlink(tmp)` wrapped in
  `try/except OSError` for Windows compatibility.  See AUDIT.md PLATFORM-002.

- **calc.py `math.cbrt` fallback** — Uses `getattr` fallback for Python < 3.11
  compatibility.  See AUDIT.md BUG-052.

- **calc.py NUL sentinel replaced** — Implicit multiplication placeholder
  changed from `\x00` to `\ufdd0` (Unicode noncharacter).
  See AUDIT.md BUG-055.

### Testing

- **119 automated tests** covering protocol parsing, store operations, rate
  limiting, calculator safety, sender behavior, authentication, async
  architecture, and all security hardening fixes from passes six and seven.

## [1.2.0] — 2026-03-04

Full async conversion and quality pass.  The entire bot now runs on a single
asyncio event loop — no more spawning threads for every command.  All module
handlers are coroutines.  Blocking I/O (HTTP, disk, password hashing) runs
via `asyncio.to_thread()` inside the handler, keeping the event loop free.

### Architecture

- **asyncio event loop** replaces all daemon threads for the connection
  lifecycle, command dispatch, keepalive, send queue, console, and deferred
  channel rejoin.

- **Sender** is now an async drain loop over `asyncio.PriorityQueue` +
  `StreamWriter.drain()`.  Token-bucket rate limiting uses `asyncio.sleep`.
  Thread-safe `enqueue()` uses `loop.call_soon_threadsafe()` so module
  handlers can call `bot.send()` / `bot.privmsg()` from any context.

- **Command dispatch** creates `asyncio.Task` per command.  Handlers are
  awaited directly — no `asyncio.to_thread()` wrapper.  Only the actual
  blocking operations (HTTP, password hashing) use the thread pool.

- **Console** uses `asyncio.to_thread(input)` for non-blocking stdin reads,
  with the console task running alongside the main bot task via
  `asyncio.wait(return_when=FIRST_COMPLETED)`.

- **Signal handling** uses `loop.add_signal_handler()` instead of the
  `signal` module, properly integrating with the event loop.

### Added

- **SASL PLAIN authentication** — When the server advertises SASL support and a
  NickServ password is configured, the bot authenticates during capability
  negotiation (before registration completes). This eliminates the timing race
  between NickServ IDENTIFY and `+R` channel joins. Falls back to traditional
  NickServ IDENTIFY if SASL fails. `AUTHENTICATE` payloads are redacted in logs.

- **Exponential reconnect backoff** — Reconnect delays now follow exponential
  backoff: 15s, 30s, 60s, 120s, 240s, capped at 5 minutes. Resets on successful
  connection.

- **Thread-safe `ChannelSet`** — `active_channels` is a proper thread-safe
  container (still uses `threading.Lock` because `enqueue()` may be called from
  thread pool executors).

- **User pruning** — User tracking entries older than 90 days (configurable via
  `user_max_age_days` in `config.ini`) are automatically pruned during store
  flushes.

- **Standalone test suite** — 79 tests in `tests/run_tests.py` covering protocol
  parsing, store, calculator, dice, weather merging/formatting, units, sender
  injection prevention, password hashing, ChannelSet, backoff, async sender
  (drain, priority bypass, thread-safe enqueue), and async handler verification
  (all module and core handlers confirmed as coroutines).

- **`protocol.py` extraction** — Pure protocol helpers (ISUPPORT parsing, MODE
  parsing, NAMES parsing, SASL payload encoding, tag stripping) in a separate
  module with no bot state or I/O.

### Changed

- **All command handlers are now coroutines** — Every module handler and every
  core command (auth, help, load, shutdown, etc.) is `async def`.  HTTP calls
  use `await asyncio.to_thread(requests.get, ...)` inside the handler.
  Password verification uses `await asyncio.to_thread(verify_password, ...)`.
  Pure computation (calc, dice, help text) runs directly in the event loop.

- **Channels module cleanup is an asyncio task** — The verification timeout
  garbage collector is now `asyncio.create_task(_cleanup_loop())` instead of a
  `threading.Thread`.  Created during `on_load()`, cancelled on `on_unload()`.

- **Type annotations everywhere** — All files use `from __future__ import
  annotations` with PEP 604 union syntax.  Every public function, method, and
  class attribute is annotated.

- **README updated** — Architecture section reflects async design, protocol.py,
  tests.  Module example uses async handlers.  SASL, backoff, pruning, testing
  documented.

### Fixed

- **Admin auth case-insensitive** — `_authed` now normalizes nicks to lowercase,
  matching IRC's case-insensitive nick semantics per RFC 2812. Previously, a
  case mismatch between auth and subsequent commands could silently drop admin
  status.

- **Hostmask capture now includes `user@` portion** — JOIN, NICK, and PRIVMSG
  regexes captured only the hostname after `@`, losing the ident/username. The
  `.users` display showed `nick!hostname` instead of `nick!user@hostname`, and
  `users.json` entries were inconsistent with the CHGHOST handler (which
  correctly stored `user@host`). All three regexes now capture the full
  `user@host` string.

- **Premature `active_channels.add` in `_on_invite` and `_deferred_rejoin`** —
  Both methods added channels to the active set and saved to disk before the
  server confirmed the JOIN. If the server rejected the JOIN, phantom entries
  persisted. Removed the premature adds; `_on_join` (triggered by the server's
  JOIN echo) now handles both add and save.

- **Missing JOIN error handlers for 403/405/476** — ERR_NOSUCHCHANNEL (403),
  ERR_TOOMANYCHANNELS (405), and ERR_BADCHANMASK (476) were unhandled, leaving
  phantom channels in `active_channels` and `channels.json`. Now handled
  alongside the existing 471/474/475 handlers.

- **Task done_callback safe after `_tasks.clear()`** — During reconnect, all
  tasks are cancelled and the list cleared. When cancelled tasks subsequently
  completed, their done callback called `list.remove()` on the empty list,
  raising `ValueError`. The callback now guards with an `in` check first.

- **`channels.py` uses `asyncio.get_running_loop()`** — Replaced deprecated
  `asyncio.get_event_loop()` call in `on_load()`.

- **Test suite expanded** — 6 new tests covering admin case-insensitivity,
  hostmask regex capture, JOIN error numerics, NICK regex, and done_callback
  safety. Total: 79 tests.

## [1.1.0] — 2026-03-03

Full codebase audit and hardening pass. 39 findings identified and resolved
across security, stability, architecture, and quality-of-life categories.
Includes hybrid weather data source merging, MODE/ISUPPORT parsing fixes,
and thread safety improvements found in the follow-up review.
See `AUDIT.md` for detailed forensic writeups of each finding.

### Added

- **Hybrid weather data merging** — Weather commands for US locations now query
  both NWS and Open-Meteo, merging results into a single output. NWS values
  take priority; Open-Meteo fills gaps (common for NWS stations that report
  null temperature, visibility, or humidity). Both sources return structured
  `WeatherDict` dicts instead of pre-formatted strings. A `_merge_current()`
  function combines them, and `_format_current()` produces the output. NWS heat
  index and wind chill labels are preserved through the merge.

- **Channel founder verification** — `.join` and `.part` now verify the
  requesting user is the registered channel founder via IRC services before
  acting. The bot WHOIS-es the user for their NickServ account, queries
  ChanServ/X3/etc. for the channel founder, and compares. Works across Anope,
  Atheme, Epona, X2, X3, and forks. Configurable via `services_nick` in
  `config.ini`. Bot admins bypass verification. `/INVITE` remains open.

- **`on_raw(line)` module hook** — Modules can now intercept raw IRC traffic
  (server numerics, NOTICEs, protocol messages) by overriding `on_raw()` in
  their `BotModule` subclass. The core dispatches every incoming line (after
  IRCv3 tag stripping) to all loaded modules. Used by the channels module for
  founder verification, available for any future module that needs protocol-level
  access.

- **Auth brute-force protection** — 5-minute lockout after 5 failed password
  attempts per nick. Counter resets after lockout expires or on successful auth.

- **Credential redaction in logs** — Outgoing `PASS`, `IDENTIFY`, and `OPER`
  commands are redacted in the sender's debug log. Incoming `AUTH` messages are
  redacted in the main loop. Command dispatch log redacts auth arguments.

- **Graceful shutdown** — `SIGTERM`, `SIGINT`, and the new `.shutdown` / `.die`
  admin command all trigger the same clean exit path: save channel list to disk,
  call `on_unload()` on every loaded module, send `QUIT` to the server, wait for
  the sender queue to flush, then exit. `.restart` also saves state and unloads
  modules before `execv`. Accepts an optional quit reason
  (e.g. `.shutdown maintenance window`).

- **`services_nick` config option** — New setting under `[bot]` for specifying
  the IRC services bot name. Defaults to `ChanServ`. Set to `X3`, `Q`, etc. for
  non-ChanServ networks.

- **Configurable user modes, oper modes, and snomask** — Three new `[irc]`
  config options: `user_modes` (applied after MOTD, e.g. `+ix`), `oper_modes`
  (applied after successful OPER, e.g. `+s`), and `oper_snomask` (server notice
  mask applied after OPER, e.g. `+cCkKoO`). All validated at startup. Also added
  `.mode` and `.snomask` admin commands for runtime changes without restart.

- **Runtime log control** — Two new admin commands: `.loglevel` and `.debug`.
  `.loglevel` with no args shows current state; `.loglevel WARNING` changes
  the base output level; `.loglevel internets.weather DEBUG` enables debug for
  a single subsystem.  `.debug on/off` toggles global debug.  `.debug weather`
  enables debug output for just the weather subsystem without flooding everything
  else — only that module's debug records appear in the main log and console.
  `.debug weather off` disables it.  Multiple subsystems can be debugged
  simultaneously.  `.rehash` resets all debug state to config defaults.

- **Log rotation** — Main log file and optional debug file are now rotated via
  `RotatingFileHandler`. New config options: `max_bytes` (default 5 MB),
  `backup_count` (default 3 rotated copies).

- **Dedicated debug file** — Optional `debug_file` setting in `[logging]`.
  When set, captures ALL log output at DEBUG level regardless of the main log
  level. Useful for post-mortem analysis of protocol issues without enabling
  verbose output in the main log.

- **Hierarchical logger names** — All modules use `internets.<name>` logger
  names (e.g. `internets.weather`, `internets.store`, `internets.sender`).
  Log format now includes the logger name, making it easy to grep for a
  specific subsystem's output.

- **CLI debug flags** — `--debug` enables global debug at startup.
  `--debug weather store` enables per-subsystem debug.  `--loglevel WARNING`
  overrides the config file level.  `--debug-file debug.log` enables a
  dedicated debug trace file.  `--no-console` disables the interactive
  stdin console.

- **Interactive console** — When running interactively (stdin is a TTY),
  the bot provides a `>` prompt accepting `debug`, `loglevel`, `status`,
  and `shutdown` commands without IRC auth.  `status` shows current nick,
  channels, modules, admin sessions, and log levels.  Auto-disabled when
  stdin is not a TTY (e.g. systemd, screen -dm).

- **Chanop tracking** — The core now parses `353` (NAMES) replies and `MODE`
  changes to track which users hold `~` (owner), `&` (admin), and `@` (op)
  status in each channel. Exposed via `bot.is_chanop(channel, nick)`. Maintained
  in real time across PART, QUIT, KICK, and NICK events.

- **Rate limiter cleanup** — Stale entries in the flood and API rate limiter
  dicts are now purged every 5 minutes, preventing unbounded memory growth on
  long-running instances.

### Changed

- **Calculator completely rewritten** — Replaced `eval()` with a recursive AST
  walker that only permits numeric literals, whitelisted math functions
  (`sin`, `cos`, `sqrt`, `factorial`, etc.), and basic arithmetic operators.
  No attribute access, no builtins, no comprehensions, no string operations.
  Exponents capped at 10,000, factorial capped at 170, nesting depth capped
  at 50.

- **Message splitting respects UTF-8 boundaries** — `_split_msg()` now backs up
  to the last valid UTF-8 character boundary instead of slicing mid-codepoint.
  CJK, emoji, and accented characters no longer garble at chunk boundaries.

- **Atomic JSON persistence** — All file writes in `store.py` now use
  write-to-temp + `os.replace()`. A crash during write cannot corrupt the data
  file.

- **Store rewritten: in-memory cache with periodic flush** — `store.py` no
  longer reads and writes JSON on every operation.  All data is loaded once at
  startup and mutated in memory.  A background thread flushes dirty datasets to
  disk every 30 seconds.  `graceful_shutdown` and `.restart` force an immediate
  flush.  Each dataset (locations, channels, users) now has its own lock, so a
  weather lookup never blocks behind a user-tracking write.  Public API is
  unchanged — zero module modifications required.

- **`_require_admin` and help header use live nick** — Auth hint messages and
  the help banner now reference `self._nick` instead of the stale `NICKNAME`
  constant, so they remain correct after a nick collision.

- **Dice output truncated for large rolls** — `.d 100d100` now shows only the
  first 10 individual rolls with a count note, instead of dumping all 100 values
  into the channel.

- **Restart flushes properly** — `cmd_restart` now sends `QUIT` *then* sleeps 2
  seconds, so the sender thread can actually flush the message before `os.execv`
  replaces the process.

- **Urban Dictionary module decoupled from weather config** — Falls back to a
  default User-Agent if the `[weather]` config section is missing.

- **Constant-time comparison uses stdlib** — `hashpw._ct_eq` now delegates to
  `hmac.compare_digest` instead of a hand-rolled Python loop.

### Fixed

- **Registration flood on connect** — `NICK`/`USER`/`CAP`/`PASS` were re-sent
  on every `recv()` iteration until MOTD arrived. Added a `registered` flag so
  they're sent exactly once per connection.

- **CAP LS parser destroyed capabilities** — The regex consumed capability names
  instead of stripping `=value` suffixes. Only the first capability survived
  negotiation. Replaced with `{cap.split("=",1)[0] for cap in params.split()}`.

- **Nick collision infinite loop** — `rstrip("_") + "_"` stripped all trailing
  underscores then added one, producing the same nick on consecutive collisions.
  Now simply appends `_`.

- **Self-detection broken after nick change** — All JOIN/PART/KICK/PM
  self-detection used the `NICKNAME` constant instead of `self._nick`. Channel
  tracking broke completely after any nick collision.

- **MOTD detection false-positives** — Substring match `"376" in line` triggered
  on PRIVMSGs, nicks, and server names containing those digits. Replaced with
  `re.match(r":\S+ (376|422) ", line)`.

- **PING handler crash** — Colon-less `PING` messages (valid per RFC 2812)
  caused `IndexError`. Handler now supports both formats.

- **Auth session hijack via nick change** — `_authed` set wasn't updated on
  NICK events. Users who changed nicks left their old nick as admin; anyone
  taking that nick inherited the session. Auth now migrates on NICK change.

- **`channels_load()` race condition** — Read without lock while `channels_save`
  wrote under lock. Concurrent access could yield partial JSON. Now locked.

- **`channel_users()` race condition** — Same class of bug as `channels_load`.
  Now locked.

- **Module dict thread safety** — `_modules` and `_commands` accessed from
  dispatch threads without synchronization during hot-reload. Added
  `_mod_lock` protecting all reads and writes.

- **Empty prefix crash** — Sending just the command prefix character (`.`)
  with nothing after it produced an empty list and `IndexError`. Guarded.

- **MODE arg desync corrupted chanop tracking** — The MODE parser hardcoded
  which modes consume parameters, ignoring the server's ISUPPORT CHANMODES
  and PREFIX values. Unknown modes (e.g. `L` type B, `H` type C) caused arg
  misalignment, shifting all subsequent parameters. A `+Loq` change would
  assign the wrong nicks to the wrong modes. Added 005 ISUPPORT parsing for
  both CHANMODES and PREFIX. MODE processing now handles all four CHANMODES
  types correctly. See AUDIT.md BUG-017.

- **Thread safety on `active_channels` iteration** — The `active_channels` set
  was modified from multiple threads without synchronization. `sorted()` on the
  set during concurrent mutation could crash. All iteration sites now use
  `set()` snapshots. See AUDIT.md BUG-018.

- **Gusts displayed when wind is zero** — `_format_current` showed gusts for
  any nonzero value when wind speed was 0 (`0 * 1.3 = 0` always passes).
  Added explicit `wind_kph > 0` guard. See AUDIT.md BUG-019.

- **Stale `fmt_dt` import in `nws.py`** — Unused import left over after the
  structured dict refactor. Removed.

### Security

- **RCE via `eval()` eliminated** — The calculator's `eval()` sandbox was
  trivially bypassable. Replaced with a safe AST walker. See AUDIT.md BUG-001.

- **Path traversal in module loader blocked** — `.load ../../evil` could execute
  arbitrary Python files outside the modules directory. Module names now validated
  against `^[a-z][a-z0-9_]*$`. See AUDIT.md SEC-002.

- **CRLF injection in IRC output blocked** — Embedded `\r\n` in outgoing
  messages could inject raw IRC protocol commands. Sender now strips all CR/LF.
  See AUDIT.md SEC-003.

- **Admin sessions cleared on reconnect** — After disconnect, authenticated
  nicks persisted but may belong to different people on the new connection.
  `_authed` is now cleared on every disconnect. See AUDIT.md SEC-005.

- **Admin password no longer logged** — Auth arguments were written to the log
  file at INFO level. Now redacted. See AUDIT.md SEC-001, SEC-004.

- **Auth brute-force lockout added** — No rate limiting existed on password
  attempts beyond the global 3-second flood gate. Now locks out after 5 failures
  for 5 minutes. See AUDIT.md SEC-006.

- **Non-atomic writes fixed** — A crash mid-write could corrupt JSON data files.
  Now uses atomic temp-file + rename. See AUDIT.md BUG-013.

- **Calculator DoS mitigated** — `factorial(99999)` could hang a thread; deeply
  nested expressions could blow the stack. Inputs and depth are now capped.
  See AUDIT.md BUG-015.

- **TLS 1.0/1.1 blocked** — SSL context now enforces `TLSv1_2` as the minimum
  version, preventing downgrade attacks to deprecated protocols.
  See AUDIT.md SEC-009.

- **Log injection prevented** — IRC content with embedded `\r\n` could forge log
  entries. A custom `_SafeFormatter` now strips all CR/LF/NUL from log messages
  before they reach any handler. See AUDIT.md SEC-007.

- **Error info disclosure fixed** — Raw Python exception details were sent back
  to IRC users in module load errors and unhandled command crashes. Now sends
  generic "see log for details" messages. See AUDIT.md SEC-008.

- **PRIVMSG/NOTICE target validation** — Empty or space-containing targets in
  `privmsg()` and `notice()` are now rejected, preventing protocol parameter
  injection within a single IRC line. See AUDIT.md BUG-027.

- **Symlink traversal in module loader blocked** — Symlinks in the modules
  directory pointing outside it could load arbitrary Python files. The loader
  now `resolve()`s paths and verifies they remain under `MODULES_DIR`.
  See AUDIT.md BUG-028.

- **IRC 512-byte line limit enforced** — Sender now truncates outgoing lines to
  510 bytes (plus `\r\n`) with UTF-8-safe boundary detection.
  See AUDIT.md BUG-026.

- **Concurrent task cap** — `_dispatch` now limits active command tasks to 50,
  preventing resource exhaustion from coordinated slow-command flooding.
  See AUDIT.md BUG-030.

- **Command argument length cap** — Arguments exceeding 400 characters are
  rejected before reaching any handler, preventing oversized input attacks.
  See AUDIT.md BUG-031.

- **Config file permission warning** — Startup now warns if `config.ini` is
  world-readable, since it contains credentials. See AUDIT.md BUG-029.

### Fixed (post-audit)

- **Channels not rejoined after reboot** — Invite-only (`+i`) channels silently
  failed to rejoin because the original invite expired on disconnect. The bot now
  handles 473 (ERR_INVITEONLYCHAN) by asking ChanServ to re-invite it. Also,
  NickServ identification now completes before rejoin attempts, so `+R` channels
  and ChanServ access lists work. Join errors 471 (full), 474 (banned), and 475
  (bad key) are logged and the channel is removed from the saved list.
