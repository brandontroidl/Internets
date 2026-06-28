# Internets v3.0.0

A modular IRC bot and weather aggregator built on Python's asyncio and RFC 2812. Handles worldwide weather, stock and crypto prices, movie lookups, Last.fm, YouTube search, dictionary definitions, IP geolocation, URL shortening, web/image search, Steam, Twitch, IdleRPG, QDB, FML, calculator, dice, translation, and Urban Dictionary lookups — plus stateful IRC-native tools (seen, tell, remind, notes), API-driven entertainment modules (PokéAPI, MTG, D&D, ISS tracker, xkcd, APOD, recipes, cocktails, HN, Reddit, …), pure-local dev utilities, and a full admin toolkit. Plugin architecture with hot-reload — modules can be loaded, unloaded, and reloaded without restarting the bot.

Weather queries are served by a capability-based dispatcher across **30 provider packages** spanning fourteen capabilities, ranked by the scientific accuracy of the underlying numerical models. The general chain leads with NWS (US gov NDFD + HRRR + WaveWatch III, no key), Meteomatics (ECMWF/ICON/GFS blend), Apple WeatherKit (NWS + IBM TWC), Open-Meteo (ECMWF/ICON/GFS multi-model + CAMS + ERA5, no key), and Visual Crossing (ERA5), then AccuWeather, OpenWeatherMap, WeatherBit, WeatherAPI, Pirate Weather, Stormglass (marine), Tomorrow.io, World Weather Online, Weatherstack, and MET Norway/Yr (no key). Specialist sources extend the command set: **air quality** (`.aqi`) — AirNow (US EPA), PurpleAir (crowdsourced PM2.5), WAQI, OpenAQ, IQAir; **astronomy** (`.astro`) — SunriseSunset.io; **UV** (`.uv`) — Open-Meteo + currentuvindex; **pollen** (`.pollen`, Europe) — Open-Meteo/CAMS; **alerts** (`.alerts`) — GDACS (global disasters) + ECCC (Canada); **historical** (`.history`) — NASA POWER; **wildfire** (`.wildfire`) — NIFC (US) + NASA FIRMS (global); **space weather/aurora** (`.space`) — NOAA SWPC; **tides** (`.tides`) — TideCheck + NOAA CO-OPS. Each provider is a sub-module package with one file per API endpoint. The dispatcher auto-discovers capabilities, applies the static accuracy rank first, then live health (success rate, latency, rate limits), and routes each request accordingly. Force any active provider with a per-command flag — e.g. `.w -aw 67127`, `.w -vc Tokyo`, `.f -nws`, `.aqi -an 67127`, `.uv -om`, `.tides -coops`.

Outbound credentials (NickServ / SASL / server / oper passwords, every API key, the User-Agent contact identifier) are read from `INTERNETS_<NAME>` environment variables or the `[secrets]` section of a gitignored 0600 `config.ini`. `config.ini.example` is the committed credential-free template; personal non-secret overrides may also go in a gitignored `config.local.ini` overlay. See `config.ini.example` for the full key list and [Security](#security) below for the lookup order.

**Platform support:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2.  
**Python:** 3.10+  
**Dependencies:** `pip install -r requirements.txt` installs the full runtime stack. Individual packages listed in [Requirements](#requirements).

## Architecture

```
internets.py          Core: asyncio event loop, IRC state machine, command dispatch
protocol.py           Pure protocol helpers (ISUPPORT parsing, MODE parsing, SASL, NAMES)
sender.py             Async outbound queue with token-bucket rate limiting
store.py              In-memory state with periodic disk flush (locations, channels, user tracking)
hashpw.py             Password hashing and verification (scrypt/bcrypt/argon2)

secret_store.py       Two-tier secret store: env var → config.ini[secrets] (0600)

weather_providers/
  base.py             Dataclasses: WeatherResult, HourlyResult, AlertsResult, AirQualityResult, etc.
  _http.py            Async HTTP helper (aiohttp with requests fallback)
  _dispatch.py        Capability-based dispatcher — accuracy rank → health → reg order
  _health.py          Provider health tracking (success rate, latency, rate limits)
  __init__.py         Provider registry, configure(), public get_*() functions

  Providers, ranked by scientific accuracy (see _dispatch.DEFAULT_RELIABILITY):
  nws/                NWS (Weather.gov) — US only, no key (NDFD + HRRR + WaveWatch III)
  meteomatics/        Meteomatics — username/password (ECMWF IFS / ICON / GFS blend)
  weatherkit/         Apple WeatherKit — Apple Developer Program (NWS + IBM TWC blend)
  openmeteo/          Open-Meteo — free, no key (ECMWF/ICON/GFS + CAMS AQ + ERA5)
  visualcrossing/     Visual Crossing — key (ECMWF + ERA5 reanalysis)
  accuweather/        AccuWeather — key (proprietary long-range)
  openweathermap/     OpenWeatherMap — key (GFS + ECMWF + CAMS AQ)
  weatherbit/         WeatherBit — key (GFS + station obs)
  weatherapi/         WeatherAPI.com — key (GFS-derived)
  pirateweather/      Pirate Weather — key (Dark Sky compat; HRRR + MRMS for US nowcast)
  stormglass/         Stormglass — key (marine specialist, 7-model wave blend)
  tomorrowio/         Tomorrow.io — key (proprietary nowcasting)
  worldweatheronline/ World Weather Online — key (basic single-model)
  weatherstack/       Weatherstack — key (basic, plaintext HTTP — least preferred)

modules/
  base.py             BotModule base class — the interface every plugin implements
  geocode.py          Location resolution via Nominatim (supports city names, zip codes, lat/lon)
  units.py            Temperature, wind, pressure, and distance formatting with dual-unit display
  weather.py          Weather command handler — calls weather_providers for data
  location.py         User location registration and lookup
  calc.py             Expression evaluator
  dice.py             Dice roller with XdN+M notation
  translate.py        Translation via Google Translate
  urbandictionary.py  Urban Dictionary lookups with result pagination
  stocks.py           Stock and crypto price lookup (Finnhub, Alpha Vantage, Twelve Data)
  imdb.py             Movie/TV lookup via OMDb API
  lastfm.py           Last.fm user profile and now-playing track
  youtube.py          YouTube video search with stats
  dictionary.py       English dictionary definitions (Free Dictionary API)
  ipinfo.py           IP/hostname geolocation lookup (ip-api.com)
  urls.py             URL shortener (is.gd) and expander
  steam.py            Steam user status, games, and nick-to-ID registration
  twitch.py           Twitch stream, channel, and game lookup (Helix API)
  idlerpg.py          IdleRPG player lookup (configurable endpoint)
  qdb.py              Quote database lookup (configurable QDB-compatible endpoint)
  fml.py              FMyLife random quote
  search.py           Web and image search (DuckDuckGo + optional Brave)
  bofh.py             Bastard Operator From Hell excuse generator
  channels.py         Join/part management and per-channel user roster queries

tests/
  run_tests.py        Standalone test suite (no external dependencies)
```

The core (`internets.py`) owns the asyncio event loop, IRC state machine, and command dispatch. Everything else is a module. Modules register commands via a `COMMANDS` dict mapping command names to async method names, receive `(nick, reply_to, arg)` on invocation, and talk back through `bot.privmsg()` / `bot.notice()` / `bot.reply()`. Every command invocation runs as an `asyncio.Task`.

The outbound path goes through `Sender`, an async drain loop over `asyncio.PriorityQueue` that implements a token-bucket (5 burst, ~40 msg/min sustained) to stay under IRC flood limits. Protocol messages (PONG, CAP, NICK) bypass the bucket at priority 0. `Sender.enqueue()` is thread-safe via `loop.call_soon_threadsafe()`.

## Design Decisions

**Async architecture.** The bot runs on a single asyncio event loop. The connection, line reading, command dispatch, send queue, keepalive, and console all run as async tasks or coroutines. Module command handlers are coroutines too — blocking I/O (HTTP via `requests`, password hashing) runs via `asyncio.to_thread()` inside the handler. This keeps the event loop free for protocol processing while still supporting the `requests` library without requiring `aiohttp` as an additional dependency.

**Founder-gated channel control.** `.join` and `.part` require the requesting user to be either a bot admin or the registered channel founder. Founder verification is done asynchronously: the bot WHOIS-es the user for their NickServ account and queries IRC services (`INFO #channel`) for the channel founder, then compares. This works across Anope, Atheme, Epona, X2, X3, and forks — anything that responds with a `Founder:` or `Owner:` line. The services bot nick is configurable via `services_nick` in `config.ini` (default: `ChanServ`). Users who aren't the founder can still bring the bot in via IRC's native `/INVITE`, which is always accepted. Joined channels are persisted to `channels.json` and restored on reconnect.

**Two-tier rate limiting.** A global per-nick flood gate drops commands that arrive faster than `flood_cooldown` seconds. A separate API cooldown rate-limits expensive operations (geocoding + weather API calls). Authed admins bypass the flood gate but not the API cooldown. This is a deliberate split: we don't want a fast-typing admin to trigger provider rate limits, but we also don't want them locked out of `.reload` during an incident.

**Multi-provider weather with capability-based dispatch.** Weather queries go through a Dispatcher that auto-discovers each provider's capabilities via `hasattr()` on method names (`get_weather`, `get_hourly`, `get_alerts`, etc.). For each request, the dispatcher builds a chain of providers that support the requested capability, sorts them by: (1) the static per-capability accuracy rank in `_dispatch.DEFAULT_RELIABILITY`, (2) live health (success rate, latency, rate-limit errors), (3) registration order from `provider_priority` in `[weather_providers]`. Each provider is a sub-module package with one file per API endpoint (e.g. `weather_providers/openmeteo/current.py`, `weather_providers/weatherapi/hourly.py`). All responses are normalized to shared dataclasses (`WeatherResult`, `HourlyResult`, `AlertsResult`, `AirQualityResult`, `UVResult`, `PollenResult`, `WildfireResult`, `SpaceWeatherResult`, `TideResult`, etc.) so the command module never touches raw API responses. Many providers need no credentials (NWS, Open-Meteo, MET Norway, SunriseSunset, GDACS, ECCC, NASA POWER, NIFC, NOAA SWPC, NOAA CO-OPS, currentuvindex); the remaining ~19 keyed providers register only when their keys are present in the secret store.

**Response routing.** Regular output goes to the channel. Help text and admin command responses go as `NOTICE` to the requesting user (keeps help spam out of channels). Everything in PM stays as `PRIVMSG`. This is the `reply()` / `preply()` split.

**IRCv3 capability negotiation.** The bot requests `multi-prefix`, `away-notify`, `account-notify`, `chghost`, `extended-join`, `server-time`, `message-tags`, and `sasl`. If the server supports SASL and a NickServ password is configured, the bot authenticates via SASL PLAIN during capability negotiation — before registration completes. This eliminates the timing race between NickServ IDENTIFY and channel joins. If SASL fails, the bot falls back to traditional NickServ IDENTIFY. All capabilities degrade gracefully if the server supports none of them.

## Requirements

Python 3.10 or later. Install the full runtime stack with a single command:

```
pip install -r requirements.txt
```

This pulls in every package listed below. `scrypt` is built into Python's `hashlib`, so it needs no install.

| Package | Required for |
|---|---|
| [`requests`](https://pypi.org/project/requests/) | Core HTTP client used by every module that talks to a third-party API. Pinned `>=2.32.3` for CVE-2024-35195. |
| [`aiohttp`](https://pypi.org/project/aiohttp/) | True async HTTP transport for weather provider calls (falls back to `requests` + `asyncio.to_thread` if missing). |
| [`argon2-cffi`](https://pypi.org/project/argon2-cffi/) | Argon2id admin password hashing — recommended (memory-hard, GPU-resistant). |
| [`bcrypt`](https://pypi.org/project/bcrypt/) | bcrypt admin password hashing — alternative to Argon2id. |
| [`PyJWT`](https://pypi.org/project/PyJWT/) + [`cryptography`](https://pypi.org/project/cryptography/) | Apple WeatherKit JWT signing (ES256). Needed only if WeatherKit credentials are configured. |
| [`defusedxml`](https://pypi.org/project/defusedxml/) | Hardened XML parser used by `modules/qdb.py`. Blocks billion-laughs DoS on top of stdlib's XXE protection. |

For development (tests, linting, security scans), install the editable dev extras:

```
pip install -e ".[dev]"
```

This adds `pytest`, `pytest-asyncio`, `pytest-cov`, `coverage`, `bandit`, `pip-audit`, and `build`.

## Setup

**Generate an admin password hash:**

```
python hashpw.py                    # defaults to scrypt
python hashpw.py --algo bcrypt
python hashpw.py --algo argon2
```

Paste the output into `config.local.ini` under `[admin] password_hash`. Plaintext passwords are rejected at startup.

**Set up your config:**

`config.ini.example` is the committed template — never edit it with real values. Your gitignored local files:

| File | Purpose | What goes in it |
|------|---------|-----------------|
| `config.ini` (0600) | Settings + credentials | Everything: server / nickname / modules autoload, **plus** the `[secrets]` section (NickServ / SASL / server / oper passwords, every API key, the User-Agent contact identifier) |
| `config.local.ini` (optional) | Non-secret personal overrides | Loaded on top of `config.ini` — useful if you want a personal overlay file separate from the main config |

**Quickest path** (works without any extra Python packages, foreground-friendly, works inside tmux / systemd / screen):

```
python -m secret_store init        # copies config.ini.example → config.ini (0600)
$EDITOR config.ini                 # paste your real values (including [secrets])
```

`config.ini.example` lists every supported key with signup URLs and tier limits inline. Edit `config.ini` like any other config file — the bot only reads values from `[secrets]` if perms are `0600`.

**Upgrading from a pre-`[secrets]` deployment (had a separate `secrets.ini`):**

```bash
# Inside your bot directory, after git pull:
{ echo; cat secrets.ini; } >> config.ini       # append [secrets] from old file
shred -u secrets.ini                            # securely remove the old file
chmod 600 config.ini                            # required — bot refuses 0644
```

Then restart the bot.  `INTERNETS_<NAME>` environment variables still win over the file, so anything stored there keeps working untouched.

**Environment variables** override the file: `export INTERNETS_NICKSERV_PASSWORD=...` for container/CI setups.

Useful commands:

```
python -m secret_store status                # backends available
python -m secret_store list                  # all known secrets + which backend holds each
python -m secret_store get <name>            # non-revealing: prints "(set, N chars, backend=X)"
python -c "import secret_store; print(secret_store.get('<name>'))"   # extract the value (for rotation)
python -m secret_store set <name>            # prompt for value, store in config.ini[secrets]
python -m secret_store delete <name>         # remove from config.ini[secrets]
python -m secret_store migrate               # sweep plaintext from other sections into [secrets]
```

See [Security](#security) below for the threat model and visibility guarantees.

**Run:**

```
python internets.py
```

**Add to a channel:**

Anyone can invite the bot via IRC's native INVITE (the server enforces permissions):

```
/INVITE Internets #yourchannel
```

Or the registered channel founder can use `.join` from any channel or PM:

```
.join #yourchannel
```

The bot verifies ownership by checking the user's NickServ account against the channel founder registered with IRC services (ChanServ, X3, etc.). Bot admins bypass this check. The bot remembers channels across restarts.

## Example Session

```
<alice> .w 10001
<Internets> :: New York, NY :: Conditions Partly Cloudy :: Temperature 18.3C / 64.9F ::
             Dew point 12.1C / 53.8F :: Pressure 1018mb / 30.06in :: Humidity 67% ::
             Visibility 16.1km / 10.0mi :: Wind from SW at 11.2km/h / 6.9mph ::
             Updated March 03, 02:51 PM UTC ::

<alice> .f 10001
<Internets> :: New York, NY :: Monday Partly Cloudy 19.2C / 66.6F / 11.8C / 53.2F ::
             Tuesday Rain 14.5C / 58.1F / 9.3C / 48.7F :: Wednesday Mainly Clear
             16.7C / 62.1F / 8.1C / 46.6F :: Thursday Overcast 13.4C / 56.1F /
             7.2C / 45.0F ::

<bob> .cc sqrt(144) + 2pi
<Internets> [calc] sqrt(144) + 2pi = 18.283185

<carol> .d 3d6+2
<Internets> :: Total 14/20 [71%] :: Rolls [4, 5, 3] ::

<dave> .t es Hello, how are you?
<Internets> [t] [en→es] Hola, ¿cómo estás?

<alice> .regloc 90210
<Internets> alice: location set to Beverly Hills, CA

<alice> .w
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 22.1C / 71.8F :: ... :: [NWS] ::

<alice> .w -n bob
<Internets> :: Chicago, IL :: Conditions Overcast :: Temperature 8.4C / 47.1F :: ... :: [NWS] ::

<alice> .u yolo /2
<Internets> [2/7] An acronym for "you only live once", used to justify doing ...
```

Admin session (via PM):

```
-> *Internets* AUTH mypassword
<Internets> Authentication successful.

<alice> .modules
<Internets> Loaded (21): bofh (2), calc (1), channels (3), dice (1), dictionary (2), fml (1), idlerpg (2), imdb (1), ipinfo (1), lastfm (1), location (3), qdb (1), search (4), steam (2), stocks (3), translate (1), twitch (2), urbandictionary (2), urls (3), weather (19), youtube (2)
<Internets> Use .help to see commands grouped by module.

<alice> .reload weather
<Internets> 'weather' unloaded. 'weather' loaded (19 commands).

<alice> .version
<Internets> Internets 3.0.0 — async modular IRC bot  https://github.com/brandontroidl/Internets
```

CLI startup:

```
$ python internets.py --version
Internets 3.0.0

$ python internets.py
2026-05-20 14:00:01 [INFO] internets.modules: Loaded calc (['cc'])
2026-05-20 14:00:01 [INFO] internets.modules: Loaded weather (['weather', 'w', 'forecast', 'f', ...])
...
2026-05-20 14:00:02 [INFO] internets.conn: Connecting irc.example.org:6697 (SSL)
2026-05-20 14:00:03 [INFO] internets.sasl: Starting SASL PLAIN authentication
2026-05-20 14:00:03 [INFO] internets.conn: Joined #mychannel
> status
  version  = 3.0.0
  nick     = Internets
  channels = #mychannel
  modules  = advice, apod, bofh, bored, calc, catfact, channels, chuck, cocktail, cowsay, crypto, dadjoke, devutils, dice, dictionary, dnd, fact, fml, fx, games, hn, httpcode, idlerpg, imdb, ipinfo, iss, lastfm, location, mtg, notes, numberfact, poke, qdb, qr, recipe, reddit, remind, search, seen, spacex, steam, stocks, tell, translate, twitch, urbandictionary, urls, weather, xkcd, youtube
  admins   = (none)
>
```

## Configuration

The bot reads `config.ini` at startup. Relevant sections:

**`[irc]`** — Server connection. Supports SSL (default on), optional certificate verification bypass for self-signed certs, NickServ identification, server password (for bouncers), and IRC operator credentials. Also configurable: `user_modes` (applied after connect, e.g. `+ix`), `oper_modes` (applied after OPER succeeds, e.g. `+s`), and `oper_snomask` (server notice mask, e.g. `+cCkKoO`).

**`[bot]`** — Command prefix (default `.`), rate limiting (`api_cooldown`, `flood_cooldown`), file paths for persistent storage (`locations_file`, `channels_file`, `users_file`), `default_location` (fallback coordinates), `modules_dir`, `autoload` list, `services_nick` (IRC services bot for channel ownership verification, default `ChanServ`), and `user_max_age_days` (prune user tracking entries older than this, default 90).

**`[admin]`** — Hashed password for admin authentication. Supports `scrypt$`, `bcrypt$`, and `argon2$` prefixes.

**`[weather]`** — User-Agent template and default unit system. The actual contact identifier (URL or email) lives in `config.ini[secrets]` as `weather_user_agent`.

**`[weather_providers]`** — `provider_priority` is a comma-separated list controlling registration order and the final tie-breaker after the accuracy rank + live health scores. NWS and Open-Meteo need no credentials. Every other provider's key lives in `config.ini[secrets]` (`weatherapi_key`, `tomorrowio_key`, `openweathermap_key`, `visualcrossing_key`, `pirateweather_key`, `weatherstack_key`, `accuweather_key`, `worldweatheronline_key`, `weatherbit_key`, `stormglass_key`, `airnow_key`, `purpleair_key`, `waqi_token`, `openaq_key`, `iqair_key`, `tidecheck_key`, `firms_key`, plus `meteomatics_username` / `meteomatics_password`, and four WeatherKit fields). Providers without credentials are silently skipped at startup; their per-command flag is hidden from `.help` and rejected by `-l`.

**`[stocks]`** — Multi-provider failover for `.stock` / `.crypto`. Keys (`finnhub_key`, `alphavantage_key`, `twelvedata_key`) live in `config.ini[secrets]`. Configure at least one to enable the module.

**`[imdb]`** / **`[lastfm]`** / **`[youtube]`** / **`[steam]`** / **`[twitch]`** — Each module reads its credential(s) from `config.ini[secrets]` via the secret store (`omdb_key`, `lastfm_key`, `youtube_key`, `steam_key`, `twitch_client_id` + `twitch_client_secret`). See `config.ini.example` for signup URLs and free-tier limits. `[steam]` keeps the non-secret `steamids_file` path (default `steamids.json`).

**`[idlerpg]`** — `api_url` for the IdleRPG XML endpoint (default: Rizon's `http://idlerpg.rizon.net/xml.php`). No key required.

**`[qdb]`** — `api_url` for a QDB-compatible XML endpoint. qdb.us is defunct; leave blank to keep `.qdb` hidden, or set it to any working QDB-compatible endpoint. No key required.

**`[search]`** — Web search defaults to DuckDuckGo (free, no key). Image search and an upgraded web tier need `brave_key` in `config.ini[secrets]` (Brave Search API, 2,000 queries/month free).

**`[logging]`** — Log level, output file, rotation, and optional debug file.  The
main log is rotated at `max_bytes` (default 5 MB) keeping `backup_count` old
copies (default 3).  Set `debug_file` to a path to capture ALL output at DEBUG
level regardless of the main `level` setting — useful for protocol diagnostics.
Runtime control via `.loglevel` and `.debug` admin commands (see below).

Config can be reloaded at runtime with `.rehash`, which also invalidates all active admin sessions.

## Commands

### User Commands

| Command | Description |
|---------|-------------|
| `.help` | Show available commands (admin commands visible only when authed) |
| `.modules` | List loaded modules (with command counts) and unloaded ones available on disk |
| `.weather` / `.w [-flag] [location]` | Current conditions — worldwide (multi-provider) |
| `.forecast` / `.f [-flag] [location]` | Multi-day forecast — worldwide (multi-provider) |
| `.hourly` / `.h [location]` | Hourly forecast — next 12 hours |
| `.alerts` / `.al [location]` | Active weather alerts and warnings |
| `.aqi` / `.air [location]` | Air quality index and pollutants |
| `.astro` / `.sun [location]` | Sunrise, sunset, moon phase |
| `.history` / `.hist [YYYY-MM-DD] [location]` | Weather on a past date |
| `.marine` / `.sea [location]` | Ocean conditions — waves, swell, water temp |
| `.nowcast` / `.nc [location]` | Precipitation nowcast — next 1-2 hours |
| `.providers` | Provider health and capability status (admin only) |
| `.regloc <location>` | Save your default location |
| `.myloc` | Show your saved location |
| `.delloc` | Delete your saved location |
| `.cc <expression>` | Calculator (supports math functions, implicit multiplication) |
| `.d [X]dN[+/-M]` | Dice roller |
| `.t [src] <tgt> <text>` | Translate text |
| `.u <word> [/N]` | Urban Dictionary lookup |
| `.stock <symbol>` / `.s <symbol>` | Stock quote (price, change, open/high/low, volume) |
| `.crypto <symbol>` | Cryptocurrency price in USD |
| `.imdb <title>` | Movie/TV lookup (rating, genre, director, actors, plot) |
| `.lastfm <user>` | Last.fm profile with play count and now-playing track |
| `.yt <search>` / `.youtube <search>` | YouTube video search with stats |
| `.dict <word> [/N]` / `.dictionary` | English dictionary definition |
| `.ipinfo <ip/host>` | IP/hostname geolocation lookup |
| `.shorten <url>` | Shorten a URL via is.gd |
| `.expand <url>` / `.unshorten <url>` | Expand a shortened URL |
| `.steam [user/-g/-n nick]` | Steam user status and game info |
| `.regsteam <id/vanity>` | Register your Steam ID |
| `.tw [-s query]` / `.twitch` | Search Twitch streams (default: top live) |
| `.tw -c <channel>` | Twitch channel info |
| `.tw -g <game>` | Search Twitch games |
| `.irpg <player>` / `.idlerpg` | IdleRPG player lookup |
| `.qdb [id]` | Random or specific quote from configured QDB |
| `.fml` | Random FMyLife quote |
| `.sw <query>` / `.g <query>` | Web search (DuckDuckGo) |
| `.si <query>` / `.gi <query>` | Image search (Brave API key required) |
| `.bofh` / `.excuse` | Random BOFH excuse |
| `.join <#channel>` | Invite the bot — requires channel founder or admin |
| `.part <#channel>` | Remove the bot — requires channel founder or admin |
| `.users [#channel]` | Show known users in a channel |

All weather commands accept city names, zip codes, raw `lat,lon` pairs, or `-n nick` to look up another user's saved location.

In PM, the `.` prefix is optional — `weather 10001` works the same as `.weather 10001`.

`.help` skips modules whose `is_configured()` returns False (e.g. `imdb` without an `omdb_key`), so users only see commands they can actually run. `.modules` shows every loaded module and the unloaded ones available on disk.

#### Weather provider flags

Every weather command accepts per-provider flags (anywhere in the line, before or after the location) to force a specific source instead of letting the dispatcher choose. The default chain is ranked by scientific accuracy — see [the architecture section](#architecture) for the ordering.

| Flag | Provider | Notes |
|------|---------|-------|
| `-nws` | NWS (Weather.gov) | US only — NDFD + HRRR + WaveWatch III |
| `-mm` / `-meteomatics` | Meteomatics | ECMWF/ICON/GFS blend (paid) |
| `-aw` / `-wk` / `-apple` / `-appleweather` / `-weatherkit` | Apple WeatherKit | NWS + IBM TWC blend |
| `-om` / `-openmeteo` | Open-Meteo | Free; ECMWF/ICON/GFS + CAMS AQ + ERA5 |
| `-vc` / `-visualcrossing` | Visual Crossing | ECMWF + ERA5 reanalysis |
| `-acc` / `-accuweather` | AccuWeather | Proprietary long-range |
| `-owm` / `-openweathermap` | OpenWeatherMap | GFS + ECMWF + CAMS AQ |
| `-wb` / `-weatherbit` | WeatherBit | GFS + station obs |
| `-wapi` / `-weatherapi` | WeatherAPI.com | GFS-derived |
| `-pw` / `-pirate` / `-pirateweather` | Pirate Weather | Dark Sky compatible; HRRR + MRMS for US nowcast |
| `-sg` / `-stormglass` | Stormglass | Marine specialist (7-model wave blend) |
| `-tio` / `-tomorrow` / `-tomorrowio` | Tomorrow.io | Proprietary nowcasting |
| `-wwo` / `-worldweatheronline` | World Weather Online | Basic single-model |
| `-ws` / `-weatherstack` | Weatherstack | Basic; least preferred |
| `-l` | (list mode) | List active providers ranked by accuracy for that capability |

Examples:

```
<alice> .w 67127 -aw
<Internets> :: Wichita, KS, USA :: Conditions Mostly Clear :: Temperature 18.3C / 64.9F :: ... :: [Apple Weather] ::

<alice> .w -vc Tokyo
<Internets> :: Tokyo, Japan :: Conditions Light rain :: ... :: [Visual Crossing] ::

<alice> .f -nws -n bob
<Internets> :: Boston, MA, USA :: Today Partly Sunny 22C / 12C :: Tomorrow Sunny 25C / 14C :: ... :: [NWS] ::

<alice> .marine -sg
<Internets> :: Newport, RI, USA :: Waves 1.2m / 3.9ft (8s, ENE) :: Swell 0.8m / 2.6ft (10s, E) :: [Stormglass] ::

<alice> .w -l
<Internets> alice: current providers (most → least accurate): 1.nws [OK] (-nws), 2.openmeteo [OK] (-om/-openmeteo), 3.weatherapi [?] (-wapi/-weatherapi), ...
<Internets> alice: legend  [OK]=auth ok, calls succeeding  [?]=loaded, untested  [X]=loaded but failing
```

If you force a provider that isn't active (no API key in the secret store) or doesn't support the requested capability (e.g. `-ws` for marine), the bot says so and aborts — no silent fallback when you've made an explicit choice.

### Admin Commands

Authenticate first: `/MSG Internets AUTH <password>`

| Command | Description |
|---------|-------------|
| `.auth <password>` | Authenticate (PM only) |
| `.deauth` | End admin session (PM only) |
| `.load <module>` | Load a module |
| `.unload <module>` | Unload a module |
| `.reload <module>` | Reload a module |
| `.reloadall` | Reload all loaded modules |
| `.restart` | Full process restart via `execv` |
| `.rehash` | Reload `config.ini` and clear admin sessions |
| `.mode <+/-modes>` | Set bot user modes (e.g. `.mode +ix`) |
| `.snomask <+/-flags>` | Set server notice mask (e.g. `.snomask +cCkK`) |
| `.loglevel [LEVEL]` | Show or set log output level (DEBUG/INFO/WARNING/ERROR) |
| `.loglevel <logger> <LEVEL>` | Set level for a specific subsystem (e.g. `.loglevel internets.weather DEBUG`) |
| `.debug [on\|off]` | Toggle global debug output |
| `.debug <subsystem> [off]` | Debug a single subsystem (e.g. `.debug weather`) |
| `.shutdown [reason]` / `.die [reason]` | Save state, unload modules, quit cleanly |

### Console Commands

When running interactively (stdin is a TTY), the bot starts a console task.
Type commands at the `>` prompt — no auth required.  Disable with `--no-console`
or when running under a process manager (auto-detected: console is skipped when
stdin is not a TTY).

| Console Command | IRC Equivalent |
|-----------------|----------------|
| `debug [on\|off]` | `.debug [on\|off]` |
| `debug <sub> [off]` | `.debug <sub> [off]` |
| `loglevel [LEVEL]` | `.loglevel [LEVEL]` |
| `loglevel <logger> LEVEL` | `.loglevel <logger> LEVEL` |
| `status` | *(no equivalent — shows nick, channels, modules, admins, log state)* |
| `shutdown [reason]` | `.shutdown [reason]` |
| `help` | *(shows console commands)* |

## Writing a Module

Create a Python file in `modules/`. Implement `setup(bot)` returning a `BotModule` subclass. Define commands in the `COMMANDS` dict.

```python
from __future__ import annotations
from .base import BotModule

class PingModule(BotModule):
    COMMANDS: dict[str, str] = {"ping": "cmd_ping"}

    async def cmd_ping(self, nick: str, reply_to: str, arg: str | None) -> None:
        self.bot.privmsg(reply_to, f"{nick}: pong")

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}ping   Pong"]

def setup(bot: object) -> PingModule:
    return PingModule(bot)
```

The bot passes `nick` (who sent the command), `reply_to` (the channel or nick to respond to), and `arg` (everything after the command, or `None`). All command handlers are coroutines (`async def`). Use `self.bot.privmsg()` for public responses, `self.bot.notice()` for private ones, or `self.bot.reply()` / `self.bot.preply()` for automatic routing.

For blocking I/O (HTTP, disk, CPU-heavy work), use `await asyncio.to_thread(...)`:

```python
import asyncio, requests

async def cmd_fetch(self, nick, reply_to, arg):
    resp = await asyncio.to_thread(requests.get, "https://api.example.com/data", timeout=10)
    self.bot.privmsg(reply_to, f"Got: {resp.json()}")
```

Available from `self.bot`: `cfg` (ConfigParser), `loc_get(nick)`, `loc_set(nick, raw)`, `loc_del(nick)`, `rate_limited(nick)`, `flood_limited(nick)`, `is_admin(nick)`, `channel_users(channel)`, `active_channels`, `send(raw_irc, priority)`.

Lifecycle hooks: `on_load()` runs after the module is registered. `on_unload()` runs before it's removed. `on_raw(line)` is called for every incoming IRC line (after IRCv3 tag stripping) and lets modules react to server numerics, NOTICEs, or any other traffic the core doesn't dispatch as a command. Use these for setup, cleanup, and advanced protocol integration.

## Adding a Weather Provider

Create a package directory in `weather_providers/` with one sub-module per API endpoint. The dispatcher auto-discovers capabilities from method names.

**1. Create the package:**

```
weather_providers/myprovider/
    __init__.py      ← provider class, delegates to sub-modules
    current.py       ← get_weather endpoint
    forecast.py      ← get_forecast endpoint
    _codes.py        ← shared helpers (optional)
```

**2. Implement endpoint sub-modules** (e.g. `current.py`):

```python
from weather_providers._http import get_json
from weather_providers.base import WeatherResult

async def fetch(api_key, lat, lon, location):
    data = await get_json("https://api.myweather.com/current",
                          params={"key": api_key, "lat": lat, "lon": lon})
    return WeatherResult(
        source="MyWeather",
        temperature=data.get("temp_c"),
        description=data.get("condition", "Unknown"),
        location=location,
    )
```

**3. Create the provider class** (`__init__.py`):

```python
from weather_providers.base import *
from . import current, forecast

class MyProvider:
    name = "MyWeather"
    requires_key = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def get_weather(self, lat, lon, location, **kwargs):
        return await current.fetch(self._key, lat, lon, location)

    async def get_forecast(self, lat, lon, location, days=4, **kwargs):
        return await forecast.fetch(self._key, lat, lon, location, days)
```

**4. Register the factory** in `weather_providers/__init__.py`:

```python
def _f_myprovider(cfg):
    key = cfg.get("weather_providers", "myprovider_key", fallback="").strip()
    if not key: return None
    from .myprovider import MyProvider
    return MyProvider(key)

_reg("myprovider", _f_myprovider)
```

**5. Add config keys** to `config.ini` under `[weather_providers]`:

```ini
myprovider_key = your-api-key-here
```

Add `myprovider` to the `provider_priority` list. The dispatcher auto-discovers capabilities from method names (`get_weather` → "current", `get_hourly` → "hourly", etc.), tracks provider health, and handles fallback automatically. Add more `async def get_*` methods to support additional capabilities (see `CAPABILITY_METHODS` in `_dispatch.py` for the full list).

## Configuring Apple WeatherKit

WeatherKit is a built-in provider for Apple Developer Program members. It is not enabled by default.

**Prerequisites:**
- Apple Developer Program membership ($99/year, includes 500K WeatherKit API calls/month)
- `PyJWT` and `cryptography` packages: `pip install internets-irc[weatherkit]`

**Setup:**

1. In the [Apple Developer portal](https://developer.apple.com/account), go to Identifiers and create a new **Services ID** with WeatherKit enabled.

2. Go to Keys and create a new key with **WeatherKit** capability. Download the `.p8` private key file.

3. Store the four values in `config.ini[secrets]`:

```ini
; config.ini, under [secrets]
weatherkit_team_id    = YOUR_TEAM_ID
weatherkit_service_id = com.example.weatherkit-client
weatherkit_key_id     = YOUR_KEY_ID
weatherkit_key_file   = /path/to/AuthKey_XXXXXXXX.p8
```

The `weatherkit_key_file` field is a *path* to the `.p8` file, not its contents — keep the key file outside `config.ini`.

4. The bot signs JWT/ES256 tokens with the private key. Tokens are cached and refreshed before expiry. Apple requires the source to display as "Apple Weather" — the bot handles this via the `[Apple Weather]` tag in output.

If `PyJWT` / `cryptography` are not installed or any of the four values are missing, the WeatherKit provider is silently skipped and the next provider in the chain takes over.

## Operational Notes

**Nick collision recovery:** If the configured nick is taken, the bot appends `_` and retries.

**Auto-reconnect with exponential backoff.** On disconnect, the bot reconnects with exponential backoff: 15s, 30s, 60s, 120s, 240s, capped at 5 minutes. The attempt counter resets on successful connection. Channel list is restored from `channels.json`. If SASL is available, identification happens during registration. Otherwise, if a NickServ password is configured, the bot waits for identification confirmation (up to 10 seconds) before sending JOINs so that `+R` channels and ChanServ access lists work. If a saved channel is invite-only (`+i`), the bot asks ChanServ to re-invite it. Channels that reject with 471 (full), 474 (banned), or 475 (bad key) are logged and removed from the saved list.

**Keepalive:** An async task sends `PING` every 90 seconds. If the read times out after 300 seconds with no data, the connection is presumed dead and the reconnect logic takes over.

**User tracking.** The bot maintains a per-channel registry of nicks, hostmasks, and first/last seen timestamps in memory, flushed to `users.json` every 30 seconds. Populated from observed JOINs, PARTs, QUITs, NICKs, and channel activity — it is not a complete roster (NAMES replies are not used for the general roster). Entries older than 90 days (configurable via `user_max_age_days` in `config.ini`) are automatically pruned during flushes.

**Channel ownership verification:** When a non-admin user runs `.join` or `.part`, the bot verifies they are the channel founder by WHOIS-ing them for their NickServ account (330 numeric) and querying the configured services bot (`services_nick`, default ChanServ) with `INFO #channel` for the founder name. If the account matches the founder (case-insensitive), the action proceeds. Verification times out after 15 seconds. This covers Anope, Atheme, Epona, X2, X3, and compatible forks. The services bot name is the only thing that varies — set `services_nick = X3` (or `Q`, etc.) in `config.ini` for non-ChanServ networks.

**Module conflicts:** If two modules try to register the same command, the second load is rejected with a conflict error.

## Security

**Secret store.** Outbound credentials (NickServ / SASL / server / oper passwords, every API key, the User-Agent contact identifier) live in the `[secrets]` section of a **gitignored** `config.ini`. Lookup order, first hit wins:

1. `INTERNETS_<NAME>` environment variable
2. Gitignored `config.ini` `[secrets]` section (0600 perms strictly enforced)

`config.ini.example` is the committed credential-free *structural* template — section names, non-secret defaults, comments, plus a placeholder `[secrets]` section listing every supported key with signup URLs and tier limits inline. Personal non-secret overrides may also go in an optional gitignored `config.local.ini` overlay.

These values are **not hashed**. Hashing is one-way; the bot has to send the literal password / API key on the wire, so the correct primitive is encryption-at-rest, not hashing. OS-keyring support was removed in v3.0.0 — the bot targets headless deployments where `keyring` has no usable backend; the 0600 `config.ini` (or `INTERNETS_*` env vars) is the storage.

**Visibility guarantees:**

- The bot never logs the *value* of any secret. Module `on_load()` logs presence only.
- Outbound IRC traffic is scrubbed for credential prefixes (`PASS`, `NS IDENTIFY`, `OPER`, `AUTHENTICATE`) before being logged by `sender.py`.
- `python -m secret_store get <name>` prints only `(set, N chars, backend=<env|file>)` — never the value. There is **no CLI flag to print the secret** (closes a scrollback / shell-history exposure surface). For legitimate extraction (key rotation), use `python -c "import secret_store; print(secret_store.get('<name>'))"` so the intent is explicit at the call site.
- `python -m secret_store list` shows the backend per secret, never the values.
- `config.ini` `[secrets]` is read only when `stat().st_mode & 0o777 == 0o600`. The store fails closed (returns empty) if perms are looser.
- Unconfigured providers and modules are hidden: the `BotModule.is_configured()` hook makes `.help` skip them, weather flags for unconfigured providers don't appear in `.w -l`, and forcing such a provider returns "not active" without making an API call.

**Authentication:** Admin passwords are hashed with scrypt (default), bcrypt, or argon2. Constant-time comparison via `hmac.compare_digest`. Brute-force lockout after 5 failures (5-minute cooldown). Sessions are tracked by nickname *and* hostmask — if a nick's hostmask changes after authentication (e.g. someone else takes the nick), the session is automatically invalidated. Sessions are also cleared on disconnect. Auth commands are restricted to PM. All auth state is protected by a dedicated `threading.Lock` for GIL-free Python compatibility.

**Transport:** TLS 1.2 minimum enforced. No fallback to TLS 1.0/1.1. Certificate verification enabled by default (`ssl_verify = true`). Set `ssl_verify = false` only for servers with self-signed certs.

**Input validation:** Module names validated against `^[a-z][a-z0-9_]*$`. Channel names validated against IRC format regex. Command arguments capped at 400 characters. PRIVMSG/NOTICE targets validated (no spaces). All user input treated as untrusted.

**Protocol compliance:** Outgoing lines capped at 512 bytes (RFC 2812). Incoming lines limited to 8KB buffer. CRLF/NUL stripped from all outgoing messages. PING payload reflection capped at 400 bytes.

**Injection prevention:** Log injection prevented via `_SafeFormatter` (sanitizes msg and args). No `eval()`/`exec()` anywhere — calculator uses AST walker with strict whitelist. Module loader blocks symlink traversal. Config path resolved to absolute at startup.

**Resource limits:** Concurrent command tasks capped at 50. Sender queue bounded at 200 messages. INVITE acceptance rate-limited (5s cooldown). Store data files capped at 10MB on load. API and flood rate limiters per-nick.

**Information disclosure:** All error messages sent to IRC are generic ("see log for details"). No stack traces, file paths, or internal state exposed. Outgoing credentials (PASS, IDENTIFY, OPER, AUTHENTICATE) redacted in logs.

**Cross-platform:** Config permission check guarded for POSIX. Store I/O uses explicit UTF-8 encoding. Temp file cleanup is exception-safe on Windows. Restart uses subprocess on Windows (os.execv doesn't replace the process). math.cbrt fallback for Python < 3.11.

## Testing

154+ automated tests across `tests/run_tests.py` (standalone, no dependencies) and `tests/test_*.py` (pytest). No external test framework required for the standalone suite:

```
python tests/run_tests.py
```

For the pytest suite:

```
pip install pytest
pytest tests/ -v
```

Covers protocol parsing, store operations (CRUD, flush, atomic writes, pruning, type validation), calculator sandboxing and DoS guards, dice, weather provider registry, capability-based dispatch, provider health scoring, configuration parsing, output formatting, unit conversions, sender injection prevention and line limits, password hashing, thread-safe containers, async architecture verification, rate limiting, and all security hardening fixes. Both test suites are compatible and can be run independently.

## Known Limitations

The translation module uses an undocumented Google Translate endpoint (`translate.googleapis.com`). It has no SLA and may break or be rate-limited without notice.

**Persistence:** The store loads all JSON files into memory once at startup. Mutations happen in-memory; a background thread flushes dirty data to disk every 30 seconds. Each dataset (locations, channels, users) has its own lock, so weather lookups never block on user-tracking writes. Worst-case data loss on a hard crash is 30 seconds of user-tracking timestamps — channel list and location changes are also flushed on `.shutdown`, `.restart`, and signal handlers.

The bot does not parse `353` (NAMES reply) for user roster purposes. Users who were already in the channel when the bot joined will not appear in `.users` output until they trigger an observable event (JOIN, PART, QUIT, NICK, or sending a message).

**Atomic writes on Windows:** `os.replace()` is atomic on POSIX but not guaranteed atomic on NTFS. It is the best Python offers cross-platform. Data loss from a crash during the brief write window is unlikely but theoretically possible on Windows.

## License

ISC — see [LICENSE.md](LICENSE.md).
