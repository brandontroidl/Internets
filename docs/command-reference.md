# Command Reference

Every command the bot can dispatch: 165 primary module commands across 70 modules,
4 public core commands, and 23 admin core commands, as of 2026-08-15.

The inventory below is **generated**, not hand-listed. `scripts/gen-command-reference.py`
walks `modules/`, instantiates each `BotModule` subclass without running `__init__`,
and reports the real `COMMANDS` registrations plus
`admin_cmds.py - AdminCommandsMixin._CORE`:

```console
$ python scripts/gen-command-reference.py
...
Primary module commands: 165. Core public: 4. Core admin: 23.
```

Drift is gated. The check mode exits non-zero if any registered command name is
missing from this file:

```console
$ python scripts/gen-command-reference.py --check docs/command-reference.md
docs/command-reference.md: all registered commands present
```

Run it from the repository root after adding or renaming a command. Syntax and
descriptions here come from each module's own `help_lines()` output - the same text
`.help <module>` prints - so this file and the in-channel help have one source.

---

## Dispatch rules

These apply to every command in this document. Implementation:
`internets.py - IRCBot._handle_privmsg()` and `IRCBot._dispatch()`; see
[IRC Protocol](irc-protocol.md#11-privmsg-notice-and-ctcp) for the parsing that
precedes them.

### Prefix

The prefix is `[bot] command_prefix` from `config.ini`, `.` by default. It is read
from the live config at use time (`IRCBot._cmd_prefix()`), so a change plus
`.rehash` or SIGHUP takes effect without a restart. An empty prefix is rejected at
startup, since it would make every message a command.

**In a channel** the prefix is mandatory. **In a PM it is optional**: a private
message whose bare first word matches a known command dispatches that command. So
`/msg bot help` and `/msg bot .help` are equivalent, while `help` typed in a channel
is ordinary chat.

Commands come from `PRIVMSG` only. `NOTICE` never dispatches, and any message
beginning with `\x01` (CTCP) is dropped before command parsing.

### Gate chain

Checks run in this fixed order; the first that fires stops the command.

| Order | Gate | Behavior when it fires |
|---|---|---|
| 1 | Shadow-ban on the sender | silent drop - no reply, no rate-limit consumption, no audit entry |
| 2 | `.auth` / `.deauth` outside a PM | refusal message, no dispatch |
| 3 | Per-nick flood window | notice to the sender; admins bypass |
| 4 | Per-channel burst window | silent drop, logged only |
| 5 | Argument length over 400 characters | notice to the sender |
| 6 | Concurrent command tasks at 50 | "bot is busy" notice |
| 7 | Handler lookup (`_CORE` first, then modules) | unknown command is ignored silently |

The shadow-ban drop is deliberately indistinguishable from the bot being offline.
The channel burst gate is silent because a throttle notice would itself add to the
flood.

### Rate limits

Three independent windows in `store.py - RateLimiter`, all floored so a zero or
negative configured value cannot disable the gate:

| Window | Default | Scope | Applied by |
|---|---|---|---|
| Flood | 3 s (`[bot] flood_cooldown`) | per nick, all commands | the dispatcher, before the handler |
| API | 10 s (`[bot] api_cooldown`) | per nick, network commands | each module, inside its handler |
| Channel burst | 20 commands / 10 s | per channel, across all nicks | the dispatcher |

The distinction matters: the **flood** window is enforced for you by the dispatcher
and admins bypass it. The **API** window is voluntary - a module calls
`bot.rate_limited(nick)` before spending an upstream request, and it has no admin
bypass. Modules that skip the call are noted in the findings ledger; do not assume
every network command is API-gated.

### Admin gating

`.auth` binds an admin session to the nick **and** the hostmask observed at auth
time. `is_admin()` re-checks that binding on every call and revokes the session when
the hostmask changes, when the nick changes, on QUIT, on disconnect, and on rehash.
Sessions never survive a reconnect. Brute force is bounded at 5 failures per 300 s.

`.auth` and `.deauth` must be sent in a PM; typing a password into a channel is
refused rather than processed. Admin command handlers call `_require_admin()`
themselves, so an unauthenticated invocation gets "auth first", not silence.

### Task and time caps

At most 50 command tasks run concurrently (`_MAX_TASKS`); each is bounded at 60 s
(`_CMD_TIMEOUT`). A handler that exceeds the timeout is cancelled, counted, and the
user is told the command timed out. Any other exception is logged with a traceback
and reported as a generic internal error - handler exception text never reaches IRC.

### Reply routing

`reply()` sends to the channel for a normal channel command, back to the nick for a
PM, and as a **notice to the nick** for privileged output invoked in a channel, so
admin output does not leak into the channel. Message bodies are chunked at 400 bytes
on UTF-8 boundaries and the wire line is hard-capped at 512 bytes.

### Conventions in this document

- `<required>`, `[optional]`, `a|b` alternatives.
- Aliases follow the primary name in parentheses.
- **Public** unless marked **admin** or **PM only**.
- "Key: none" means the module works with no credential configured. A module whose
  key is missing is hidden from `.help` for non-admins but remains dispatchable.

---

## Core commands

Registered in `admin_cmds.py - AdminCommandsMixin._CORE` and resolved by the
dispatcher **before** the module registry, so a core name always wins a collision.

### Public (4)

- **`.help [module|command|all|admin]`** - progressive help. Bare `.help` lists
  modules by group; `.help weather` lists that module's commands; `.help aqi` shows
  one command; `.help all` prints the full grid. e.g. `.help dns`
- **`.modules`** - loaded modules with per-module command counts, plus the modules
  present on disk but not loaded. e.g. `.modules`
- **`.version`** - version string and repository URL. e.g. `.version`
- **`.auth <password>`** - **PM only.** Authenticate as bot admin. Binds the session
  to your nick and current hostmask; 5 failures in 300 s locks you out.
  e.g. `/msg bot auth hunter2`

### Admin (23)

All require an authenticated admin session. Actions marked audit-logged append a
tamper-evident record via `audit_log.py`.

Session and process:

- **`.deauth`** - end the current admin session. e.g. `.deauth`
- **`.restart`** - re-exec the bot process, preserving argv. Audit-logged.
  e.g. `.restart`
- **`.shutdown [reason]`** (`.die`) - clean shutdown: QUIT, drain, flush, exit.
  Audit-logged. e.g. `.shutdown maintenance`
- **`.rehash`** - re-read `config.ini` plus the local overlay, re-apply the log
  level, and clear all admin sessions. Does **not** reload credentials already
  captured at import time. Audit-logged. e.g. `.rehash`

Modules:

- **`.load <module>`** - load a module by name from `modules/`. Audit-logged.
  e.g. `.load weather`
- **`.unload <module>`** - unload a module and deregister its commands.
  Audit-logged. e.g. `.unload weather`
- **`.reload <module>`** - unload then load, picking up source edits from disk.
  Audit-logged. e.g. `.reload weather`
- **`.reloadall`** - reload every loaded module, reporting per-module success.
  Audit-logged. e.g. `.reloadall`

IRC control:

- **`.nick <newnick>`** - request a nick change; validated against RFC 2812 nick
  syntax. The local nick updates only on the server's confirmation. Audit-logged.
  e.g. `.nick Internets_`
- **`.mode <+/-modes>`** - set user modes on the bot itself. Not a channel MODE
  command. Audit-logged. e.g. `.mode +iw`
- **`.snomask <+/-flags>`** - set the server-notice mask (`MODE <nick> +s <mask>`).
  Audit-logged. e.g. `.snomask +cFk`
- **`.raw <IRC line>`** - inject a raw protocol line. CR/LF/NUL are rejected and the
  line is capped at 510 bytes. The echo, the log, and the audit record are
  credential-redacted; the wire gets the real line. Audit-logged.
  e.g. `.raw WHOIS alice`
- **`.say [target] <text>`** - speak as the bot; target defaults to the current
  channel. Audit-logged. e.g. `.say #ops deploy finished`
- **`.act [target] <text>`** - CTCP ACTION as the bot. Audit-logged.
  e.g. `.act waves`

Diagnostics:

- **`.uptime`** - process uptime and current-connection uptime. Note this core
  command shadows the public `.uptime` in the `health` module, because `_CORE` is
  resolved first. e.g. `.uptime`
- **`.stats`** - counters, send-queue depth, module counts, channel count, audit
  record count, RSS. e.g. `.stats`
- **`.audit [N|tail|grep <pattern>|verify]`** - read the audit log; default last 10,
  `verify` checks the hash chain. e.g. `.audit grep shadow-ban`
- **`.fingerprint <nick>`** - cross-reference one nick: hostmask, tracked channels,
  shadow-ban status, `.seen` record, pending tells, note count, audit mentions.
  e.g. `.fingerprint alice`
- **`.loglevel [DEBUG|INFO|WARNING|ERROR]`** - read or set the base log level.
  Audit-logged. e.g. `.loglevel DEBUG`
- **`.debug [on|off|<subsystem> [off]]`** - bare or `on` enables debug for every
  subsystem; `off` disables it and clears per-subsystem overrides. One subsystem per
  invocation, bare name or `internets.`-prefixed. Audit-logged.
  e.g. `.debug weather`, then `.debug weather off`

Moderation:

- **`.shadow-ban <nick> [reason]`** - silently drop all of a nick's commands and
  suppress module `on_raw` delivery. Refuses the bot itself and the caller.
  Persisted 0600. Audit-logged. e.g. `.shadow-ban spammer flooding`
- **`.shadow-unban <nick>`** - lift a shadow-ban. Audit-logged.
  e.g. `.shadow-unban spammer`
- **`.shadow-list`** - list active shadow-bans with reasons. e.g. `.shadow-list`

---

## Module commands

Grouped exactly as `.help` groups them (`admin_cmds.py - _MODULE_GROUPS`); modules
in no group fall into "More", which is what `.help` does at runtime.

## Weather and space

### weather

Module `weather` - key: none required (provider keys optional) - 15 commands.

Location argument accepted by every weather command: a city, a ZIP, a postal code
with country (`08000 es`), `lat,lon`, or `39°N 98°W`. `.regloc` saves a default.
`-n <nick>` reads another user's saved location. A `-<flag>` forces one provider
(`-nws`, `-vc`, `-aw`, ...); `.<cmd> -l` lists the flags available for that command.

- **`.weather [place]`** (`.w`) - current conditions. e.g. `.w 90210`
- **`.forecast [place]`** (`.f`) - 4-day daily forecast. e.g. `.f -vc Tokyo`
- **`.hourly [place]`** (`.h`) - next 12 hours. e.g. `.h Berlin`
- **`.nowcast [place]`** (`.nc`) - short-range precipitation nowcast.
  e.g. `.nc Seattle`
- **`.aqi [place]`** (`.air`) - air quality index and pollutant concentrations.
  e.g. `.aqi -an 67127`
- **`.uv [place]`** (`.uvi`) - UV index now and today's peak. e.g. `.uv London`
- **`.pollen [place]`** (`.allergy`) - pollen counts. e.g. `.pollen Austin`
- **`.astro [place]`** (`.sun`) - sun and moon ephemeris. e.g. `.astro Reykjavik`
- **`.alerts [place]`** (`.al`) - active weather alerts, widening to state level when
  nothing is local. e.g. `.alerts 33101`
- **`.wildfire [place]`** (`.fire`) - nearby fire detections. e.g. `.fire Redding`
- **`.space [place]`** (`.aurora`) - Kp index and aurora chance. e.g. `.aurora`
- **`.marine [place]`** (`.sea`) - waves, swell, water temperature.
  e.g. `.marine Monterey`
- **`.tides [place]`** (`.tide`) - next high and low from the nearest station.
  e.g. `.tides -coops`
- **`.history <YYYY-MM-DD> [place]`** (`.hist`) - conditions on a past date.
  e.g. `.hist 2024-07-04 Boston`
- **`.providers`** - **admin.** Provider health and capability chains.
  e.g. `.providers`

### iss

Module `iss` - key: none - 1 command.

- **`.iss`** - ISS ground position and current crew. e.g. `.iss`

### spacex

Module `spacex` - key: none - 1 command.

- **`.spacex`** - next scheduled SpaceX launch. e.g. `.spacex`

### apod

Module `apod` - key: `nasa_api_key` (falls back to NASA's shared demo key) -
1 command.

- **`.apod`** - NASA Astronomy Picture of the Day. e.g. `.apod`

### astro2

Module `astro2` - key: none (keyless endpoints) - 5 commands.

- **`.solar`** - NOAA space weather: X-ray flare class and sunspot number.
  e.g. `.solar`
- **`.neo`** - NASA near-earth objects today plus the closest approach. e.g. `.neo`
- **`.launches [n]`** - next 1 to 3 rocket launches. e.g. `.launches 3`
- **`.moon [YYYY-MM-DD]`** - moon phase, illumination, age. e.g. `.moon 2026-01-01`
- **`.sky <M#|name>`** - Messier catalog lookup. e.g. `.sky M31`

### satpass

Module `satpass` - key: `n2yo_api_key` **required** - 1 command.

- **`.passes <sat> <lat,lon>`** - next visible pass of a satellite.
  e.g. `.passes 25544 39.1,-94.6`

## Science and math

### mathx

Module `mathx` - key: none, no network - 9 commands.

- **`.isprime <n>`** - primality test plus the next prime. e.g. `.isprime 97`
  **Known defect:** this runs synchronously on the event loop and falls through to an
  unbounded Pollard rho for large composites, so a pasted 100-digit semiprime hangs
  the whole bot. Verified; see [known-issues.md](known-issues.md).
- **`.factor <n>`** - prime factorisation. e.g. `.factor 360`
- **`.gcd <a> <b> [...]`** - GCD and LCM. e.g. `.gcd 84 132`
- **`.base <n> <from> <to>`** - convert between bases 2 to 36. e.g. `.base ff 16 10`
- **`.stats <n1 n2 ...>`** - mean, median, stdev, min, max, sum.
  e.g. `.stats 3 7 7 19`
- **`.roman <n|numeral>`** - Arabic to Roman and back, 1 to 3999. e.g. `.roman 1987`
- **`.pct <expr>`** - percentage helper. e.g. `.pct 20% of 150`
- **`.bignum <expr>`** - exact big-integer `n!`, `fib(n)`, `2^n`. e.g. `.bignum 50!`
- **`.const <name>`** - physical constant value and unit. e.g. `.const planck`

### physcalc

Module `physcalc` - key: none, no network - 6 commands.

- **`.ly <distance>`** - light time to distance and back (ly/au/km/min).
  e.g. `.ly 4.2ly`
- **`.sr <v>`** - special-relativity gamma for a velocity as a fraction of c.
  e.g. `.sr 0.9`
- **`.escape <body|m r>`** - escape velocity and surface gravity. e.g. `.escape mars`
- **`.ohm <two of V,I,R,P>`** - Ohm's law and power solver. e.g. `.ohm 12V 2A`
- **`.rc <bands|ohms>`** - resistor colour code to value and back.
  e.g. `.rc brown black red`
  **Known defect:** the 5-band decode consumes the tolerance band as a multiplier and
  returns a wrong value, and `tests/test_physcalc.py` asserts the wrong value.
- **`.baud <bytes> <bps>`** - serial transfer time. e.g. `.baud 1024 9600 -fmt 8N1`

### numberfact

Module `numberfact` - key: none - 1 command.

- **`.numberfact [n] [type]`** (`.nf`) - number trivia; type is
  `trivia`, `math`, `date`, or `year`. e.g. `.nf 42 math`

### scinews

Module `scinews` - key: none (curated feeds) - 1 command.

- **`.sci [topic|read <N>|sources]`** - science, infosec, AI, and BSD headlines;
  `read <N>` opens item N from the last list, `sources` lists feed topics.
  e.g. `.sci sources`

## Dev, net, and security

### calc

Module `calc` - key: none, no network - 1 command.

- **`.cc <expression>`** - calculator. e.g. `.cc sqrt(144)`

### netcalc

Module `netcalc` - key: none, no network - 3 commands.

- **`.cidr <ip/prefix>`** - network, broadcast, mask, host count, range.
  e.g. `.cidr 192.0.2.0/24`
- **`.subnet <ip/prefix> <newlen>`** - split a block into subnets.
  e.g. `.subnet 198.51.100.0/24 26`
- **`.port <number|name>`** - port number to service name and back. e.g. `.port 6697`

### encode

Module `encode` - key: none, no network - 12 commands.

- **`.unicode <char|U+hex|name>`** - codepoint, name, category, UTF-8 bytes, block.
  e.g. `.unicode U+2603`
- **`.hash <algo> <text>`** - md5, sha1, sha256, sha512, or blake2b digest.
  e.g. `.hash sha256 hello`
- **`.crc <text>`** - CRC32 and Adler-32. e.g. `.crc hello`
- **`.b32 <text>`** - Base32 encode or decode, auto-detected. e.g. `.b32 hello`
- **`.slug <text>`** - slugify text. e.g. `.slug Hello World!`
- **`.ulid`** - generate a ULID. e.g. `.ulid`
- **`.ascii [dec|hex|char]`** - ASCII table lookup. e.g. `.ascii 65`
- **`.ds <value> <unit>`** - data-size conversion, decimal and binary.
  e.g. `.ds 1.5 GiB`
- **`.defang <url>`** - defang or refang a URL, IP, or email. e.g. `.defang 192.0.2.1`
- **`.entropy <password>`** - estimate password entropy. e.g. `.entropy correcthorse`
- **`.pw [len] [-s]`** - random password, `-s` for a passphrase. e.g. `.pw 24`
- **`.lorem [words]`** - lorem ipsum filler. e.g. `.lorem 30`

### devtools

Module `devtools` - key: none, no network - 7 commands.

- **`.jwt <token>`** - decode JWT header and payload. No signature check.
  e.g. `.jwt eyJhbGci...`
- **`.semver <a> <b>`** - compare two semantic versions. e.g. `.semver 1.2.3 1.10.0`
- **`.uuid5 <ns> <name>`** - deterministic UUIDv5, or inspect an existing UUID.
  e.g. `.uuid5 dns example.com`
- **`.tz <time> <from> <to>`** - convert a clock time between zones.
  e.g. `.tz 14:30 UTC America/Chicago`
- **`.unix <signal|errno>`** - look up a Unix signal or errno. e.g. `.unix SIGHUP`
- **`.color <value>`** - hex, rgb, hsl conversion plus nearest CSS name.
  e.g. `.color #3366ff`
- **`.cron <expr>`** - validate and explain a cron expression with next fire times.
  e.g. `.cron */15 * * * *`

### devutils

Module `devutils` - key: none, no network - 6 commands.

- **`.b64 <text>`** - Base64 encode. e.g. `.b64 hello`
- **`.unb64 <text>`** - Base64 decode. e.g. `.unb64 aGVsbG8=`
- **`.hex <text>`** - hex encode or decode, auto-detected. e.g. `.hex hello`
- **`.morse <text>`** - Morse encode or decode; `/` is a word break.
  e.g. `.morse SOS`
- **`.uuid`** - random UUID4. e.g. `.uuid`
- **`.epoch [arg]`** - epoch seconds to ISO 8601 UTC and back.
  e.g. `.epoch 1767225600`

### httpcode

Module `httpcode` - key: none, local table - 1 command.

- **`.http <code>`** - HTTP status code lookup. e.g. `.http 418`

### qr

Module `qr` - key: none; emits a URL, fetches nothing - 1 command.

- **`.qr <text>`** - QR-code image URL. e.g. `.qr https://example.com`
  **Known defect:** the advertised 1000-character cap collides with the 400-character
  `strip_ctrl` default, so long inputs emit a silently truncated, broken QR link.

### pkginfo

Module `pkginfo` - key: none - 3 commands.

- **`.pypi <package>`** - PyPI version, summary, licence, release date.
  e.g. `.pypi requests`
- **`.npm <package>`** - npm version, description, licence, date. e.g. `.npm express`
- **`.crates <name>`** - crates.io version, downloads, licence, docs.
  e.g. `.crates serde`

### ghinfo

Module `ghinfo` - key: none (unauthenticated GitHub API, 60 requests/hour) -
1 command.

- **`.gh <owner/repo>`** - repository stars, language, licence, last push.
  e.g. `.gh torvalds/linux`

### dnsutils

Module `dnsutils` - key: none, HTTPS DoH and RDAP - 5 commands.

- **`.dns <host> [type]`** - DNS lookup. Ten record types are accepted although the
  help advertises six. e.g. `.dns example.com MX`
- **`.rdns <ip>`** - reverse PTR lookup. e.g. `.rdns 192.0.2.1`
- **`.caa <domain>`** - CAA records plus SPF and DMARC. e.g. `.caa example.com`
- **`.whois <domain>`** - RDAP domain registration info. e.g. `.whois example.com`
- **`.asn <ip|ASn>`** - RDAP network and autonomous-system info. e.g. `.asn AS15169`

### probe

Module `probe` - key: none; connects to a user-supplied host through the SSRF guard -
4 commands.

- **`.headers <url>`** - status, server, content type, redirects, security headers.
  e.g. `.headers https://example.com`
- **`.ssl <host[:port]>`** - TLS certificate issuer, CN, days to expiry.
  e.g. `.ssl example.com:443`
  **Known defect:** a bare IPv6 literal is mangled, because the host is split at the
  first colon.
- **`.tcp <host> <port>`** - TCP connect probe with latency. e.g. `.tcp example.com 22`
- **`.down <host|url>`** - reachability check. e.g. `.down example.com`

### secinfo

Module `secinfo` - key: none - 5 commands.

- **`.cve <CVE-ID>`** - NVD CVSS score, summary, publication date.
  e.g. `.cve CVE-2021-44228`
- **`.pwn <password>`** - **PM only.** HIBP breach count; only a hash prefix leaves
  the process (k-anonymity). Refused in a channel. e.g. `/msg bot pwn hunter2`
- **`.hashid <hash>`** - identify the likely hash type. e.g. `.hashid 5f4dcc3b5aa...`
- **`.cvss <vector>`** - compute a CVSS v3.1 base score.
  e.g. `.cvss AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H`
- **`.cipher <name>`** - cipher reference: key size, status. e.g. `.cipher AES-256-GCM`

### ipinfo

Module `ipinfo` - key: none - 1 command.

- **`.ipinfo <ip/host>`** - IP geolocation. e.g. `.ipinfo 8.8.8.8`
  Note: this module queries ip-api.com over cleartext HTTP, so the response is
  forgeable by an on-path observer.

## Reference

### dictionary

Module `dictionary` - key: none - 1 command.

- **`.dict <word> [/N]`** (`.dictionary`) - definition; `/N` selects the Nth sense.
  e.g. `.dict ephemeral /2`

### urbandictionary

Module `urbandictionary` - key: none - 1 command.

- **`.u <word> [/N]`** (`.urbandictionary`) - Urban Dictionary entry.
  e.g. `.u yolo /2`

### reflookup

Module `reflookup` - key: none - 8 commands.

- **`.wiki <query>`** - Wikipedia summary and link. e.g. `.wiki Alan Turing`
- **`.doi <doi>`** - Crossref work metadata. e.g. `.doi 10.1038/nature12373`
- **`.isbn <isbn>`** - Open Library book lookup. e.g. `.isbn 9780132350884`
- **`.so <query>`** - top Stack Overflow question. e.g. `.so python asyncio timeout`
- **`.rfc <number|title>`** - RFC by number or title search. e.g. `.rfc 2812`
- **`.rtfm <command>`** - Unix, Linux, and BSD command reference via tldr.
  e.g. `.rtfm tar`
- **`.arxiv <id|query>`** - arXiv paper lookup. e.g. `.arxiv 1706.03762`
  Note: this is the one cleartext HTTP URL in the module.
- **`.element <name|symbol|Z>`** - periodic-table entry, offline. e.g. `.element Fe`

### translate

Module `translate` - key: none - 1 command.

- **`.t [src] <tgt> <text>`** (`.translate`) - translate text. e.g. `.t en es Hello`

### search

Module `search` - key: `brave_key` required for `.si` only - 2 commands.

- **`.sw <query>`** (`.g`) - web search via DuckDuckGo, keyless.
  e.g. `.sw asyncio tutorial`
- **`.si <query>`** (`.gi`) - image search; requires a Brave API key.
  e.g. `.si aurora borealis`

### location

Module `location` - key: none - 3 commands.

- **`.regloc <zip|city>`** (`.register_location`) - save your default location for
  the weather commands. e.g. `.regloc 90210`
  Note: this handler writes nick-and-location pairs into the bot log, where
  `.forgetme` cannot purge them. Privacy finding, recorded in the ledger.
- **`.myloc`** - show your saved location. e.g. `.myloc`
- **`.delloc`** - remove your saved location. e.g. `.delloc`

## Media and finance

### imdb

Module `imdb` - key: `omdb_key` **required** - 1 command.

- **`.imdb <title>`** - movie or TV lookup. e.g. `.imdb The Matrix`

### lastfm

Module `lastfm` - key: `lastfm_key` **required** - 1 command.

- **`.lastfm <user>`** - Last.fm profile and now playing. e.g. `.lastfm RJ`

### youtube

Module `youtube` - key: `youtube_key` **required** - 1 command.

- **`.yt <search>`** (`.youtube`) - YouTube search. e.g. `.yt cat videos`

### xkcd

Module `xkcd` - key: none - 1 command.

- **`.xkcd [num]`** - comic, random or by number. e.g. `.xkcd 927`

### mtg

Module `mtg` - key: none (Scryfall) - 1 command.

- **`.mtg <card>`** - Magic: the Gathering card. e.g. `.mtg Black Lotus`

### poke

Module `poke` - key: none (PokeAPI) - 1 command.

- **`.poke <name|id>`** (`.pokemon`) - Pokemon info. e.g. `.poke pikachu`

### dnd

Module `dnd` - key: none (dnd5eapi.co) - 1 command.

- **`.dnd <name>`** - D&D 5e SRD spell or monster. e.g. `.dnd fireball`

### recipe

Module `recipe` - key: none (TheMealDB) - 1 command.

- **`.recipe <name>`** (`.meal`) - recipe lookup. e.g. `.recipe carbonara`

### cocktail

Module `cocktail` - key: none (TheCocktailDB) - 1 command.

- **`.cocktail <name>`** (`.drink`) - cocktail recipe. e.g. `.cocktail negroni`

### steam

Module `steam` - key: `steam_key` **required** - 2 commands.

- **`.steam [user|-g|-n <nick>]`** - Steam status and games. e.g. `.steam -g`
- **`.regsteam <id|vanity>`** (`.register_steam`) - register your Steam ID.
  e.g. `.regsteam gabelogannewell`

### twitch

Module `twitch` - key: `twitch_client_id` and `twitch_client_secret` **required** -
1 command.

- **`.tw [-s <query>|-c <channel>|-g <game>]`** (`.twitch`) - top live streams by
  default; `-c` for channel info, `-g` to search games. e.g. `.tw -c esl_sc2`

### idlerpg

Module `idlerpg` - key: none - 1 command.

- **`.irpg <player>`** (`.idlerpg`) - IdleRPG player info. e.g. `.irpg alice`

### hn

Module `hn` - key: none (Firebase HN API) - 1 command.

- **`.hn [rank]`** - top Hacker News story, rank 1 to 30. e.g. `.hn 3`

### reddit

Module `reddit` - key: none - 1 command.

- **`.reddit <sub> [period]`** (`.r`) - top post from a subreddit; period is
  `hour`, `day`, `week`, and so on. e.g. `.r netsec week`

### crypto

Module `crypto` - key: none (CoinGecko public API) - 1 command.

- **`.gecko <symbol>`** (`.coingecko`, `.cg`) - spot price and 24 h change.
  e.g. `.cg btc`

### fx

Module `fx` - key: none (frankfurter.dev, ECB rates) - 1 command.

- **`.fx <from> <to> [amount]`** - currency conversion. e.g. `.fx usd eur 100`

### stocks

Module `stocks` - key: at least one finance provider key **required** - 2 commands.

- **`.stock <symbol>`** (`.s`) - stock quote. e.g. `.s AAPL`
- **`.crypto <symbol>`** - crypto price through the finance providers, distinct from
  `.gecko` above. e.g. `.crypto BTC`

**Known security defect (verified, reproduced):** when every provider fails,
`_try_providers()` appends `str(exception)` to the channel reply. urllib3 transport
errors embed the full request URL including `token=` and `apikey=` query parameters,
so an outage while keys are configured publishes those keys to the channel.
`redact_secrets()` is log-only and does not scrub PRIVMSG. See
[known-issues.md](known-issues.md).

## Fun

### bofh

Module `bofh` - key: none, local list - 1 command.

- **`.bofh`** (`.excuse`) - random BOFH excuse. e.g. `.bofh`

### cowsay

Module `cowsay` - key: none, local - 1 command.

- **`.cowsay <text>`** - ASCII cow speaks the text. e.g. `.cowsay moo`

### fact

Module `fact` - key: none - 1 command.

- **`.fact`** - random useless fact. e.g. `.fact`

### catfact

Module `catfact` - key: none - 1 command.

- **`.catfact`** (`.cat`) - random cat fact. e.g. `.cat`

### chuck

Module `chuck` - key: none - 1 command.

- **`.chuck`** - random Chuck Norris joke. e.g. `.chuck`

### dadjoke

Module `dadjoke` - key: none - 1 command.

- **`.dadjoke`** (`.joke`) - random dad joke. e.g. `.joke`

### advice

Module `advice` - key: none - 1 command.

- **`.advice`** - random piece of advice. e.g. `.advice`

### bored

Module `bored` - key: none - 1 command.

- **`.bored`** - random activity suggestion. e.g. `.bored`

### games

Module `games` - key: none, local - 4 commands.

- **`.coin`** - flip a coin. e.g. `.coin`
- **`.8ball <question>`** - magic 8-ball. e.g. `.8ball will it build`
- **`.rps <choice>`** - rock, paper, scissors. e.g. `.rps rock`
- **`.choose A, B, C, ...`** - pick one at random. e.g. `.choose tea, coffee`

### dice

Module `dice` - key: none, local - 1 command.

- **`.d [X]dN[+/-M]`** - dice roller. e.g. `.d 3d6+2`

### qdb

Module `qdb` - key: none - 1 command.

- **`.qdb [id]`** - random or specific bash.org-style quote. e.g. `.qdb 4281`

### fml

Module `fml` - key: none - 1 command.

- **`.fml`** - random FMyLife quote. e.g. `.fml`

## Utility and social

### remind

Module `remind` - key: none, local state - 3 commands.

- **`.remind <when> <msg>`** - schedule a reminder. `when` accepts `30s`, `5m`,
  `1h30m`, `tonight`, `14:30 UTC`, or an ISO timestamp.
  e.g. `.remind 1h30m stand up`
- **`.remind-list`** - list your pending reminders. e.g. `.remind-list`
- **`.remind-cancel <N>`** - cancel reminder N. e.g. `.remind-cancel 2`

### tell

Module `tell` - key: none, local state - 3 commands.

- **`.tell <nick> <msg>`** - leave a message delivered on the recipient's next
  PRIVMSG. e.g. `.tell alice build is green`
- **`.tell-cancel`** - cancel all your pending tells. e.g. `.tell-cancel`
- **`.tell-list`** - list your pending tells. e.g. `.tell-list`

### notes

Module `notes` - key: none, local state - 1 command.

- **`.notes <sub> [args]`** - personal sticky notes. Subcommands: `list`,
  `add <text>`, `del <N>`, `show <N>`, `clear`. e.g. `.notes add check the lockfile`

### seen

Module `seen` - key: none, local state - 1 command.

- **`.seen <nick>`** - when a nick was last seen and what they were doing.
  e.g. `.seen alice`

### channels

Module `channels` - key: none - 3 commands.

- **`.join <#channel>`** - ask the bot to join. An admin is honoured immediately;
  anyone else goes through channel-founder verification. e.g. `.join #ops`
- **`.part <#channel>`** - ask the bot to leave, same authorisation.
  e.g. `.part #ops`
- **`.users [#channel]`** - **admin.** Tracked users in a channel; the reply carries
  hostmask PII, which is why it is gated. e.g. `.users #ops`
  Note: the module's own `.help` line does not mark this admin-only. The handler
  does enforce it.

### urls

Module `urls` - key: none - 2 commands.

- **`.shorten <url>`** - shorten a URL via is.gd. e.g. `.shorten https://example.com`
- **`.expand <url>`** (`.unshorten`) - expand a shortened URL.
  e.g. `.expand https://is.gd/abc`

### privacy

Module `privacy` - key: none - 4 commands.

- **`.forgetme`** - **PM only.** Erase every record the bot holds about you.
  e.g. `/msg bot forgetme`
- **`.privacy`** - **PM only.** Disclose what the bot stores about you.
  e.g. `/msg bot privacy`
- **`.optout`** - mark yourself opted out of future tracking. e.g. `.optout`
- **`.optin`** - undo a previous opt-out. e.g. `.optin`

  Note: only `.forgetme` and `.privacy` call `_require_pm()`; `.optout` and
  `.optin` run anywhere and answer by NOTICE. No privacy command is
  rate-limited.
  `.forgetme` also reports "tracking in 1 channel(s) (erased now)" for a user who
  was never tracked, because the opt-out sentinel row is counted.

## More

Modules that `.help` lists under "More" because they are in no group.

### health

Module `health` - key: none - 2 commands.

- **`.health`** - **admin.** Per-subsystem health snapshot, one line per subsystem.
  e.g. `.health`
  **Known defect:** two fields print `?` permanently, because the handler reads
  `_dirty_locations` / `_dirty_channels` while `Store` defines `_dirty_locs` /
  `_dirty_chans`.
- **`.uptime`** - public bot uptime. Shadowed at runtime by the admin core
  `.uptime`, which the dispatcher resolves first, so this handler is unreachable
  while the core command exists. e.g. `.uptime`

### ipintel

Module `ipintel` - key: `abuseipdb_key` optional; fully usable keyless - 1 command.

- **`.ip <ip|host>`** (`.rep`) - aggregated IP reputation across DNSBLs, DShield,
  GreyNoise, Tor exit lists, and AbuseIPDB when a key is present.
  e.g. `.ip 192.0.2.1`

### scholar

Module `scholar` - key: none, all sources keyless - 3 commands.

- **`.papers <orcid|query> [-oa]`** - papers by ORCID iD or topic via OpenAlex;
  `-oa` restricts to open access. e.g. `.papers 0000-0002-1825-0097 -oa`
- **`.thesis <query> [-oa]`** - dissertations and theses by topic.
  e.g. `.thesis quantum error correction`
- **`.scholar <name|topic>`** - find researchers and their ORCID iDs.
  e.g. `.scholar Jane Goodall`

### linktitle

Module `linktitle` - key: none (`youtube_key` enriches YouTube links) - 0 commands.

Passive only: announces page titles for URLs posted in channels, through the
`on_raw` fanout. Nothing to invoke.

Note: it logs every announced and skipped URL with its channel at INFO level, which
puts user browsing activity in the bot log.

### example

Module `example` - key: none - 1 command. A copy-and-fill skeleton for a new module,
not a user feature.

- **`.example <text>`** - echo the text back uppercased. e.g. `.example hello`

  Note: the skeleton's comments describe an admin bypass on the API cooldown that
  does not exist, and overstate its input bounding. Read
  [Writing Modules](writing-modules.md) as the authority, not this file's comments.

---

## See also

- [IRC Protocol](irc-protocol.md) - line parsing, the gates' protocol context.
- [Runtime Architecture](architecture.md) - dispatch pipeline and module loader.
- [Modules](modules.md) - what each module does and which upstream it calls.
- [Writing Modules](writing-modules.md) - the `BotModule` contract and `COMMANDS`.
- [Configuration](configuration.md) - prefix, cooldowns, autoload, and keys.
- [internals/admin_cmds.md](internals/admin_cmds.md) - core command internals.
