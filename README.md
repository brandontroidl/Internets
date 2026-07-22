# Internets v5.0.0

A modular IRC bot and multi-provider weather aggregator on Python's asyncio and RFC 2812. Worldwide weather (current, forecast, hourly, nowcast, air quality, UV, pollen, astronomy, alerts, wildfire, space weather, marine, tides, historical), stock/crypto/FX prices, movie and music lookups, dictionary and reference tools (Wikipedia, DOI, ISBN, RFC, arXiv, periodic table, tldr-pages), a large developer/encoding/network/security toolkit, IP geolocation and reputation, science/infosec/AI/BSD news feeds, and stateful IRC-native tools (seen, tell, remind, notes). Plugin architecture with hot-reload: modules load, unload, and reload without restarting the bot.

This README is the entry point. Deep dives live in `docs/`:

- `docs/architecture.md` - core event loop, dispatch, state, the full module catalog
- `docs/configuration.md` - every `config.ini` section and key
- `docs/modules.md` - writing a module
- `docs/providers.md` - adding a weather provider
- `docs/security-model.md` - threat model, secret store, audit log, SSRF guard
- `docs/deployment.md` - running headless, process lock, metrics, ops

**Platform support:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2.
**Python:** 3.10+ (CI runs 3.10 through 3.14).
**License:** ISC.

## What it is

The core (`internets.py`) owns a single asyncio event loop, the IRC state machine, and command dispatch. Everything else is a module under `modules/`. A module declares a `COMMANDS` dict mapping command name to async method name; each handler receives `(nick, reply_to, arg)` and replies through `bot.privmsg()` / `bot.notice()` / `bot.reply()` / `bot.preply()`. Every command invocation runs as its own `asyncio.Task`, capped at 50 concurrent.

Weather is served by a capability-based dispatcher over **32 provider packages** (`weather_providers/`). The dispatcher auto-discovers each provider's capabilities by `hasattr()` on method names (`get_weather`, `get_hourly`, `get_alerts`, ...), then ranks the providers that support a requested capability by: (1) the static per-capability accuracy rank in `_dispatch.DEFAULT_RELIABILITY`, (2) live health (success rate, latency, rate-limit errors), (3) registration order from `provider_priority`. All responses normalize to shared dataclasses in `weather_providers/base.py`, so the command module never touches raw API JSON. Providers needing no key (NWS, Open-Meteo, MET Norway/Yr, SunriseSunset, GDACS, ECCC, NASA POWER, NIFC, NOAA SWPC, NOAA CO-OPS, currentuvindex) always register; keyed providers register only when their key is present in the secret store. For a `current` request the dispatcher skips an empty or all-N/A result and fills any missing secondary fields (dewpoint, pressure, visibility, humidity) from the next-ranked provider, bounded to 3 contributors and crediting both sources (e.g. `NWS + Open-Meteo`); see `weather_providers/_dispatch.py` plus `has_gaps` / `fill_gaps` in `weather_providers/base.py`.

## Architecture overview

```
internets.py     asyncio event loop, IRC state machine, command dispatch, reconnect
admin_cmds.py    AdminCommandsMixin: auth/load/reload/raw/say/stats/audit/shadow-ban/...
console.py       interactive stdin console (TTY only)
protocol.py      pure protocol helpers (ISUPPORT, MODE, SASL, NAMES parsing)
sender.py        async outbound queue, token-bucket flood control, credential scrubbing
store.py         in-memory state + periodic disk flush (locations, channels, users, opt-out)
config.py        config.ini parsing, __version__, CLI argparse (cli_args)
botlog.py        logging setup, _SafeFormatter (log-injection guard), rotation
hashpw.py        admin password hashing/verify (scrypt/bcrypt/argon2)
secret_store.py  two-tier secret store: INTERNETS_<NAME> env -> config.ini[secrets] (0600)
audit_log.py     append-only HMAC-chained tamper-evident log of privileged actions
metrics.py       opt-in Prometheus exporter (127.0.0.1 only; off by default)
process_lock.py  PID lock with stale-detection; blocks a second instance corrupting state

weather_providers/   32 provider packages + base dataclasses, _http, _dispatch, _health
modules/             72 command modules (see docs/modules.md for the catalog)
modules/base.py      BotModule base class, help_row(), strip_ctrl(), the plugin interface
modules/_netsafe.py  SSRF-safe fetch with DNS-TOCTOU pinning (probe, scinews article reader)
tests/               run_tests.py (standalone) + 39 pytest modules
```

The outbound path goes through `Sender`, an async drain over `asyncio.PriorityQueue` implementing a token bucket (5 burst, ~40 msg/min sustained) to stay under flood limits. Protocol messages (PONG, CAP, NICK) bypass the bucket at priority 0. `Sender.enqueue()` is thread-safe via `loop.call_soon_threadsafe()`. The queue is bounded at 200 messages.

Key design points (full detail in `docs/architecture.md`):

- **Blocking I/O off the loop.** HTTP via `requests`, password hashing, and disk writes run under `asyncio.to_thread()`. Weather provider calls prefer `aiohttp` and fall back to `requests` + `to_thread` if aiohttp is absent.
- **Two-tier rate limiting.** A per-nick flood gate (`flood_cooldown`) drops fast-repeated commands; a separate API cooldown (`api_cooldown`) throttles geocode + weather calls. Admins bypass the flood gate but not the API cooldown.
- **Founder-gated channel control.** `.join` / `.part` require bot-admin or verified channel founder (WHOIS account + services `INFO #channel`). IRC-native `/INVITE` is always accepted. Joined channels persist to `channels.json` and restore on reconnect.
- **IRCv3 + SASL.** Requests `multi-prefix`, `away-notify`, `account-notify`, `chghost`, `extended-join`, `server-time`, `message-tags`, `sasl`. SASL PLAIN runs during cap negotiation when a NickServ password is set, eliminating the IDENTIFY/JOIN race; falls back to NickServ IDENTIFY. All caps degrade gracefully.

## Requirements

Python 3.10+. `scrypt` is built into `hashlib` (no install).

```
pip install -r requirements.txt        # full runtime stack
```

| Package | Required for |
|---|---|
| `requests` (>=2.32.3) | Core HTTP client for every module hitting a third-party API. Pin closes CVE-2024-35195. |
| `aiohttp` (>=3.14.1) | Async HTTP transport for weather calls; falls back to `requests` + `to_thread` if absent. |
| `argon2-cffi` (>=23.1.0) | Argon2id admin password hashing (recommended). |
| `bcrypt` (>=4.2.0) | bcrypt admin password hashing (alternative). |
| `PyJWT` + `cryptography` | Apple WeatherKit ES256 JWT signing. Only if WeatherKit is configured. |
| `defusedxml` (>=0.7.1) | Hardened XML for `modules/qdb.py` (billion-laughs guard on top of stdlib XXE protection). |

Dev extras (tests, lint, security scans):

```
pip install -e ".[dev]"     # pytest, pytest-asyncio, pytest-cov, coverage, bandit, pip-audit, build
```

## Install and setup

**1. Admin password hash:**

```
python hashpw.py                # scrypt (default)
python hashpw.py --algo bcrypt
python hashpw.py --algo argon2
```

Paste the output into `config.ini` under `[admin] password_hash`. Plaintext passwords are rejected at startup.

**2. Config:**

`config.ini.example` is the committed credential-free template - never edit it with real values. Your gitignored local files:

| File | Perms | Holds |
|------|-------|-------|
| `config.ini` | 0600 | All settings (server / nick / autoload) plus the `[secrets]` section (NickServ/SASL/server/oper passwords, every API key, the User-Agent contact identifier) |
| `config.local.ini` | optional | Non-secret personal overrides, loaded on top of `config.ini` |

```
python -m secret_store init     # copies config.ini.example -> config.ini (0600)
$EDITOR config.ini              # paste real values, including [secrets]
```

The `[secrets]` section of `config.ini` is read **only** when its perms are exactly 0600; looser perms fail closed (the store returns empty). `INTERNETS_<NAME>` env vars override the file, for containers/CI:

```
export INTERNETS_NICKSERV_PASSWORD=...
```

Secret-store CLI:

```
python -m secret_store status                # backends available
python -m secret_store list                  # known secrets + which backend holds each (no values)
python -m secret_store get <name>            # non-revealing: "(set, N chars, backend=X)"
python -m secret_store set <name>            # prompt, store in config.ini[secrets]
python -m secret_store delete <name>         # remove from config.ini[secrets]
python -m secret_store migrate               # sweep plaintext from other sections into [secrets]
python -c "import secret_store; print(secret_store.get('<name>'))"   # extract a value (rotation)
```

There is intentionally no CLI flag that prints a secret value (closes a scrollback/shell-history surface). OS-keyring support was removed in 3.0.0; the bot targets headless hosts where `keyring` has no usable backend.

**Upgrading from a pre-`[secrets]` deployment (separate `secrets.ini`):**

```bash
{ echo; cat secrets.ini; } >> config.ini       # append old secrets
shred -u secrets.ini                            # securely remove old file
chmod 600 config.ini                            # required - bot refuses 0644
```

**3. Run:**

```
python internets.py
```

CLI flags (defined in `config.py`):

| Flag | Effect |
|------|--------|
| `--version` | Print `Internets <version>` and exit |
| `--debug [SUBSYSTEM ...]` | Global debug (no args) or per-subsystem (`--debug weather store`) |
| `--loglevel LEVEL` | Base log level: DEBUG/INFO/WARNING/ERROR |
| `--debug-file PATH` | Write all DEBUG to a separate file |
| `--no-console` | Disable the stdin console (for daemonized use) |

A `ProcessLock` (`internets.pid`) is taken around the event loop; a second instance against the same state directory refuses to start (prevents JSON state corruption). The interactive console starts only when `--no-console` is absent **and** stdin is a TTY; under systemd/pipes it auto-skips.

**4. Add to a channel:**

```
/INVITE Internets #yourchannel      # anyone; server enforces permissions
.join #yourchannel                  # registered founder or bot admin
```

Founder is verified by matching the user's NickServ account (WHOIS 330) against the services `INFO #channel` founder line (`services_nick`, default `ChanServ`). Works across Anope/Atheme/Epona/X2/X3 and forks.

## Configuration overview

`config.ini` is read at startup; `.rehash` reloads it and invalidates all admin sessions. Sections (full key list in `docs/configuration.md` and `config.ini.example`):

- **`[irc]`** - server, SSL (default on), `ssl_verify`, NickServ, server password, oper credentials, `user_modes`, `oper_modes`, `oper_snomask`.
- **`[bot]`** - command prefix (default `.`), `api_cooldown`, `flood_cooldown`, state file paths, `default_location`, `modules_dir`, `autoload`, `services_nick`, `user_max_age_days` (default 90).
- **`[admin]`** - `password_hash` with `scrypt$` / `bcrypt$` / `argon2$` prefix.
- **`[weather]`** - User-Agent template, default unit system. Contact identifier lives in `[secrets] weather_user_agent`.
- **`[weather_providers]`** - `provider_priority` (registration order + final tie-breaker). All keyed providers read their key from `[secrets]`.
- **`[stocks]`**, **`[imdb]`**, **`[lastfm]`**, **`[youtube]`**, **`[steam]`**, **`[twitch]`**, **`[search]`** - each reads its credential(s) from `[secrets]`; non-secret paths (e.g. `steamids_file`) stay in the section.
- **`[idlerpg]`** / **`[qdb]`** - `api_url` for the XML endpoint. No key.
- **`[logging]`** - level, file, `max_bytes` (5 MB), `backup_count` (3), optional `debug_file`. Runtime control via `.loglevel` / `.debug`.
- **`[metrics]`** - `enable` (default false), `host` (default 127.0.0.1; 0.0.0.0 is rejected), `port` (default 9779). See Operational notes.

## Commands

`.help` lists commands grouped by category and shows only modules whose `is_configured()` passes (e.g. `imdb` is hidden without `omdb_key`); admin commands appear only when authed. `.modules` lists every loaded module plus unloaded ones on disk. In PM the `.` prefix is optional. Command arguments are capped at 400 characters.

### Weather and space

| Command | Description |
|---------|-------------|
| `.weather` / `.w [-flag] [loc]` | Current conditions, worldwide multi-provider |
| `.forecast` / `.f [-flag] [loc]` | Multi-day forecast |
| `.hourly` / `.h [loc]` | Next 12 hours |
| `.nowcast` / `.nc [loc]` | Precipitation nowcast, next 1-2 hours |
| `.aqi` / `.air [loc]` | Air quality index and pollutants |
| `.uv` / `.uvi [loc]` | UV index |
| `.pollen` / `.allergy [loc]` | Pollen and allergy index |
| `.astro` / `.sun [loc]` | Sunrise, sunset, moon phase |
| `.alerts` / `.al [loc]` | Active weather alerts |
| `.wildfire` / `.fire [loc]` | Wildfire activity |
| `.space` / `.aurora [loc]` | Space weather and aurora |
| `.marine` / `.sea [loc]` | Ocean waves, swell, water temp |
| `.tides` / `.tide [loc]` | Tide predictions |
| `.history` / `.hist [YYYY-MM-DD] [loc]` | Weather on a past date |
| `.providers` | Provider health and capability status (admin only) |
| `.iss` | ISS location and current crew |
| `.spacex` | Next scheduled SpaceX launch |
| `.apod` | NASA Astronomy Picture of the Day |
| `.solar` | NOAA space weather: X-ray flare class and SSN |
| `.neo` | NASA near-earth objects today and closest |
| `.launches [n]` | Next 1-3 rocket launches |
| `.moon [YYYY-MM-DD]` | Moon phase, illumination, age |
| `.sky <M#\|name>` | Messier catalog lookup |
| `.passes <sat> <lat,lon>` | Next visible satellite pass (needs n2yo key) |

All weather commands accept city names, zip codes, raw `lat,lon`, or `-n nick` to use another user's saved location. Per-provider flags below force a source; the default chain is accuracy-ranked.

### Science and math

| Command | Description |
|---------|-------------|
| `.sci [topic]` | Science/infosec/AI/BSD headlines (**52 feeds**; topics: all, ai, cs, sec, pentest, tech, physics, math, bio, astro, space, bsd) |
| `.sci read <N>` | Read item N from the last list (lead and link) |
| `.sci sources` | List feed topics and feed count |
| `.isprime <n>` | Primality test and next prime |
| `.factor <n>` | Prime factorization |
| `.gcd <a> <b> [..]` | GCD and LCM |
| `.base <n> <from> <to>` | Convert between bases 2..36 |
| `.stats <n1 n2 ...>` | Mean/median/stdev/min/max/sum |
| `.roman <n\|numeral>` | Arabic to/from Roman (1..3999) |
| `.pct <expr>` | Percentages: `20% of 150`, `50 to 75`, `30 of 120` |
| `.bignum <expr>` | Exact `n!` / `fib(n)` / `2^n` |
| `.const <name>` | Physical constant value and unit |
| `.ly <distance>` | Light time to/from distance (ly/au/km/min) |
| `.sr <v>` | Special-relativity gamma for v as fraction of c |
| `.escape <body\|m r>` | Escape velocity and surface gravity |
| `.ohm <two of V,I,R,P>` | Ohm-law and power solver |
| `.rc <bands\|ohms>` | Resistor color code to/from value |
| `.baud <bytes> <bps>` | Serial transfer time (`-fmt 8N1`) |
| `.numberfact` / `.nf [n] [type]` | Number trivia (trivia/math/date/year) |

### Developer, encoding, network, security

| Command | Description |
|---------|-------------|
| `.cc <expression>` | Calculator (AST-whitelisted; math functions, implicit multiplication) |
| `.cidr <ip/prefix>` | Network/broadcast/mask/hosts/range |
| `.subnet <ip/prefix> <newlen>` | Split a block into subnets |
| `.port <number\|name>` | Port number to/from service name |
| `.b64` / `.unb64 <text>` | Base64 encode / decode |
| `.hex <text>` | Hex encode/decode (auto) |
| `.b32 <text>` | Base32 encode/decode (auto) |
| `.morse <text>` | Morse encode/decode (auto) |
| `.hash <algo> <text>` | md5/sha1/sha256/sha512/blake2b digest |
| `.crc <text>` | CRC32 and Adler-32 |
| `.unicode <char\|U+hex\|name>` | Codepoint / name / UTF-8 / block |
| `.ascii [dec\|hex\|char]` | ASCII dec/hex/oct/char/name |
| `.slug <text>` | Slugify text |
| `.uuid` / `.ulid` | Random UUID4 / ULID |
| `.uuid5 <ns> <name>` | Deterministic UUIDv5 |
| `.ds <value> <unit>` | Data-size convert (decimal and binary) |
| `.defang <url>` | Defang/refang URL/IP/email (auto) |
| `.entropy <password>` | Estimate password entropy |
| `.pw [len] [-s]` | Random password / passphrase |
| `.lorem [words]` | Lorem ipsum text |
| `.epoch [arg]` | Epoch to/from ISO 8601 UTC |
| `.jwt <token>` | Decode JWT header and payload (no sig check) |
| `.semver <a> <b>` | Compare two semantic versions |
| `.tz <time> <from> <to>` | Convert a clock time between zones |
| `.unix <signal\|errno>` | Look up a Unix signal or errno |
| `.color <value>` | hex/rgb/hsl convert and nearest CSS name |
| `.cron <expr>` | Validate/explain cron and next fire times |
| `.http <code>` | HTTP status code lookup |
| `.qr <text>` | QR-code image URL |
| `.pypi` / `.npm` / `.crates <name>` | Package registry lookup |
| `.gh <owner/repo>` | GitHub repo info |
| `.dns <host> [type]` | DNS lookup (A/AAAA/MX/TXT/NS/CNAME) |
| `.rdns <ip>` | Reverse PTR lookup |
| `.caa <domain>` | CAA records (and SPF/DMARC) |
| `.whois <domain>` | RDAP domain registration info |
| `.asn <ip\|ASn>` | RDAP network / AS info |
| `.headers <url>` | HTTP status/server/type/redirect/security headers (SSRF-guarded) |
| `.ssl <host[:port]>` | TLS cert issuer/CN/days-to-expiry |
| `.tcp <host> <port>` | TCP connect probe and latency |
| `.down <host\|url>` | Reachability check (up/down; SSRF-guarded) |
| `.cve <CVE-ID>` | NVD CVSS score, summary, date |
| `.pwn <password>` | HIBP breach count (PM-only) |
| `.hashid <hash>` | Identify likely hash type |
| `.cvss <vector>` | Compute CVSS v3.1 base score |
| `.cipher <name>` | Cipher reference (size/status) |
| `.ipinfo <ip/host>` | IP/hostname geolocation (ip-api.com) |
| `.ip <ip/host>` / `.rep` | IP reputation: DNSBL / DShield / GreyNoise / Tor / AbuseIPDB |

`.ip` / `.rep` (`modules/ipintel.py`) **queries** reputation sources and aggregates a verdict; it does not feed or write to any blocklist.

### Reference and language

| Command | Description |
|---------|-------------|
| `.dict <word> [/N]` / `.dictionary` | English dictionary definition |
| `.u <word> [/N]` / `.urbandictionary` | Urban Dictionary lookup |
| `.wiki <query>` | Wikipedia summary and link |
| `.doi <doi>` | Crossref work metadata |
| `.isbn <isbn>` | Open Library book lookup |
| `.so <query>` | Top Stack Overflow question |
| `.rfc <number>` | RFC title/status/date |
| `.rtfm <command>` | tldr-pages command reference (Unix/BSD/Linux) |
| `.arxiv <id\|query>` | arXiv paper lookup |
| `.element <name\|symbol\|Z>` | Periodic-table entry (offline) |
| `.t [src] <tgt> <text>` / `.translate` | Translate text |
| `.sw` / `.g <query>` | Web search (DuckDuckGo) |
| `.si` / `.gi <query>` | Image search (Brave API key required) |
| `.regloc <location>` | Save your default location |
| `.myloc` | Show your saved location |
| `.delloc` | Delete your saved location |

### Media and finance

| Command | Description |
|---------|-------------|
| `.imdb <title>` | Movie/TV lookup (rating, genre, cast, plot) |
| `.lastfm <user>` | Last.fm profile and now-playing |
| `.yt` / `.youtube <search>` | YouTube video search |
| `.xkcd [num]` | xkcd comic (random or specific) |
| `.mtg <card>` | Magic: the Gathering card (Scryfall) |
| `.poke` / `.pokemon <name\|id>` | Pokemon info (PokeAPI) |
| `.dnd <name>` | D&D 5e SRD spell or monster |
| `.recipe` / `.meal <name>` | Recipe lookup (TheMealDB) |
| `.cocktail` / `.drink <name>` | Cocktail recipe (TheCocktailDB) |
| `.steam [user/-g/-n nick]` / `.regsteam <id>` | Steam status / register ID |
| `.tw [-s\|-c\|-g]` / `.twitch` | Twitch streams / channel / game |
| `.irpg <player>` / `.idlerpg` | IdleRPG player lookup |
| `.hn [rank]` | Top Hacker News story (1-30) |
| `.reddit` / `.r <sub> [period]` | Top post from a subreddit |
| `.stock` / `.s <symbol>` | Stock quote (Finnhub / Alpha Vantage / Twelve Data failover) |
| `.crypto <symbol>` | Cryptocurrency price in USD (keyed stock providers) |
| `.gecko` / `.cg` / `.coingecko <symbol\|name>` | Crypto spot price + 24h change + market cap (CoinGecko, no key) |
| `.fx <from> <to> [amount]` | FX conversion (frankfurter.dev / ECB) |

`.crypto` (`modules/stocks.py`) needs a stock-provider key; `.gecko` / `.cg` (`modules/crypto.py`) is keyless via CoinGecko.

### Fun

| Command | Description |
|---------|-------------|
| `.d [X]dN[+/-M]` | Dice roller |
| `.coin` / `.8ball` / `.rps` / `.choose` | Coin flip, magic 8-ball, RPS, pick one |
| `.bofh` / `.excuse` | Random BOFH excuse |
| `.fml` | Random FMyLife quote |
| `.qdb [id]` | Random or specific QDB quote |
| `.advice` | Random piece of advice |
| `.bored` | Random activity suggestion |
| `.fact` | Random useless fact |
| `.catfact` / `.cat` | Random cat fact |
| `.chuck` | Random Chuck Norris joke |
| `.dadjoke` / `.joke` | Random dad joke |
| `.cowsay <text>` | ASCII cow speaks your text |

### Personal and social

| Command | Description |
|---------|-------------|
| `.remind <when> <msg>` | Schedule a reminder (`30s`, `5m`, `1h30m`, `14:30 UTC`, ISO) |
| `.remind-list` / `.remind-cancel <N>` | List or cancel your reminders |
| `.tell <nick> <msg>` | Leave a message for a nick |
| `.tell-list` / `.tell-cancel` | List or cancel your pending tells |
| `.notes <list\|add\|del\|show\|clear>` | Personal sticky notes |
| `.seen <nick>` | When a nick was last seen |
| `.shorten <url>` | Shorten a URL via is.gd |
| `.expand <url>` / `.unshorten <url>` | Expand a shortened URL |
| `.privacy` / `.forgetme` / `.optout` / `.optin` | See, erase, or opt out of stored data (PM-only) |
| `.join` / `.part <#channel>` | Invite or remove the bot (founder or admin) |
| `.users [#channel]` | Show known users in a channel |

`.forgetme` is right-to-erasure: it purges the invoking nick from every dataset (saved location + per-channel tracking). `.optout` sets a persistent opt-out flag (`store.set_opt_out`, the source of truth) so the bot stops tracking that nick; `.optin` reverses it.

### Weather provider flags

Force a specific source instead of the dispatcher's choice. Flags work anywhere on the line, before or after the location. Forcing an inactive provider (no key) or one that lacks the requested capability aborts with a message - no silent fallback once you have made an explicit choice.

| Flag | Provider | Notes |
|------|---------|-------|
| `-nws` | NWS (Weather.gov) | US only - NDFD + HRRR + WaveWatch III |
| `-mm` / `-meteomatics` | Meteomatics | ECMWF/ICON/GFS blend (paid) |
| `-aw` / `-wk` / `-apple` / `-appleweather` / `-weatherkit` | Apple WeatherKit | NWS + IBM TWC blend |
| `-om` / `-openmeteo` | Open-Meteo | Free; ECMWF/ICON/GFS + CAMS AQ + ERA5 |
| `-vc` / `-visualcrossing` | Visual Crossing | ECMWF + ERA5 reanalysis |
| `-acc` / `-accuweather` | AccuWeather | Proprietary long-range |
| `-owm` / `-openweathermap` | OpenWeatherMap | GFS + ECMWF + CAMS AQ |
| `-wb` / `-weatherbit` | WeatherBit | GFS + station obs |
| `-wapi` / `-weatherapi` | WeatherAPI.com | GFS-derived |
| `-pw` / `-pirate` / `-pirateweather` | Pirate Weather | Dark Sky compatible; HRRR + MRMS US nowcast |
| `-sg` / `-stormglass` | Stormglass | Marine specialist (7-model wave blend) |
| `-tio` / `-tomorrow` / `-tomorrowio` | Tomorrow.io | Proprietary nowcasting |
| `-wwo` / `-worldweatheronline` | World Weather Online | Basic single-model |
| `-ws` / `-weatherstack` | Weatherstack | Basic; least preferred |
| `-l` | (list mode) | List active providers ranked by accuracy for that capability |

Specialist capabilities (`.aqi`, `.uv`, `.pollen`, `.alerts`, `.history`, `.wildfire`, `.space`, `.tides`) accept their own source flags (e.g. `.aqi -an`, `.uv -om`, `.tides -coops`). See `docs/providers.md`.

### Admin commands

Authenticate in PM first: `/MSG Internets AUTH <password>`. Auth is restricted to PM. Sessions are keyed by nick **and** hostmask; a hostmask change invalidates the session. Brute-force lockout after 5 failures (5-minute cooldown). Every privileged action is written to the HMAC-chained audit log.

| Command | Description |
|---------|-------------|
| `.auth <password>` | Authenticate (PM only) |
| `.deauth` | End admin session (PM only) |
| `.load <module>` | Load a module |
| `.unload <module>` | Unload a module |
| `.reload <module>` | Reload a module |
| `.reloadall` | Reload all loaded modules |
| `.restart` | Full process restart (`execv`; subprocess on Windows) |
| `.rehash` | Reload `config.ini`, re-read password hash, clear admin sessions |
| `.mode <+/-modes>` | Set bot user modes (e.g. `.mode +ix`) |
| `.snomask <+/-flags>` | Set server notice mask (e.g. `.snomask +cCkK`) |
| `.raw <line>` | Send a raw IRC line (capped at 510 bytes) |
| `.nick <newnick>` | Change the bot's nick |
| `.say <target> <text>` | Send a PRIVMSG as the bot |
| `.act <target> <text>` | Send a CTCP ACTION (/me) as the bot |
| `.loglevel [LEVEL]` / `.loglevel <logger> <LEVEL>` | Show or set global / per-subsystem log level |
| `.debug [on\|off]` / `.debug <subsystem> [off]` | Toggle global or per-subsystem debug |
| `.uptime` | Process and current-connection uptime |
| `.stats` | Runtime counters, send-queue depth, modules, RSS, audit-log size |
| `.audit [N\|grep <pat>\|tail\|verify]` | View the audit log; `verify` checks the HMAC chain |
| `.fingerprint <nick>` | Cross-reference hostmask, channels, last-seen, tells, notes, audit mentions, shadow-ban status |
| `.shadow-ban <nick> [reason]` | Silently drop all traffic (commands + on_raw) from a nick; audit-logged |
| `.shadow-unban <nick>` | Lift a shadow-ban |
| `.shadow-list` | List shadow-banned nicks |
| `.health` | Per-subsystem health snapshot |
| `.shutdown [reason]` / `.die [reason]` | Save state, unload modules, quit cleanly |

### Console commands

When stdin is a TTY (and `--no-console` is absent) the bot runs a console task. No auth required - it is local-operator-only. Type at the `>` prompt:

| Console | Equivalent |
|---------|-----------|
| `debug [on\|off]` / `debug <sub> [off]` | `.debug` |
| `loglevel [LEVEL]` / `loglevel <logger> LEVEL` | `.loglevel` |
| `status` | (none) - shows nick, channels, modules, admins, log state |
| `shutdown [reason]` / `quit` | `.shutdown` |
| `help` | shows console commands |

## Writing a module

Copy `modules/example.py` - a loadable, fully-commented skeleton - to `modules/<name>.py`, rename the class, set `COMMANDS`, and write the `cmd_*` coroutine(s). Add the name to `[bot] autoload` (or `.load` it at runtime, admin). Full guide in `docs/modules.md`.

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

`nick` is the sender, `reply_to` is the channel or nick to respond to, `arg` is everything after the command (or `None`). Handlers are coroutines; run blocking I/O under `await asyncio.to_thread(...)`. Reply via `self.bot.privmsg()` (public), `self.bot.notice()` (private), or `self.bot.reply()` / `self.bot.preply()` (auto-routed). For user-influenceable URLs use `modules/_netsafe.py` (SSRF guard). Outbound HTTP should go through `modules/base.fetch_json` (size-capped); never `r.json()` or unbounded `r.text`.

Available on `self.bot`: `cfg`, `loc_get/set/del(nick)`, `rate_limited(nick)`, `flood_limited(nick)`, `is_admin(nick)`, `channel_users(channel)`, `active_channels`, `send(raw, priority)`. Lifecycle hooks: `on_load()`, `on_unload()`, `on_raw(line)` (every incoming line after IRCv3 tag stripping). `is_configured()` gates whether the module appears in `.help` and whether its commands run; return False when a required key is absent so the module hides cleanly.

If two modules register the same command name, the second load is rejected with a conflict error.

## Adding a weather provider

Create a package in `weather_providers/` with one sub-module per API endpoint; the dispatcher auto-discovers capabilities from `async def get_*` method names. Register a factory in `weather_providers/__init__.py` that returns `None` when its key is absent, add the key under `[weather_providers]`, and add the name to `provider_priority`. Full walkthrough (including WeatherKit ES256 setup) in `docs/providers.md`.

## Security model overview

Full threat model in `docs/security-model.md`. Highlights:

- **Secret store.** Lookup order, first hit wins: `INTERNETS_<NAME>` env var, then `[secrets]` in `config.ini` (read only at exactly 0600, else fail-closed). Secrets are stored encrypted-at-rest, never hashed (the bot must send the literal value on the wire). The bot never logs a secret value; `sender.py` scrubs `PASS` / `NS IDENTIFY` / `OPER` / `AUTHENTICATE` before logging.
- **Audit log** (`audit_log.py`). Append-only, HMAC-SHA-256-chained over each record's `prev_hash` plus fields; the HMAC key is a 0600 sidecar (`audit.key`), so a leaked copy of `audit.log` cannot be used to forge or recompute entries. `.audit verify` walks the chain. Rotates at a size cap; each segment is independently verifiable. Honest limit: tail-truncation by an attacker holding both files is undetectable from the file alone (needs an external append-only sink).
- **SSRF guard** (`modules/_netsafe.py`). For user-influenceable URLs it resolves the host, rejects any private/loopback/link-local/metadata/ULA/IPv4-mapped answer, then pins the connection to the validated IP (thread-local DNS pin) so urllib3 cannot re-resolve to an internal address between check and connect; re-validates every redirect hop. SNI/TLS/Host stay correct.
- **Authentication.** scrypt (default) / bcrypt / argon2; constant-time compare via `hmac.compare_digest`; per-nick brute-force lockout; sessions bound to nick+hostmask and cleared on disconnect; auth state under a dedicated `threading.Lock`.
- **Transport.** TLS 1.3-only by default - the entire TLS 1.2 cipher surface is closed; TLS 1.2 is permitted only with `INTERNETS_ALLOW_TLS12=1`, never 1.0/1.1; cert verification on by default (`ssl_verify`).
- **Input validation.** Module names `^[a-z][a-z0-9_]*$`; channel-name regex; args capped 400 chars; PRIVMSG/NOTICE targets validated; module loader blocks symlink traversal; no `eval`/`exec` anywhere (calculator is an AST walker with a strict whitelist).
- **Resource limits.** 50 concurrent command tasks; sender queue bounded at 200; INVITE acceptance rate-limited; state files capped at 10 MB on load; per-nick API + flood limiters.
- **Information disclosure.** IRC error messages are generic ("see log for details"); no stack traces, paths, or internal state leak to the channel.
- **Single-instance safety.** `process_lock.py` (PID lock with stale-detection) refuses a second instance that would race the JSON state files.

## Testing

```
python tests/run_tests.py        # standalone, no dependencies
pytest tests/ -v                 # full suite (pip install -e ".[dev]")
```

**Both are required. They are disjoint suites, not two ways to run one.**
`tests/run_tests.py` is named `run_tests.py`, not `test_*.py`, so pytest's
default collection **does not pick it up** - running only `pytest` silently
skips its 204 checks, and running only `run_tests.py` skips the other 1669.
CI runs both as separate steps (`.github/workflows/tests.yml`), and so should
you before opening a PR.

`tests/run_tests.py` is a self-contained runner built on a `@test` decorator
with no third-party dependency, so it works on a bare checkout before
`pip install -e ".[dev]"`. It holds the completeness gates (for example the
`strip_ctrl` enumeration in `docs/security-model.md`) and the geocode/format
unit checks.

The pytest suite is 39 modules (`tests/test_*.py`) covering protocol parsing,
store CRUD/flush/pruning, calculator sandboxing and DoS guards, dice, weather
provider registry and capability dispatch, provider health scoring, gap-fill
merge semantics, NWS coverage handling, config parsing, output formatting, unit
conversion, sender injection prevention and line limits, password hashing, rate
limiting, the SSRF/netsafe guard, `fetch_json` size caps, secret store, and
per-module behavior (ipintel, scinews, secinfo, devtools, mathx, physcalc,
probe, dnsutils, pkginfo, reflookup, satpass, astro2, crypto-cache, stocks, and
more).

The coverage gate (`fail_under = 75` in `pyproject.toml`) is **core-only**:
`modules/*` and `weather_providers/*` are omitted from the measured source, so
the headline percentage measures the top-level orchestration modules, not the
whole repo. Do not read it as repo-wide coverage.

## Operational notes

Full ops detail in `docs/deployment.md`.

- **Reconnect.** Exponential backoff 15s/30s/60s/120s/240s (jittered +/-25%) capped at 5 min; counter resets on success. Channels restored from `channels.json`; invite-only channels trigger a ChanServ re-invite; channels rejecting with 471/474/475 are logged and dropped from the saved list.
- **Keepalive.** `PING` every 90s; a 300s read timeout with no data presumes the link dead and hands off to reconnect, and a companion 240s pong-timeout force-reconnects when the server stops answering client PINGs.
- **User tracking.** Per-channel nicks/hostmasks/first-last-seen in memory, flushed to `users.json` every 30s, pruned past `user_max_age_days` (default 90). Populated from observed JOIN/PART/QUIT/NICK and channel activity; `353` NAMES is **not** used for the general roster, so a user already present when the bot joined will not appear in `.users` until they trigger an observable event.
- **Persistence.** All JSON loads into memory once at startup; a background thread flushes dirty datasets every 30s, each under its own lock. Worst-case hard-crash loss is ~30s of user-tracking timestamps; location/channel changes also flush on `.shutdown`, `.restart`, and signal handlers. `os.replace()` is atomic on POSIX, best-effort on NTFS.
- **Metrics.** `metrics.py` is an opt-in Prometheus text exporter. Counters are always collected (cheap); the HTTP exporter starts only when `[metrics] enable = true`. It binds 127.0.0.1 by default and **rejects 0.0.0.0** - expose off-host only behind an authenticating reverse proxy.
- **Nick collision.** If the configured nick is taken, the bot appends `_` and retries.

## Known limitations

- The translation module uses an undocumented Google Translate endpoint (`translate.googleapis.com`): no SLA, may break or rate-limit without notice.
- `.qdb` depends on a working QDB-compatible XML endpoint (`[qdb] api_url`); qdb.us is defunct, so `.qdb` is hidden until one is configured.
- The roster is event-driven, not NAMES-derived (see User tracking above).
- The audit log detects edit/reorder/delete of any non-tail record but cannot detect tail-truncation by an attacker holding both `audit.log` and `audit.key` from the file alone.
- Coverage's reported percentage is core-only by design; do not read it as repo-wide.

## License

ISC - see [LICENSE.md](LICENSE.md).
