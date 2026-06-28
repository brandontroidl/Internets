# Changelog

All notable changes to Internets are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Skeleton module (`modules/example.py`).** A loadable, fully-commented
  copy-and-fill template for a new command module: documents the `BotModule`
  contract (`COMMANDS`, the `cmd_*(nick, reply_to, arg)` signature, `on_load`,
  `is_configured`, `help_lines`, `forget`, `setup`) and the real conventions -
  rate limiting, `strip_ctrl` on output, the off-loop `_fetch_sync` shape with
  error handling over the size-capped `fetch_json`, the `_netsafe` SSRF caveat
  for user-supplied URLs, and the shared User-Agent via `cred`. Not autoloaded;
  `docs/modules.md` Part 1 points to it.

### Fixed

- **Weather: gap-fill N/A current-conditions fields from the fallback chain.**
  Providers build results with `.get()`, so a sparse upstream response yielded a
  non-None result with missing fields; the dispatcher returned it and the
  formatter printed N/A even when a fallback had the data. A result with no
  usable core now falls through (`WeatherResult`/`HourlyResult.is_empty`), and a
  sparse current result keeps its more-accurate temperature and conditions and
  has only its missing secondary fields filled from the next usable provider,
  crediting both sources (`[NWS + Open-Meteo]`).

## [4.0.0] - 2026-06-28

Major release. Backward-incompatible changes: `[bot] default_location` was
removed (the key is now ignored; `.weather` with no saved location prompts
`.regloc` instead of falling back to a default), and `is_admin` now fails
closed on an unverifiable hostmask binding. See Removed / Security below.

### Added - `ipintel` (`.ip` / `.rep`) IP-reputation aggregator

- New keyless multi-source command (`modules/ipintel.py`, autoloaded). One IRC
  line per target aggregating: 6 DNSBL zones over Cloudflare DNS-over-HTTPS
  (DroneBL, SpamCop, PSBL, UCEPROTECT, s5h, GBUdb), SANS ISC / DShield,
  GreyNoise community, the Tor bulk exit list, and AbuseIPDB (optional, via the
  new `abuseipdb_key` secret - degrades cleanly to keyless when unset).
- Safety model: the target is resolved to ONE public IP through
  `_netsafe.resolve_safe_ip` before any request, so a private / loopback /
  link-local / reserved / unresolvable target is refused and an internal IP can
  never leak to a third party. The validated IP only ever appears as a query
  param / path segment against FIXED endpoints (no user-controlled URL), so
  there is no SSRF surface here. Every upstream field is `strip_ctrl`'d; every
  body is size-capped; the fan-out isolates per-source failures so one dead
  source never breaks the reply.
- Spamhaus ZEN is deliberately NOT in the zone set: it refuses large public
  resolvers and would always read "clean" over DoH (worse than absent). A DNSBL
  hit is an A record in `127.0.0.0/8` excluding the `127.255.255.0/24`
  query-refused sentinel; the IPv4-only zones report `DNSBL n/a` for IPv6
  rather than a false "clean". Tor exit list cached 1h. 38 tests
  (`tests/test_ipintel.py`).

### Changed - `scinews` feed expansion (52 feeds, new ai/tech/sec/pentest/bsd topics)

- `.sci` grew from the original ~12 STEM feeds to **52** across four steps. New
  topics: `ai` and `tech` (New Scientist, Sci. American, Live Science, Eos, MIT
  Tech Review, The Register, IEEE Spectrum, Ars Technica, arXiv cs.AI/cs.LG,
  Physics World, STAT News, Space.com, NASA, plus Simon Willison, Hugging Face,
  OpenAI, DeepMind, Import AI, Latent Space); `sec` and `pentest` (The Hacker
  News, BleepingComputer, Krebs, Dark Reading, SecurityWeek, Schneier, Register
  Security, SANS ISC, CISA advisories, Exploit-DB, Project Zero, PortSwigger,
  The Record, Help Net Security, plus DFIR Report, Unit 42, Cisco Talos,
  abuse.ch); `bsd` (OpenBSD Journal / undeadly.org).
- `pentest` is the offensive subset of `sec`; security feeds are kept OUT of
  `all` so a bare `.sci` stays science. Project Zero uses its 25-item summary
  feed (the full feed is ~13 MB, over the reader's 6 MB cap); Packet Storm was
  dropped (TLS error). APS Physics uses `feeds.aps.org/rss/recent/physics.xml`
  (the `physics.aps.org` path 403s behind Cloudflare).
- Feed fetches are now bounded by an `asyncio.Semaphore(8)` so the larger set
  cannot spike the thread pool.

### Changed - geocode postal/coordinate accuracy rework + `default_country`

- **Structured postal-code resolution replaces fuzzy free-text.** Free-text
  Nominatim `q=` fuzzy-matches a postal code to the nearest building, so
  `08000` pinned to the US returned a random Ohio motel and `A1A 1A1` returned
  a Swiss street. `geocode()` now classifies the input (`_postal_kind`) and
  resolves it through structured lookups that match the value AS a postal code
  (Nominatim `postalcode=` / Zippopotam.us), returning nothing on a bogus code
  instead of garbage. It deliberately does NOT fall back to free-text for a
  classified postal code.
- **Country handling:** ZIP+4, Canadian alphanumeric, UK, plus distinctive
  dashed Ireland Eircode / Japan / Brazil formats are globally unique and pin
  their own country (CA resolves via Zippopotam-by-FSA, which has data OSM
  lacks). A bare numeric code is shared across countries and resolves
  home-country-first via the new `[weather] default_country` (ISO2, default
  `us`): a real local code stays local, one invalid there falls back to the
  global best match - so with `us`, `.w 43812` -> Ohio but `.w 08000` ->
  Barcelona. An explicit trailing country overrides (`.w 08000 spain` / `es`).
  A 2-letter tail that collides with a US state / CA province abbrev
  (`90210 ca`) is NOT treated as a country pin so the ZIP still resolves;
  `_normalize_cc` coerces a junk `default_country` back to `us` so it cannot
  disable the bias or inject into `countrycodes`. `default_country` is part of
  the geocode cache key.
- **Coordinate parsing** now handles decimal, hemisphere (`39°N 98°W`, either
  order), and DMS (`39°50'15"N 98°35'W`) forms, normalised to signed decimal
  and reverse-geocoded at the exact point; un-parsed, `39°N 98°W` resolved to a
  random Missouri suburb. A bare `39 98` (no comma/sign/decimal) is rejected as
  too ambiguous. Out-of-range pairs are rejected, not sent upstream.
- **Removed `[bot] default_location`.** `.weather` with no saved location no
  longer silently answers with an operator default (which users mistook for
  their own weather); it now tells them to `.regloc`. `weather`/`location` pass
  `default_country` into `geocode()`; the not-found echo in `weather._geo` is
  now `strip_ctrl`'d like `location`'s.

### Changed - `reflookup` / `spacex`

- `.rfc` accepts a title/keyword (datatracker search resolves it to the RFC,
  like `.wiki`), not just a number; rfc-editor title/status fields are
  whitespace-trimmed (they carried leading/trailing spaces that produced double
  spaces). New `.rtfm <command>` returns a tldr-pages summary for Unix/Linux/BSD
  commands. `.wiki` adds an opensearch fallback when the case/punctuation-
  sensitive REST summary endpoint 404s on an exact title.
- `.spacex` switched from the dead `api.spacexdata.com` (HTTP 525) to Launch
  Library 2 (one cached request).

### Security - availability and auth hardening

- **`is_admin` fails closed on an unverifiable hostmask binding.** It granted
  on the `unknown` sentinel and on a missing current hostmask, so a nick-only
  admin session re-created during the `cmd_auth` verify-password TOCTOU
  survived the admin's disconnect and any later nick-grabber inherited full
  admin (`.load` arbitrary module exec, `.raw`, `.restart`). Now grants only on
  a present, matching hostmask; revokes the sentinel and changed bindings on
  check; `cmd_auth` refuses to persist an `unknown` binding.
- **Store quarantine instead of clobber.** `Store._read` silently reset to
  empty on a checksum / size / shape / parse failure and the next flush
  overwrote the only on-disk copy via `os.replace`, destroying locations,
  channel-rejoin state, and opt-out flags (the bot then resumed tracking
  opted-out users). `_unwrap` now raises `_StoreRejected` so a real rejection
  renames the suspect file aside to `<name>.corrupt.<ts>`; `_write` also keeps
  a one-deep `<name>.bak` of the previous good file before each atomic replace.
- **Uniform `strip_ctrl` on emitted upstream/user text.** `search`, `seen`,
  `tell`, `stocks` spliced third-party or user text into bot-attributed lines
  without the canonical sanitizer (the sender backstop only strips CR/LF/NUL),
  so format/colour/BEL/ANSI bytes reached output - `search` worst, where
  `html.unescape` recreated control bytes and URLs were unstripped. `search`
  now sanitizes title/desc/URLs, `seen` strips at record time, `tell` strips
  message+target at capture, `stocks` strips the echoed symbol. A completeness
  gate fails the suite if a new emitter skips the sanitizer. `remind` likewise
  strips control bytes at capture (immediate ack and delayed delivery).
- **Audit key fails closed.** `_load_key` caught `OSError` on an EXISTING key
  file and fell through to a fresh `O_TRUNC` write, silently destroying every
  prior record's HMAC tamper-evidence on a transient FS error. A read failure
  on an existing key now raises; a genuinely-malformed key is moved aside to
  `.bad` before a fresh one is written, never truncated over a recoverable key.
- **Dispatcher time budget + breaker honesty** (`weather_providers/_dispatch.py`).
  The provider fallback chain had no end-to-end budget against the 60s outer
  command timeout - a slow provider (NWS makes 2-3 sequential 10s hops) could
  starve healthy fallbacks or blow the outer timeout. The dispatcher now
  captures a 45s chain deadline and caps each call at `min(30s, time-left)` via
  `asyncio.wait_for`. Separately, `record_success` ran BEFORE the None check,
  so a provider returning no data (incl. a slow brownout) still booked a success
  and reset its breaker streak and could never be shed; success is now booked
  only on real data, a None books nothing, and a `wait_for` timeout trips the
  breaker as a failure.
- **Untrusted-user DoS bounds.** `.cron` scanned up to ~527k minutes inline on
  the event loop (now offloaded to `to_thread` and short-circuits impossible
  `(month, day)` like Feb 30); `.bignum` ran factorial/fib/power up to ~1M
  digits inline (offloaded; also raised the int->str digit cap that broke the
  feature over most of its range); `.users` emitted one NOTICE per tracked nick
  uncapped (now 20 most-recent + a summary).
- **Opt-out survives the prune.** The 90-day stale-user prune deleted records
  purely by `last_seen` with no exemption for `opted_out`, and `set_opt_out`
  stamps `last_seen` once and never refreshes it, so an inactive opted-out user
  aged out and the bot resumed tracking them. `_prune_users` now never prunes
  an `opted_out` record; `Store` floors `user_max_age_days` at 1 (a 0/negative
  value made the cutoff `== now` and wiped all users + opt-out flags on first
  flush).
- **`.pypi`/`.npm`/`.crates` path validation.** The raw user package name was
  interpolated into the registry URL path with no charset check or quoting
  (`../simple`, `a/../b` could traverse within the trusted host). A
  conservative `_PKG_RE` + explicit `..` reject + `quote(name, safe='')` now
  gate it, matching `ipinfo`/`ipintel`.
- **Bounded caches and serialized refresh.** `crypto._fetch_sync` coin-id cache
  (attacker-influenceable via distinct `.gecko` lookups) gained `_CACHE_MAX=512`
  with FIFO eviction; `twitch._headers` OAuth check-then-refresh is now guarded
  by a `threading.Lock` so concurrent `.tw` lookups cannot both refresh.
- **Oversized-line discard revived + flush-loop guard.** The IRC read loop
  caught `asyncio.LimitOverrunError`, but `readline()` re-raises it as
  `ValueError`, so the >8192-byte recovery branch was dead code and every
  over-limit line fell through to a 5s reconnect stall with `oversized_lines`
  stuck at 0; now catches `ValueError`. `_flush_loop` ran `flush()` unguarded so
  one bad cycle silently killed the persistence thread (all future saves lost);
  it is now wrapped so a failure logs and the loop continues.
- **Config bounds + misc** (`3f40b62`): refuse an empty `command_prefix` at
  load; `metrics.expose` rejects `is_unspecified` binds (`::0`,
  `::ffff:0.0.0.0`, trailing-space forms the literal denylist missed) while
  still allowing loopback; the secret-store env-var tier now applies the same
  strip+placeholder filter as the file tier; `secret_store.set_value` rejects
  CR/LF; floor flood/api cooldowns at 1s so a 0/negative value cannot disable
  the per-nick gate; `weather` reads its UA via `cred()` (the key lives in
  `[secrets]`; a bare subscript KeyError'd on a default install and dropped
  weather from `.help`).
- **Honest counters + use-time prefix** (`b437bff`): the shutdown summary's
  `dropped=%d` was never incremented (only the Prometheus metric was) - `Sender`
  now has an `on_drop` callback so the printed count is real; core dispatch read
  the import-time `CMD_PREFIX` constant that a `.rehash` never refreshed, so a
  `command_prefix` change took effect for modules but not core dispatch - now
  read at use-time from `cfg`.

### Security - dependency CVEs, CodeQL/Bandit, policy

- **Dependency bumps (20 CVEs).** `aiohttp` 3.13.5 -> 3.14.1 (11 CVEs),
  `pyjwt` 2.12.1 -> 2.13.0 (8 PYSEC), `cryptography` 48.0.0 -> 49.0.0
  (GHSA-537c-gmf6-5ccf). `requirements.lock` regenerated; re-locked on Python
  3.10 so the hash-pinned `--require-hashes` install stays valid across the full
  3.10-3.14 CI matrix (3.14 alone drops aiohttp's conditional `typing-extensions`
  / `async-timeout`). `pip-audit` clean.
- **CodeQL code-scanning alerts (7 in-code).** `probe` pins the TLS prober to
  `minimum_version TLSv1_2`; `secinfo` marks the HIBP k-anonymity SHA-1 as
  `usedforsecurity=False`; `scinews` logs the best-effort HTML lead-parse
  failure instead of a silent pass; `mathx` `# nosec B311` on Pollard's-rho
  randomness; `metno` `# nosec B112` on the skip-malformed-timeseries continue.
  The 6 remaining alerts are URL-substring checks inside test assertions,
  dismissed as "used in tests".
- **`SECURITY.md`** - real policy replacing the GitHub-generated placeholder:
  supported = `main` + latest tagged release; private reporting via the repo
  Security tab ("Report a vulnerability", not a public issue/PR/IRC message);
  out of scope = the third-party APIs the bot calls + a deployer's own setup
  (exposed metrics port, weak admin password, a leaked `config.ini`). A draft
  that wrongly claimed the bot "feeds a honeypot/DNSBL pipeline" was corrected -
  `ipintel.py` only QUERIES reputation.

### Added: STEM, developer, network, and reference command modules

New command modules (each follows the standard BotModule contract; `.help`
groups them by category and `.modules` lists them). README User Commands is
regrouped to mirror those `.help` categories.

- **Science and math:** `mathx` (`.isprime` `.factor` `.gcd` `.base` `.stats`
  `.roman` `.pct` `.bignum` `.const`), `physcalc` (`.ly` `.sr` `.escape`
  `.ohm` `.rc` `.baud`), `scinews` (`.sci` STEM-feed aggregator plus a
  keyless article reader).
- **Developer and encoding:** `netcalc` (`.cidr` `.subnet` `.port`), `encode`
  (`.unicode` `.hash` `.crc` `.b32` `.slug` `.ulid` `.ascii` `.ds` `.defang`
  `.entropy` `.pw` `.lorem`), `devtools` (`.jwt` `.semver` `.uuid5` `.tz`
  `.unix` `.color` `.cron`), `pkginfo` (`.pypi` `.npm` `.crates`), `ghinfo`
  (`.gh`).
- **Network and security:** `dnsutils` (`.dns` `.rdns` `.caa` `.whois`
  `.asn`), `secinfo` (`.cve` `.pwn` `.hashid` `.cvss` `.cipher`), `probe`
  (`.headers` `.ssl` `.tcp` `.down`, all SSRF-guarded).
- **Reference:** `reflookup` (`.wiki` `.doi` `.isbn` `.so` `.rfc` `.arxiv`
  `.element`).
- **Space:** `astro2` (`.solar` `.neo` `.launches` `.moon` `.sky`), `satpass`
  (`.passes`, needs `n2yo_api_key`).

### Security

- **SSRF DNS-rebinding TOCTOU closed** in `probe` (`.headers`/`.down`),
  `scinews` (article reader), and `urls` (`.expand`/`.shorten`). New shared
  `modules/_netsafe.py` validates every DNS answer and pins the connection to
  the validated IP via thread-local DNS pinning. The previous IP-literal
  pinned adapter failed TLS SNI under urllib3 2.7 (so `urls` `.expand`
  silently broke on https); DNS pinning keeps the hostname for SNI/Host/cert.
  Single SSRF source of truth now, covered by `tests/test_netsafe.py`.

### Fixed

- **`.cron` event-loop DoS:** field bounds are validated before the integer
  range is built, so a huge range (e.g. `0-999999999`) is rejected at once
  instead of freezing the loop or risking OOM.
- **Command handlers now time out** (`asyncio.wait_for`), so a wedged handler
  cannot permanently hold a task slot and eventually block every command,
  including admin ones.
- **`.cc` (calc)** strips IRC control codes from the echoed expression and
  honors the rate-limit gate.
- **Weather HTTP** caps the aiohttp response body incrementally (it was
  buffered before the size check) and bounds error-snippet reads on both
  transports.
- **`.b32`** no longer re-encodes valid-but-binary base32 (dead-branch fix);
  a forced weather provider on an open circuit now warns instead of failing
  silently; `scinews` evicts stale last-list entries; dropped sends increment
  the drop metric; shadow-ban saves run off the event loop.

### Added - two air-quality providers (AirNow, PurpleAir)

- **`weather_providers/airnow/`** - US EPA official Air Quality Index via
  the AirNow `latLong/current` observation API.  Air-quality only, US
  locations only (raises on no coverage so the dispatcher falls through
  to a global provider).  Reports the dominant pollutant's AQI + category
  (e.g. `AirNow (PM2.5)`).  Requires `airnow_key` (free, 500 req/hour).
  Ranked **#1** for `air_quality` as the authoritative US source.
- **`weather_providers/purpleair/`** - crowdsourced real-time PM2.5 from
  the nearest outdoor PurpleAir sensor (bounding-box query around the
  geocoded point).  Applies the EPA/Barkjohn (2021) humidity correction
  and converts to AQI with the **2024** EPA PM2.5 breakpoints
  (`_codes.pm25_to_aqi`).  Requires `purpleair_key` (free read key).
  Ranked **last** for `air_quality` (crowdsourced, noisier); surfaces
  sensor distance in the source label for provenance.
- New per-command flags: `-airnow`/`-an` and `-purpleair`/`-pa` (work
  with `.aqi`/`.air`, hidden until their key is configured).

### Added - weather subsystem expansion (5 new capabilities, 14 new providers)

- **New capabilities + commands:** `.uv`/`.uvi` (UV index), `.pollen`/`.allergy`
  (Europe/CAMS), `.wildfire`/`.fire` (active fire detections), `.space`/`.aurora`
  (geomagnetic Kp + aurora chance), `.tides`/`.tide` (next high/low). Each adds a
  normalized dataclass (`UVResult`, `PollenResult`, `WildfireResult`,
  `SpaceWeatherResult`, `TideResult`), a `CAPABILITY_METHODS` entry, and a
  `DEFAULT_RELIABILITY` ranking.
- **New air-quality sources:** WAQI/aqicn (`-waqi`), OpenAQ v3 (`-openaq`/`-oaq`),
  IQAir AirVisual (`-iqair`/`-iq`). Open-Meteo AQI now also reports `aerosol_optical_depth`
  (smoke proxy).
- **Astronomy:** SunriseSunset.io (`-ss`, no key) - full moon-phase + twilight set;
  now ranked first for `.astro`.
- **UV:** Open-Meteo `uv_index` + currentuvindex.com (`-cuv`, no key).
- **Alerts:** GDACS global multi-hazard (`-gdacs`) and ECCC Canada (`-eccc`), both no key.
- **Historical:** NASA POWER (`-nasapower`/`-power`, no key, global reanalysis).
- **Wildfire:** NIFC WFIGS (US, no key) + NASA FIRMS (`-firms`, global active-fire).
- **Space weather:** NOAA SWPC (no key) - planetary Kp + OVATION aurora grid.
- **Tides:** TideCheck (`-tc`, global) + NOAA CO-OPS (`-coops`, US, no key).
- **General fallback:** MET Norway / Yr (`-metno`/`-yr`, no key) for
  current/forecast/hourly/alerts/nowcast; Open-Meteo now also serves `nowcast`
  (`minutely_15`), `uv`, and `pollen`.
- Provider count is now **32 packages** across 14 capabilities (incl. the
  AirNow / PurpleAir air-quality pair in the entry above). New secret keys:
  `waqi_token`, `openaq_key`, `iqair_key`, `tidecheck_key`, `firms_key`.

### Changed - `.help` system: consistency, accuracy, flood-safety

- New shared `modules.base.help_row(prefix, usage, desc)` formatter; **all
  command modules migrated to it** so `.help <module>` output aligns
  uniformly (previously each module hand-padded to a different column, 18–50)
  and renders correctly in both monospace and proportional IRC clients.
- Normalized alias notation to `.cmd/.alias` everywhere (was a mix of
  `.cmd/.alias` and `.cmd / .alias`); surfaced previously-hidden short
  aliases (`.numberfact/.nf`, `.recipe/.meal`, `.reddit/.r`).
- **Weather `.help` rewritten compact**: 14 commands grouped into themed
  bold-labelled rows + a summarized provider line (count + `-l` pointer
  instead of dumping every provider flag), so it no longer scales with the
  provider count. Restored the `.providers` admin line.
- `.help <module>` now shows the command count in its header.
- All help replies remain token-bucketed by `sender.py` (5 burst, ~40/min) -
  well inside a 10-msg/3-sec flood limit; `help_row` keeps every line far
  under the 512-byte IRC limit.
- New `tests/test_help.py` regression suite (160 checks): every module's
  primary commands must be documented, every line IRC-safe (length + indent),
  alias separators normalized - prevents help/command drift.

## [3.0.0] - 2026-05-20

### Changed - secret-store consolidation (BREAKING for fresh setups)

- **`config.ini` is now gitignored**; `config.ini.example` is the
  committed credential-free template.  The old separate `secrets.ini`
  is gone - its `[secrets]` section is now appended to the bottom of
  `config.ini` itself (still 0o600, still falls back to the OS keyring,
  still overridden by `INTERNETS_<NAME>` env vars).  Rationale: a flat
  0o600 file beside a flat 0o644 file isn't meaningfully more secure
  than one 0o600 file holding both; the split mostly created friction.
- **`secret_store.py`** - `SECRETS_FILE` now points at `config.ini`.
  `set`/`delete` perform **text-based in-place edits** of the
  `[secrets]` section (the old configparser round-trip stripped every
  comment in the file).  `init` copies `config.ini.example → config.ini`;
  `--force` is now a wholesale overwrite (the old configparser-based
  merge was incompatible with comment preservation).  `migrate` auto-
  chmods `config.ini` to 0o600 before writing, and `_scrub_config_ini`
  is now section-aware so it never blanks the very `[secrets]` entries
  it just populated.
- **Migrating an existing install:**
  `cd ~/your-bot-dir && { echo; cat secrets.ini; } >> config.ini && shred -u secrets.ini && chmod 600 config.ini`
- **`modules/numberfact.py`** - rewritten as a Wikipedia / local-math
  hybrid because numbersapi.com is defunct (it 301-redirects to
  `rembrandtpublishing.com/<path>` which 404s).  `math` facts are now
  computed locally; `date` (MM/DD) and `year` use Wikipedia's REST
  On-This-Day and page-summary endpoints; `trivia` uses the number's
  article summary with a math-fact fallback when Wikipedia returns
  the boilerplate "natural number following X and preceding Y"
  extract.  The `.numberfact` / `.nf` command surface is unchanged.

### Removed (BREAKING)

- **OS keyring backend removed.** `secret_store` is now two-tier:
  `INTERNETS_<NAME>` env var → `config.ini[secrets]` (0600).  The bot
  targets headless deployments where `keyring` has no usable backend
  ("fail" backend), and the optional desktop-session integration
  dragged in ~10 transitive dependencies (`keyring`, `jeepney`,
  `secretstorage`, `jaraco-*`, `importlib-metadata`, `zipp`,
  `more-itertools`) for no practical benefit.  `requirements.lock`
  drops from 33 to 23 packages.  The `--backend` flag on
  `secret_store set` / `delete` / `migrate` is gone (only one backend
  remains).  If you stored secrets in the OS keyring, move them into
  `config.ini[secrets]` before upgrading.
- **`python -m secret_store get --reveal`** - the `--reveal` flag is
  gone.  Printing a stored secret to stdout was a real exposure surface
  (terminal scrollback, shell history, screen recording) and CodeQL's
  `py/clear-text-logging-sensitive-data` query correctly flagged the
  data flow - closing the alert by suppression would have been hiding
  a finding that wasn't actually a false positive.  The same operator
  use case (manual key rotation) is now explicit at the call site:

      python -c "import secret_store; print(secret_store.get('omdb_key'))"

  The CLI's `get <name>` still prints `(set, N chars, backend=...)`.

### Fixed - concurrency, auth lifecycle & privacy (14-discipline audit)

- **Admin-session laundering on identity change.**  A `NICK` change
  *migrated* the authenticated session to the new nick (and `QUIT` left
  it dangling).  A malicious server or a nick-takeover could launder an
  admin session onto an attacker-chosen identity.  Auth is now
  **revoked** on both `NICK` and `QUIT` - re-authentication required.
- **Cross-thread races on `_nick_hosts` / `_chanops`.**  Both dicts were
  mutated on the event-loop thread but read from `to_thread` workers
  (`is_admin`, `is_chanop`) with no lock - a torn read or "dict changed
  size during iteration" crash.  `_nick_hosts` is now guarded by
  `_auth_lock`, `_chanops` by a new `_chanops_lock`.
- **`_nick_hosts` grew unbounded** - every nick that ever spoke was
  retained forever (no eviction on `QUIT`).  Now dropped on `QUIT`.
- **No dead-connection detection.**  The bot sent keepalive `PING`s but
  never tracked the `PONG` reply; a half-open TCP link sat idle for the
  full 300 s read-timeout.  `_keepalive` now records inbound `PONG`s and
  forces a reconnect after 240 s of silence.
- **`.forgetme` was an incomplete right-to-erasure** - it wiped only the
  saved location and channel user-tracking, leaving `.seen`, `.tell`,
  `.notes`, and `.remind` data intact.  A `forget(nick)` hook was added
  to `BotModule` and implemented by all four PII modules; `.forgetme`
  now calls it on every loaded module.
- **`BotModule.__init_subclass__`** validates the `COMMANDS` → handler
  contract at class-definition time - a typo'd method name or a
  non-coroutine handler is now an ImportError at startup, not an
  `AttributeError` the first time a user runs the command.
- **`modules/calc.py`** - `**` capped only the exponent, so a huge base
  (`(10**300)**9999`) could still build a 100k-digit integer.  The
  estimated result bit-length is now bounded too.

### Fixed

- **`modules/poke.py`** - raise the response cap from 256 KB to 1 MB so
  gen-1 Pokémon (Mewtwo ≈ 425 KB, Charizard ≈ 343 KB, Charmander ≈ 299 KB)
  no longer hit "PokéAPI response too large".  Also strip leading zeros
  on numeric IDs so `.poke 06` resolves to `#6` (Charizard) instead of
  404'ing against `/pokemon/06`.
- **`modules/numberfact.py` - CPU-DoS via unbounded `n`.**  `.nf <n>
  math` / `.nf <n>` parsed an arbitrarily large integer and ran O(√n)
  trial division - a 19-digit input measured at ~90 s of CPU on a
  worker thread.  Explicit `n` is now clamped to 10¹² (√n ≤ 10⁶).
- **Streamed HTTP responses were never closed** - `fetch_json`
  (`modules/base.py`) and the inline `stream=True` sites in `poke`,
  `numberfact` (×3), `idlerpg`, `fml`, `search` left the socket open
  on every path, leaking file descriptors over long uptimes.  All are
  now wrapped in `with requests.get(...) as r:`.
- **`config.py`** - a missing/unreadable `config.ini` now fails with an
  actionable `SystemExit` ("run `python -m secret_store init`") instead
  of a bare `KeyError: 'irc'` deep in import.
- **`secret_store.delete()`** - no longer swallows `PermissionError`;
  `_delete_file_secret` raises on a non-0600 file so a delete blocked
  by bad perms is reported as an error, not silently as "not found".
- **`modules/search.py`** - `_web_sync` / `_image_sync` logged provider
  failures only at `debug`, so on a default `INFO` level a bad Brave
  key or DDG markup drift produced no log line at all.  Both now
  `log.warning` each provider failure; `_image_sync` distinguishes
  "no key configured" from "the keyed call failed".
- **`modules/units.py`** - km/h→mph used the imprecise divisor `1.609`;
  now `1.609344` (exact), matching `km_mi`.
- **Windows: `UnicodeDecodeError` reading `config.ini`** - pin
  `encoding="utf-8"` on every `configparser.read()` call site
  (`config.py:reload_config`, `secret_store.py` ×4).  Python's default
  on Windows is cp1252, which choked on the non-ASCII characters
  in `config.ini.example`'s section headers - broke every Windows test
  job at import-time.

### Security

- **`audit_log.py` - HMAC-keyed hash chain.**  The tamper-evident audit
  chain used plain SHA-256, which anyone with a copy of `audit.log`
  could recompute to forge entries (the algorithm is in the repo).  It
  is now HMAC-SHA-256 under a 32-byte key auto-generated into a 0600
  sidecar (`audit.log.key`) - a leaked log alone can no longer be
  forged.  Records carry `"v": 2`; pre-3.0.0 entries still verify
  (legacy SHA-256 fallback).  The log also rotates to
  `audit.log.<timestamp>` past 5 MB instead of growing unbounded.
- **`modules/seen.py` - retention pruning.**  Passively-collected
  last-seen entries were kept forever; now pruned past `max_age_days`
  (default 180), on load and on every flush - mirrors `store.py`'s
  user-tracking prune.
- **`scripts/regen-lockfile.sh`** now requires Python 3.10 specifically
  and fails loudly otherwise - the lock must resolve on the lowest
  supported Python so `python_version < "3.11"` conditional transitives
  (e.g. `async-timeout`) are captured; a lock built on 3.14 silently
  omitted them.
- **Test coverage** - new `tests/test_fetch_json.py` pins the
  `fetch_json` size-cap boundary, the 404 paths, and malformed-JSON
  handling; `tests/test_secret_store.py` gains mid-file `[secrets]`
  edit + newline-injection tests; `tests/test_modules_base.py` covers
  the `BotModule.forget` hook and the `__init_subclass__` `COMMANDS`
  validator.
- **HTTP response size caps everywhere** - added `fetch_json(url, *, ua,
  …, max_bytes=256 KB)` to `modules/base.py` and migrated every module
  that called `requests.get(...).json()` through it: `imdb`, `dictionary`,
  `urbandictionary`, `lastfm`, `twitch`, `stocks` (×6), `steam` (×3 -
  GetOwnedGames bumped to 1 MB for power users), `search`, `youtube`,
  `urls` (is.gd).  `idlerpg` (XML) and `fml` (HTML scrape) inlined the
  same stream + cap pattern.  Twitch's OAuth POST got an inline 16 KB
  cap too.  Closes the OOM / JSON-bomb gap a third-party-API audit
  flagged (the rest of the codebase already followed this pattern via
  `r.raw.read(MAX_BODY_BYTES + 1, decode_content=True)`).
- **`modules/idlerpg.py`** - use `defusedxml.ElementTree` instead of the
  stdlib parser for 3rd-party IdleRPG XML (Bandit B314 - XXE / billion-
  laughs hardening).
- **`metrics.py`** - annotate the all-interfaces refusal guard with
  `# nosec B104` (the literals appear as a defensive *check*, not a
  bind target; false positive).
- **`secret_store.py`** - strip the secret *name* from the keyring-
  failure debug log (CodeQL `py/clear-text-logging-sensitive-data` was
  flagging the identifier).
- **`weather_providers/__init__.py`** - replace WeatherKit's
  "missing: <names>" log with a count-only message (same CodeQL query
  was flagging the comprehension that bound key+value tuples).
- **Random-pick sweep** - every `random.choice` / `random.randint` /
  `random.uniform` call site routed through `random.SystemRandom`
  (`internets.py`, `modules/bofh.py`, `modules/dice.py`, `modules/fml.py`,
  `modules/numberfact.py`, `modules/xkcd.py`).  Clears Bandit B311
  across the codebase without per-line suppressions.
- **`except Exception: pass` → debug log** in five hot paths
  (`internets.py` shadow-ban prefix parse and stdin-close on shutdown,
  `admin_cmds.py` `_state_file`, `modules/tell.py` async-save scheduler,
  `modules/seen.py` temp-file cleanup).  Same best-effort semantics,
  but now observable in `--log-level=debug`.  The remaining ~25 broad
  `except Exception: pass` sites (best-effort cleanup, fallback paths)
  are annotated with `# nosec B110: best-effort cleanup` instead of
  changed - they're intentional swallows with no observability gain.
- **`assert` → `raise RuntimeError`** at two invariant checks that
  would otherwise be stripped by `python -O` (Bandit B101):
  `process_lock.py:_read_existing` and `weather_providers/_http.py:_get_session`.
- **`# nosec B105`** on `weather_providers/weatherkit/__init__.py:105`
  (`self._token = ""` is JWT-cache init, not a hardcoded password -
  `_headers()` regenerates the token on first use).
- **`# nosec B404 / B603 / B606`** on `internets.py`'s Windows
  self-restart path (`subprocess.Popen` + `os.execv` with
  `sys.executable` + `sys.argv` - interpreter-controlled, not user input).
- **`secret_store._cmd_list` rewritten** with explicit if/elif branches
  mapping the (taint-tracked) backend code to a literal display label -
  CodeQL's data-flow analysis now sees `print(label)` as printing one
  of four constants, breaking the `py/clear-text-logging-sensitive-data`
  false positive that fired on the previous `print(f"{name} {backend}")`.
- **`.github/workflows/security.yml`** -
  `pip-audit -r requirements.lock` (audit only third-party deps, not
  the local editable `internets-irc` install which has no PyPI entry),
  `--ignore-vuln PYSEC-2025-183` (disputed pyjwt CVE; the alleged weak
  encryption is the application's key-length choice, not the library -
  Apple WeatherKit picks the key for our usage).
- **`gitleaks-action`** - `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`
  opts into Node 24 early; v2.3.9 still ships Node 20 and GitHub
  retires Node 20 in Sep 2026.
- **`secret_store.set_value()`** rejects a CR/LF in the value - the
  file backend writes `name = value` as one line, so an embedded
  newline could inject a fake section/key into `config.ini`.
- **`.gitignore`** - added `seen.json`, `tells.json`, `notes.json`,
  `reminders.json` (per-module PII state files that were not ignored).
- Removed 7 now-dead `import requests` lines left behind by the
  `fetch_json` migration; removed the OS-keyring transitive deps from
  `requirements.lock` (jeepney, secretstorage, jaraco-*, etc.).

## [2.6.0] - 2026-05-20

### Added - 24 new modules

- **IRC-native stateful** (use `on_raw` hook + own JSON store, atomic
  0o600 writes):  `seen`, `tell`, `remind`, `notes`.
- **Stateless API toys** (no key required):  `poke` (PokéAPI), `dnd`
  (D&D 5e SRD), `mtg` (Scryfall), `iss` (ISS tracker + crew), `xkcd`,
  `apod` (NASA APOD - `DEMO_KEY` fallback), `cocktail` (TheCocktailDB),
  `recipe` (TheMealDB), `hn` (Hacker News), `reddit` (subreddit top
  post), `numberfact` (NumbersAPI), `bored` (Bored API).
- **Pure-local utilities** (no network):  `games` (`.coin` `.8ball`
  `.rps` `.choose`), `devutils` (`.b64` `.unb64` `.hex` `.morse`
  `.uuid` `.epoch`), `qr` (api.qrserver.com URL builder), `httpcode`
  (HTTP status code table), `cowsay`.
- **Live data:**  `crypto` (CoinGecko spot + 24h change, no key -
  command renamed to `.gecko` / `.cg` to coexist with the keyed
  `stocks.crypto` Finnhub/AV/TD command), `fx` (frankfurter.dev
  ECB rates), `spacex` (next launch + countdown + rocket + pad).

### Added - 10 new admin commands

- `.raw <line>` - inject a raw IRC protocol line (CR/LF/NUL rejected,
  510-byte cap, audit-logged).
- `.say [target] <text>` / `.act [target] <text>` - speak / CTCP
  ACTION as the bot (target defaults to current channel).
- `.nick <newnick>` - change bot nick at runtime (RFC-2812 validated,
  `_nick` updates on the server NICK echo).
- `.uptime` - process uptime + current-connection uptime.
- `.stats` - counters (cmds dispatched, PRIVMSG in/out), sender queue
  depth, modules loaded/configured, audit log size, RSS memory.
- `.audit [N | grep <pat> | tail | verify]` - view the audit log;
  `verify` re-walks the SHA-256 chain.
- `.fingerprint <nick>` - cross-reference everything the bot knows
  about a nick: hostmask, channels, shadow-ban status, last `.seen`,
  `.tell` counts, `.notes` count, audit-log mentions.
- `.shadow-ban <nick> [reason]` / `.shadow-unban <nick>` /
  `.shadow-list` - silently drop ALL traffic from a nick (commands +
  `on_raw` fanout); persisted to `shadow_bans.json` (0o600).

### Changed

- **`.help` redesigned for progressive disclosure** - the default view
  is now ~8 lines (a wrapped module roster + drill-down hints) instead
  of 30+.  `.help <module>` shows that module's full command list,
  `.help <cmd>` shows the one-liner, `.help admin` shows the admin
  grid, `.help all` is the escape hatch for the full alphabetical
  command grid.  Canonical alias = first key in each module's
  `COMMANDS` dict (insertion order), not the longest.
- **Module lookup before command lookup** in `.help <target>`, so
  `.help weather` shows the whole module rather than collapsing to
  the single `.weather` line.

### Fixed

- **`modules/qdb.py`** - extract the real numeric quote ID from the
  bash-org-archive permalink anchor instead of falling back to the
  literal placeholder `"qdb"` (was producing `[qdb qdb] ...` lines).
- **`modules/fml.py`** - rewritten for fmylife.com's Tailwind layout
  (the old `article-link` / `article-contents` selectors are gone).
  Regex anchors on the `block text-blue-500` class so it captures the
  full body instead of the short category tag-line (`Magic underwear`,
  `Knackered`, etc.).

## [2.5.0] - 2026-05-19

- Per-provider weather flags (`-nws`, `-aw`, `-vc`, `-om`, …) plus `-l` for
  a ranked-by-accuracy listing of currently-active providers.
- Provider chain now sorts by scientific accuracy first, then by live
  health score, then by registration order.
- Stormglass and WeatherBit providers wired into the dispatcher.
- Tiered secret store (`secret_store.py`): env → OS keyring → 0600
  `secrets.ini`.  Replaces plaintext keys in `config.ini`.
- `config.local.ini` overlay for personal non-secret settings.
- `is_configured()` hook on `BotModule` - `.help` and weather `-l` hide
  modules / providers without their key.
- Per-process lockfile (`process_lock.py`); circuit breaker on provider
  health; per-provider quota counter; geocoding TTL cache.
- Tamper-evident admin-action audit log (`audit_log.py`).
- Optional Prometheus exporter (`metrics.py`, off by default).
- DNS-pinned SSRF adapter in `modules/urls.py`.
- Hardened XML parser in `modules/qdb.py` via `defusedxml`.
- `.forgetme`, `.privacy`, `.optout`, `.optin` user commands.

## [2.4.0] and earlier

See git history.
