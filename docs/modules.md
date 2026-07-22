# Modules

Reference for the Internets module system and every command module on disk.
Grounded in `modules/base.py`, the loader in `internets.py`, and each
`modules/*.py`. Cross-references `docs/architecture.md` for the event loop,
dispatch, and reload internals.

---

## Part 1 - The module system

### What a module is

A module is a single `.py` file in `modules/` that exposes a top-level
`setup(bot) -> BotModule` factory. The bot loads it by file path, calls
`setup(self)`, and registers the returned instance's commands. There is no
class auto-discovery: `setup()` is the only required entry point
(`internets.py:479`). A file with no `setup` is rejected at load
(`internets.py:480`).

```python
from __future__ import annotations
from .base import BotModule

class PingModule(BotModule):
    COMMANDS: dict[str, str] = {"ping": "cmd_ping"}
    async def cmd_ping(self, nick, reply_to, arg):
        self.bot.privmsg(reply_to, f"{nick}: pong")

def setup(bot):
    return PingModule(bot)
```

That is the bare minimum. `modules/example.py` is a complete, loadable
copy-and-fill skeleton that also shows the conventions a real command needs:
rate limiting, empty-arg usage, `strip_ctrl` on output, the off-loop network
`_fetch_sync` shape (with error handling and the SSRF caveat), `on_load` /
`is_configured`, `help_lines`, and the `forget` hook. Start from it, not this
snippet.

### `BotModule` base class (`modules/base.py:196`)

Every module subclasses `BotModule`. Surface:

- **`COMMANDS: dict[str, str]`** - maps a command word (no prefix) to a
  *method-name string*. Multiple words can map to the same method (aliases):
  `{"bofh": "cmd_bofh", "excuse": "cmd_bofh"}`.
- **`__init_subclass__` contract check (`base.py:220`)** - at class-definition
  time, every value in `COMMANDS` must name a real method that is
  `async def`. A typo or a sync handler raises `TypeError` when the file is
  imported (i.e. at `.load`/startup), not at first invocation. `inspect.
  iscoroutinefunction` is used deliberately (the `asyncio` alias is deprecated
  for removal in 3.16).
- **`__init__(self, bot)`** - stores `self.bot`. The bot object is the only
  injected dependency; everything a module needs hangs off it (see "What
  `self.bot` exposes").
- **Command handler signature** - `async def cmd_x(self, nick, reply_to,
  arg)`. `nick` is the sender, `reply_to` is the channel or nick to answer,
  `arg` is everything after the command word or `None` if absent. Handlers
  return nothing; they talk back through `self.bot`.
- **`help_lines(self, prefix) -> list[str]`** - lines for `.help <module>`.
  Override and build each line with `help_row()` (below) so columns align.
  Default returns `[]`.
- **`is_configured(self) -> bool`** - default `True`. Override to return
  `False` when a required key/endpoint is missing. Effects: `.help` hides the
  module's commands, and (for weather providers) the `-flag` is hidden from
  `.w -l`. Dispatch still works - an admin can `.load` the module and add the
  key later; it is only hidden, not disabled (`base.py:251`).
- **Lifecycle hooks** - `on_load()` after registration, `on_unload()` before
  removal, `on_raw(line)` for every inbound IRC line (must be fast and sync;
  called after IRCv3 tag stripping). All default to no-op.
- **`forget(self, nick) -> int`** - right-to-erasure hook. Default returns 0
  (`base.py:276`). Only four command modules override it to delete that nick's
  records and return the count: `seen` (`seen.py:122`), `tell` (`tell.py:209`),
  `notes` (`notes.py:86`), `remind` (`remind.py:173`). `.forgetme` calls it on
  every loaded module. Saved locations are NOT erased via a module `forget()`:
  `location.py` defines none; `privacy.forgetme` wipes them by calling the core
  `bot.loc_del(nick)` directly (`privacy.py:132`). `steam.py` likewise defines
  no `forget()`, so its persisted nick->SteamID mapping survives `.forgetme`
  (privacy gap, see the steam.py row in Part 2).

### Discovery and autoload

There is no directory scan at startup. The bot loads exactly the modules named
in `config.ini [bot] autoload` (comma-separated), in list order
(`config.py:105`, `internets.py:518`). `modules_dir` (default `modules`)
sets the directory (`config.py:104`). `.modules` lists loaded modules plus any
other `*.py` on disk that could be loaded.

### Load path and guards (`internets.py:462`)

`load_module(name)` holds `self._mod_lock` and:

1. Rejects names not matching `^[a-z][a-z0-9_]*$`.
2. Rejects if already loaded.
3. Requires `modules/<name>.py` to exist.
4. **Symlink/traversal guard**: `path.resolve().relative_to(MODULES_DIR.
   resolve())` - a file whose real path escapes `modules/` is blocked
   (`internets.py:472`).
5. `importlib.util.spec_from_file_location("modules.<name>", path)` +
   `exec_module` - the file is executed fresh on every load.
6. Requires `setup`, calls `inst = mod.setup(self)`.
7. **Conflict check**: any command word already owned by a *different* module
   aborts the load with a conflict message - first loader wins, second is
   rejected (`internets.py:482`). (A reload of the same module is fine because
   `unload` removes its words first.)
8. `inst.on_load()`, then registers each `COMMANDS` word into
   `self._commands[word] = (name, method)`.

Failures are logged and returned as a generic "see log for details" string -
no traceback reaches IRC.

### Hot-reload semantics and the helper-caching caveat

`reload_module` is `unload` then `load` (`internets.py:514`). Because step 5
re-executes the target file from source, edits to the command file itself take
effect on `.reload <module>`. **But** modules import shared helpers
(`from .base import ...`, `from .geocode import ...`, `from ._netsafe import
...`, `weather_providers`), and those helper modules stay cached in
`sys.modules`. Reloading a command module does NOT re-execute its imported
helpers. Editing `modules/base.py`, `modules/geocode.py`, `modules/units.py`,
`modules/_netsafe.py`, or a `weather_providers/*` file and then `.reload`-ing a
command module picks up nothing from the helper change. Use `.restart` (full
`execv`) for helper-level edits. See `docs/architecture.md` for the full reload
model. `.reloadall` re-runs load for every currently loaded command module but
still does not touch cached helpers.

### Shared helpers (`modules/base.py`)

Imported piecemeal by modules; these are the load-bearing ones:

- **`fetch_json(url, *, ua, params, headers, timeout=10, max_bytes=256KiB,
  allow_404=False)` (`base.py:27`)** - the standard outbound JSON call. Streams
  the body, reads at most `max_bytes + 1` raw bytes, and raises
  `ResponseTooLarge` *before decode/parse* if the cap is exceeded. This is the
  JSON-bomb / OOM guard; modules with legitimately large payloads pass an
  explicit `max_bytes=` (e.g. `steam`, `geocode`, `ipintel`). (`poke` ~1 MB and
  `numberfact`'s OnThisDay feed ~4 MB cap the same way but *inline*, streaming
  against their own `_MAX_BODY_BYTES` rather than through `fetch_json`.) The
  `requests` import is lazy. `with requests.get(..., stream=True)` guarantees
  the socket is released on every exit path. `allow_404=True` returns `None` on
  404 for lookup-or-miss semantics (dictionary word) instead of
  raising. Never use bare `requests.get(...).json()` in a module.
- **`resolve_public(host, port=0) -> list` (`base.py:73`)** - anti-SSRF DNS
  check. Returns `getaddrinfo` results; raises `ValueError` if the host is
  empty/oversized/unresolvable or if ANY resolved address is private /
  loopback / link-local / multicast / reserved / unspecified. Resolve-time only
  (TOCTOU caveat in the docstring); callers that then connect should connect to
  a returned IP, not re-resolve. Used by the `probe` module's connect paths.
- **`cred(cfg, secret_name, section, key, default="") -> str` (`base.py:117`)**
  - pulls a credential or PII field: `secret_store.get(secret_name)` first,
  then `config.ini[section][key]` fallback (for pre-2.5 upgrades). Template
  placeholders (`you@example.com`, `changeme`, `set-in-secret-store`, …) are
  treated as unset so they never leak into outbound requests. Nearly every
  network module reads its contact User-Agent via
  `cred(cfg, "weather_user_agent", "weather", "user_agent")` and its API key
  the same way.
- **`strip_ctrl(s, max_len=400) -> str` (`base.py:177`)** - the single
  sanitizer for any upstream-derived text spliced into an IRC line. Strips the
  full C0 range `\x00-\x1f` plus `\x7f` (so bold `\x02`, color `\x03`, reverse,
  ESC, BEL cannot be injected as bot-attributed formatting) and caps length.
  The sender only strips `\r\n\x00` as a transport backstop, so this is the
  real injection defense - route every API title, header value, or user echo
  through it.
- **`help_row(prefix, usage, desc, *, width=24) -> str` (`base.py:157`)** -
  formats one `.help` line with the usage column padded to `width` so
  descriptions align in monospace clients. `usage` is the command + args
  *without* the prefix; write aliases as `cmd/.alias`.
- **`ResponseTooLarge` (`base.py:18`)** - raised by `fetch_json` on cap
  breach.

### The `_netsafe` import (`modules/_netsafe.py`)

Used only by modules that fetch a **user-influenceable URL/host** with
`requests`: `probe` (`.headers`/`.down`), `scinews` (article reader), `urls`
(`.shorten`/`.expand`), `ipintel`. Provides `safe_open` / `SSRFBlocked` /
`url_is_safe` / `resolve_safe_ip`. The guard resolves the host and rejects if
ANY answer is private/loopback/link-local/metadata/ULA/IPv4-mapped (rebinding
all-answers check), then **pins DNS for the calling thread** so urllib3 cannot
re-resolve to a different internal address between check and connect
(DNS-TOCTOU defense), re-validating on every redirect hop. Thread-local pinning
is used (not an IP-literal adapter) because under requests 2.34 / urllib3 2.7
the `server_hostname` override breaks TLS SNI; pinning `getaddrinfo` keeps the
hostname for SNI/Host/verification while forcing the validated IP. The global
wrapper is a no-op unless the current thread set a pin. `base.resolve_public`
is the lighter check for modules that resolve-then-connect-by-IP themselves
(probe's `.ssl`/`.tcp`); `_netsafe` is for the let-requests-drive-the-fetch
paths.

### What `self.bot` exposes (for handler authors)

`cfg` (ConfigParser), `privmsg()`, `notice()`, `reply()` / `preply()`
(routing split: channel vs NOTICE-to-user), `loc_get/loc_set/loc_del(nick)`,
`rate_limited(nick)`, `flood_limited(nick)`, `is_admin(nick)`,
`channel_users(channel)`, `active_channels`, `send(raw, priority)`. Blocking
I/O (the `requests` calls inside `_fetch_sync` helpers, hashing) must run under
`await asyncio.to_thread(...)` so the single event loop is never blocked.

---

## Part 2 - Command module reference

Every non-dunder `.py` in `modules/`, except `example.py` (the shipped
copy-and-fill skeleton, not autoloaded - see Part 1). Command words are the
live `COMMANDS` keys (aliases shown with `/`). "Needs" column: **local** = no network, no key;
**UA** = network, reads the contact User-Agent but needs no API key;
**key** = `is_configured()` returns `False` without the named credential, so
`.help` hides it; **opt-key** = works without, a key adds capability.

`modules/base.py`, `modules/_netsafe.py`, `modules/geocode.py`, and
`modules/units.py` are **helper modules, not command modules** - they define no
`COMMANDS` and no `setup()`, cannot be autoloaded, and are imported by the
command modules below. `geocode` (Nominatim/Zippopotam location resolution) is
imported by `weather.py`, `location.py`, and `health.py`. `units` (dual-unit
temp/wind/pressure/distance formatting) is imported by `weather.py` only.

### Weather and geo

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.weather`/`.w` `.forecast`/`.f` `.hourly`/`.h` `.nowcast`/`.nc` `.alerts`/`.al` `.aqi`/`.air` `.uv`/`.uvi` `.pollen`/`.allergy` `.astro`/`.sun` `.marine`/`.sea` `.history`/`.hist` `.wildfire`/`.fire` `.space`/`.aurora` `.tides`/`.tide` `.providers` | Capability-based weather dispatcher front-end; resolves location, calls `weather_providers`, formats normalized dataclasses. `.providers` is admin-only health/capability dump. Accepts city/zip/`lat,lon`/`-n nick` and per-provider `-flag`s. | UA + provider keys (core chain keyless - nws + openmeteo cover most capabilities - but ~20 of 32 providers are key-gated and register only when their credential is present) | weather.py |
| `.regloc` (`.register_location`) `.myloc` `.delloc` | Save / show / delete the caller's default location (stored in the core per-nick loc store via `bot.loc_set/loc_get/loc_del`, `location.py:48/54/65`; resolved via geocode). No `forget()` override; `.forgetme` wipes the location through `privacy.forgetme` -> `loc_del`. | local store | location.py |

Behaviour worth knowing before you touch this module (full detail in
`docs/providers.md`):

- **`.alerts` widens to a whole state.** A query that is *only* a US state name
  or USPS code (`.al mississippi`, `.al ms`) queries NWS with `area=XX` instead
  of the geocoded point, because a state resolves to one inland coordinate and a
  point lookup returns only alerts covering that exact spot. Naming a place
  inside the state (`.al jackson mississippi`) stays a point lookup. Alerts are
  deduplicated by `(event, headline)` (NWS issues one per forecast zone), sorted
  most-severe first, capped at 5, and anything beyond the cap is reported as
  `... and N more` rather than dropped.
- **Feels-like is always shown when known**, even when it nearly matches the
  temperature. It is never gap-filled from another provider - see the derived
  field invariant in `docs/providers.md` section 4.5.
- **`.wildfire` reports `(N sized)`** alongside the incident count, because
  NIFC's current-incident layer is mostly small dispatch records carrying no
  size at all. Acreage comes from `IncidentSize` (current), never
  `DiscoveryAcres` (size at initial report, a 0.01 default on nearly every
  record).
- **A location NWS does not cover is not a provider failure.** Non-US points
  return `None` from every NWS endpoint so the dispatcher falls through without
  penalising NWS's circuit breaker.
- **Geocoding runs a settlement-constrained search alongside the free-text
  one** and keeps the more prominent result, so a business no longer outranks
  the place it was named after (`new york new york` resolved to the Las Vegas
  casino before this).

### Network, DNS, lookup

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.ipinfo` | IP/hostname geolocation via ip-api.com. | UA | ipinfo.py |
| `.dns` `.rdns` `.caa` `.whois` `.asn` | DNS over HTTPS (A/AAAA/MX/TXT/NS/CNAME), reverse PTR, CAA+SPF+DMARC, RDAP domain whois, RDAP network/ASN. Keyless. | UA | dnsutils.py |
| `.headers` `.ssl` `.tcp` `.down` | Network probers against a user-supplied host. `.headers`/`.down` fetch via `_netsafe.safe_open` (SSRF-guarded); `.ssl`/`.tcp` resolve via `base.resolve_public` then connect to the validated IP. | UA + SSRF guard | probe.py |
| `.cidr` `.subnet` `.port` | Subnet math (network/broadcast/mask/hosts/range), block splitting, port↔service name. Pure stdlib. | local | netcalc.py |
| `.shorten` `.expand`/`.unshorten` | is.gd URL shortener / expander. User URL validated through the SSRF guard before is.gd sees it. | UA + SSRF guard | urls.py |

### Threat-intel and security

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.ip`/`.rep` | IP-reputation aggregator. **Queries** DNSBLs, DShield, GreyNoise, Tor exit list, and (if a key is set) AbuseIPDB, then summarizes. Keyless-usable; the AbuseIPDB key only enriches. Read-only - it consumes reputation, it does not feed any pipeline. | opt-key `abuseipdb_key` | ipintel.py |
| `.cve` `.pwn` `.hashid` `.cvss` `.cipher` | NVD CVE lookup (CVSS/summary/date), HIBP password breach count (`.pwn`, PM-only, k-anonymity), hash-type identification, CVSS v3.1 base-score compute, cipher reference table. `.cve`/`.pwn` are network; the rest are local. | UA (cve/pwn) | secinfo.py |

### Developer, encoding, math utilities (all local, no network, no key)

| Command(s) | Does | File |
|---|---|---|
| `.cc` | Calculator - AST-walker evaluator with implicit multiplication, math functions; no `eval`. | calc.py |
| `.isprime` `.factor` `.gcd` `.base` `.stats` `.roman` `.pct` `.bignum` `.const` | Math toolbox: primality+next-prime, factorization, GCD/LCM, base 2..36 convert, descriptive stats, Roman↔Arabic, percentages, exact bignum (`n!`/`fib`/`2^n`), physical constants. | mathx.py |
| `.ly` `.sr` `.escape` `.ohm` `.rc` `.baud` | Physics/EE calculators: light-time, special-relativity gamma, escape velocity+surface gravity, Ohm/power solver, resistor color code, serial transfer time. | physcalc.py |
| `.unicode` `.hash` `.crc` `.b32` `.slug` `.ulid` `.ascii` `.ds` `.defang` `.entropy` `.pw` `.lorem` | Encoding/generator toolbox: codepoint info, md5/sha/blake2b digests, CRC32/Adler-32, base32, slugify, ULID, ASCII table, data-size convert, URL/IP/email defang, password entropy estimate, random password/passphrase, lorem ipsum. | encode.py |
| `.jwt` `.semver` `.uuid5` `.tz` `.unix` `.color` `.cron` | Dev tools: decode JWT (no sig check), semver compare, deterministic UUIDv5, timezone clock convert, Unix signal/errno lookup, color hex/rgb/hsl convert, cron validate/explain+next fires. | devtools.py |
| `.b64` `.unb64` `.hex` `.morse` `.uuid` `.epoch` | Text codecs: base64 enc/dec, hex auto, morse auto, random UUID4, epoch↔ISO. | devutils.py |
| `.http` | HTTP status-code lookup (local table). | httpcode.py |
| `.qr` | QR-code image URL builder - emits a link only, no fetch. | qr.py |

### Reference, language, search

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.dict`/`.dictionary` | English definition via Free Dictionary API (`allow_404` = clean miss). | UA | dictionary.py |
| `.u`/`.urbandictionary` | Urban Dictionary lookup with `/N` result paging. | UA | urbandictionary.py |
| `.wiki` `.doi` `.isbn` `.so` `.rfc` `.rtfm` `.arxiv` `.element` | Reference lookups: Wikipedia summary, Crossref DOI, Open Library ISBN, top Stack Overflow Q, RFC metadata, `.rtfm` alias, arXiv paper, offline periodic-table element. | UA (`.element` local) | reflookup.py |
| `.t`/`.translate` | Translate via the undocumented Google Translate endpoint (no key, no SLA). | network | translate.py |
| `.sw`/`.g` `.si`/`.gi` | Web search (DuckDuckGo, keyless; Brave upgrade if `brave_key` set) and image search (`.si`/`.gi`, **requires** `brave_key`, else returns an error string). | opt-key `brave_key` | search.py |
| `.sci` | STEM + infosec news/journal/paper aggregator over curated keyless RSS/Atom feeds, with `.sci read <N>` / `.sci sources`. Article reader fetches through the SSRF guard. | UA + SSRF guard | scinews.py |
| `.gh` | GitHub repo info via the public unauthenticated REST API: stars / forks / open issues / language / license / last push. Keyless (60 req/hr); reads its UA via `cred`. `COMMANDS={'gh':'cmd_gh'}` (`ghinfo.py:70`). | UA | ghinfo.py |
| `.pypi` `.npm` `.crates` | Keyless package-registry lookups: PyPI (version/summary/license/date), npm (version/description/license/last publish), crates.io (max version/downloads/license/docs). Reads UA via `cred`; `is_configured` always True (`pkginfo.py:211`). | UA | pkginfo.py |

### Science and space

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.solar` `.neo` `.launches` `.moon` `.sky` | NOAA space-weather (X-ray flare class + SSN), NASA NeoWs near-earth objects, next rocket launches, moon phase, Messier-catalog lookup. NASA endpoints use `DEMO_KEY` by default (overridable via `nasa_api_key`); `is_configured` always True. | UA (DEMO_KEY) | astro2.py |
| `.apod` | NASA Astronomy Picture of the Day; `DEMO_KEY` default, `nasa_api_key` override, rate-limit message when throttled. | UA (DEMO_KEY) | apod.py |
| `.iss` | ISS position + current crew (open-notify.org). | UA | iss.py |
| `.spacex` | Next SpaceX launch via Launch Library 2 (thespacedevs). | UA | spacex.py |
| `.passes` | Next visible satellite pass via N2YO. **Hidden without** `n2yo_api_key`. | key `n2yo_api_key` | satpass.py |
| `.numberfact`/`.nf` | Number trivia (local math + Wikipedia REST for trivia/date/year); large-payload fetch caps at ~4 MB. | UA | numberfact.py |

### Media, entertainment, finance

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.imdb` | Movie/TV lookup via OMDb. **Hidden without** `omdb_key`. | key `omdb_key` | imdb.py |
| `.lastfm` | Last.fm profile + now-playing. **Hidden without** `lastfm_key`. | key `lastfm_key` | lastfm.py |
| `.yt`/`.youtube` | YouTube search with stats. **Hidden without** `youtube_key`. | key `youtube_key` | youtube.py |
| `.steam` `.regsteam`/`.register_steam` | Steam status/games + nick→SteamID registration. **Hidden without** `steam_key`. Persists nick→SteamID in its own JSON file (`steam.py:166-171`, default `steamids.json`). No `forget()` override, so this per-nick mapping is NOT erased by `.forgetme` (privacy gap: either add a `forget()` override to steam.py or have privacy purge the file). | key `steam_key` | steam.py |
| `.tw`/`.twitch` | Twitch stream/channel/game via Helix. **Hidden without** `twitch_client_id`+`twitch_client_secret`. | key (id+secret) | twitch.py |
| `.irpg`/`.idlerpg` | IdleRPG player lookup over the configurable XML endpoint (default Rizon `idlerpg.rizon.net/xml.php`). | UA + endpoint | idlerpg.py |
| `.qdb` | Quote-DB lookup. Default endpoint baked in (bash-org-archive.com); `[qdb] api_url` overrides. | UA + endpoint | qdb.py |
| `.mtg` | Magic: the Gathering card via Scryfall. | UA | mtg.py |
| `.poke`/`.pokemon` | PokéAPI lookup (inline 404 miss; ~1 MB inline cap). | UA | poke.py |
| `.dnd` | D&D 5e SRD spell/monster via dnd5eapi.co. | UA | dnd.py |
| `.recipe`/`.meal` | TheMealDB recipe lookup. | UA | recipe.py |
| `.cocktail`/`.drink` | TheCocktailDB recipe lookup. | UA | cocktail.py |
| `.hn` | Top Hacker News story (Firebase HN API). | UA | hn.py |
| `.reddit`/`.r` | Top post from a subreddit (old.reddit.com JSON). | UA | reddit.py |
| `.xkcd` | xkcd comic (random or by number), official JSON. | UA | xkcd.py |
| `.gecko`/`.coingecko`/`.cg` | Crypto price via CoinGecko's free public API. | UA | crypto.py |
| `.stock`/`.s` `.crypto` | Stock/crypto quote with multi-provider failover (Finnhub/Alpha Vantage/Twelve Data). **Hidden until at least one** of `finnhub_key`/`alphavantage_key`/`twelvedata_key` is set. | key (≥1) | stocks.py |
| `.fx` | FX conversion via frankfurter.dev (ECB rates), keyless. | UA | fx.py |

### Fun and games

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.d` | Dice roller, `XdN+M` notation. | local | dice.py |
| `.coin` `.8ball` `.rps` `.choose` | Coin flip, magic 8-ball, rock-paper-scissors, pick-one. | local | games.py |
| `.bofh`/`.excuse` | Random BOFH excuse from a local list. | local | bofh.py |
| `.fml` | Random FMyLife quote (scraped). | UA | fml.py |
| `.advice` | Random advice slip (adviceslip.com). | UA | advice.py |
| `.bored` | Random activity suggestion (Bored API). | UA | bored.py |
| `.fact` | Random useless fact (uselessfacts.jsph.pl). | UA | fact.py |
| `.catfact`/`.cat` | Random cat fact (catfact.ninja). | UA | catfact.py |
| `.chuck` | Random Chuck Norris joke (chucknorris.io). | UA | chuck.py |
| `.dadjoke`/`.joke` | Random dad joke (icanhazdadjoke.com). | UA | dadjoke.py |
| `.cowsay` | Render the ASCII cow speaking the given text (local). | local | cowsay.py |

### Personal and social (per-nick persistent state)

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.remind` `.remind-list` `.remind-cancel` | Schedule/list/cancel per-user reminders delivered in-channel. Overrides `forget()`. | local store | remind.py |
| `.tell` `.tell-cancel` `.tell-list` | Leave an offline message delivered on the target's next PRIVMSG; list/cancel pending. Overrides `forget()`. | local store | tell.py |
| `.notes` | Per-nick sticky notes (`list`/`add`/`del`/`show`/`clear`). Overrides `forget()`. | local store | notes.py |
| `.seen` | When a nick was last seen and doing what (driven by `on_raw`). Overrides `forget()`. | local store | seen.py |

### Admin, channel, introspection, privacy

| Command(s) | Does | Needs | File |
|---|---|---|---|
| `.join` `.part` `.users` | Founder/admin-gated join/part (async NickServ-account vs ChanServ-founder verification); `.users` lists tracked nicks in a channel. | local | channels.py |
| `.health` `.uptime` | Operator introspection - bot health snapshot and uptime. | local | health.py |
| `.forgetme` `.privacy` `.optout` `.optin` | Privacy controls (PM-only): erase all stored data (fans `forget()` across every loaded module), show what's stored, opt out/in of tracking. | local | privacy.py |

### Helper modules (not loadable, no commands)

| File | Role |
|---|---|
| base.py | `BotModule` base class + shared helpers (`fetch_json`, `resolve_public`, `cred`, `strip_ctrl`, `help_row`, `ResponseTooLarge`). |
| _netsafe.py | SSRF-safe fetch with DNS-TOCTOU thread-local pinning (`safe_open`, `SSRFBlocked`, `url_is_safe`, `resolve_safe_ip`). |
| geocode.py | Location resolution (Nominatim / Zippopotam) for weather/location. |
| units.py | Dual-unit temperature/wind/pressure/distance formatting for weather output. |
