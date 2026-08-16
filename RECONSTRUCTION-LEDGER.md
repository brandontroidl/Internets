# Documentation reconstruction ledger (working state - branch docs-reconstruction only)

Resume pointer for the full documentation reconstruction (spec given 2026-08-15).
This file is working state, removed before any merge to main. One section per phase;
flip statuses in place. Findings accumulate at the bottom and must not disappear.

## Plan of record

- P1 Layer 2 internals (docs/internals/): every source file documented.
- P2 Layer 1 enterprise docs: verify/rewrite 16 existing, create ~11 missing
  (irc-protocol, command-reference, operations, administration, state-and-persistence,
  logging-and-auditing, metrics-and-observability, writing-providers, integrations,
  testing, troubleshooting; real getting-started to replace the 11-line stub).
- P3 verification: repo-wide citation CONTENT verification (not remap), cross-document
  consistency audit, link/anchor audit, programmatic count checks, Sphinx build green.
- P4 findings ledger consolidation + completion report (spec section 41).
- Deviation from spec (deliberate): diagrams use graphviz directives, not Mermaid -
  the repo's Sphinx toolchain renders graphviz; Mermaid would not build.
- Citation style going forward: symbol-primary (`file.py - Class.method()`), line
  numbers secondary only.

## P1 internals coverage checklist

Root files (13):
- [x] internets.py
- [x] admin_cmds.py
- [x] sender.py
- [x] protocol.py
- [x] console.py
- [x] config.py
- [x] botlog.py
- [x] store.py
- [x] secret_store.py
- [x] hashpw.py
- [x] audit_log.py
- [x] process_lock.py
- [x] metrics.py

Packages:
- [ ] weather_providers/ (5 files)
- [~] modules/ (75 files): A,B done; C-I in flight/queued
- [ ] tests/ (behavior map, test-gap inventory)
- [ ] scripts/ + .github/ + packaging (pyproject, requirements, CI workflows)

## Module batch plan (P1)

A: base, _netsafe, units, example, __init__ (module API contract, deep)
B: geocode, location, weather (weather IRC side + geocoding)
C: ipintel, secinfo, dnsutils, probe, ipinfo, netcalc, httpcode (net/sec)
D: devtools, devutils, encode, calc, mathx, physcalc, numberfact (dev/math)
E: reflookup, dictionary, urbandictionary, translate, search, scholar, pkginfo, ghinfo
F: scinews, astro2, iss, apod, spacex, satpass, hn, reddit, xkcd (news/science/space)
G: imdb, lastfm, youtube, mtg, poke, dnd, recipe, cocktail, steam, twitch, idlerpg,
   crypto, fx, stocks (media/finance)
H: remind, tell, seen, notes, channels, urls, privacy, linktitle, qdb, health (social/util)
I: bofh, cowsay, fact, catfact, chuck, dadjoke, advice, bored, games, dice, fml, qr (fun)

Tooling added: scripts/gen-command-reference.py (generated command inventory +
--check drift gate). Ground truth 2026-08-15: 165 primary module commands,
4 core public, 23 core admin.

## P2 Layer 1 status

- [ ] not started (blocked on P1 outputs for grounding)

## P3 verification status

- [ ] not started

## Findings (accumulating; classified per spec section 40)

Implementation defect (VERIFIED by orchestrator against source):
- modules/health.py:134-135 reads `_dirty_locations`/`_dirty_channels`; Store's fields
  are `_dirty_locs`/`_dirty_chans`, so `.health` permanently prints `?` for those two.
- store.py:220 `.bak` backup written via write_bytes with no chmod; first creation gets
  umask perms (typically 0644) while the main file is 0600 - PII world-readable in .bak.

Questionable (agent-reported, spot-plausible, not independently re-verified yet):
- store.py Store.user_join() comment overstates opt-out scope (no caller skips updates).
- store.py Store.user_rename() onto tracked nick discards target first_seen/opted_out.
- store.py Store._write() no fsync before os.replace (durability caveat, recoverable).

Test gap:
- tests/test_store.py: no coverage for corruption quarantine, v1->v2 upgrade, opt-out
  API, user_purge, user_rename collision, RateLimiter.channel_check/_cleanup.

Fixed during reconstruction (orchestrator, verified):
- admin_cmds.py cmd_mode docstring (added 2026-08-15 same session) claimed
  ".mode <target> <modes>"; handler only sets modes on the bot itself. Corrected.

Agent-reported (admin_cmds batch): cmd_auth comment overstates fsync (record() does not
fsync); shadow-ban save iterates set in worker thread without lock (silent skip on
concurrent mutation); cmd_rehash bad-hash abort leaves partial rehash unaudited;
cmd_audit reads up-to-5MB log synchronously on the loop; _clean_actor truncation and
cmd_help fallback branches untested.

Agent-reported (sender/protocol/console batch): sender pri-0 eviction can drop pri-0
(docstring overclaims), relies on private PriorityQueue._queue (no canary test),
closing-writer discards spend tokens with no drop accounting, 50ms token poll;
protocol parse_isupport_prefix() return value discarded by its only caller while
parse_names_entry hard-codes prefix sets (non-standard PREFIX desyncs chanop tracking);
console: no test file at all, _print_status reaches into private fields, console
events not routable by the per-subsystem debug facility it controls.

Agent-reported (secret_store/hashpw batch): secret_store module docstring claims
"encryption-at-rest" - implementation is plaintext + 0600, stale keyring-era claim;
perms_ok() equality check refuses 0400 (stricter-than-0600 silently falls to defaults);
set_value() rejects CR/LF in value but not name; sasl_password KNOWN_SECRETS entry has
no consumer and its documented fallback does not exist; hashpw _FAST_HASH_THRESHOLD_S
comment describes auto cost-backoff that is not implemented; scrypt/argon2 hash fns do
not enforce MAX_PASSWORD_BYTES in-function; _verify_scrypt maps MemoryError to silent
False; botlog _VALID_HASH_PREFIXES is a hand-maintained duplicate of verify_password's
set; tests/test_hashpw.py has a stale "DOCUMENTED RESIDUAL" docstring contradicting the
implemented verify-side guard; secret_store CLI handlers largely untested.

Security concern (VERIFIED by orchestrator): audit_log.py - AuditLog.verify()
dispatches hash scheme on the record's own `v` field; records rewritten as
legacy (no `v`) verify with plain keyless SHA-256, so a writer to audit.log can
rewrite the chain from any position and verify() reports intact. Downgrade
attack on tamper evidence. Caveat: requires write access to audit.log (0600);
severity depends on where the HMAC key lives relative to the log. FINDING ONLY -
no unilateral fix; owner decides (e.g. reject legacy records after first v2, or
a cutover index pin).

Implementation defect (agent-verified, regex probe): internets.py _handle_cap /
_RE_CAP mishandles multiline CAP LS 302 (the `*` continuation marker parsed as a
cap token, leading colon kept on first cap, each LS line answered independently
- premature CAP REQ/END can fire mid-list) despite the bot requesting 302.
Also: CAP ACK branch replaces _caps instead of unioning (second ACK discards
prior grants); request_shutdown() before run() strands _shutdown_initiated with
no event so signals are ignored.

Agent-reported (audit/process_lock batch): rotation stamp 1s granularity +
silent rename overwrite can destroy a rotated segment; record() never fsyncs
(two code comments claim otherwise - both stale); verify/.audit never read
rotated segments; process_lock stale-reclaim (read/unlink/O_EXCL) not atomic -
two starters can interleave and both acquire; start_time recorded but unused.
Test gaps: no end-to-end dispatch test (tests/test_dispatcher.py actually tests
weather_providers/_dispatch.py, not bot dispatch); _handle_cap, shadow-ban
filter, keepalive timeout, reconnect loop untested; no multi-thread record()
test; no concurrent stale-reclaim test.

Owner policy (STATED 2026-08-15): superseded docs -> docs-archive/ (git mv, same
commit as the replacement), destined offsite; no parallel old/new doc sets.

Agent-reported (metrics/botlog/config batch): six of ten default metrics have no
update call site (constant 0 - built-but-not-wired); expose() "loopback-only"
claim vs actual unspecified-only guard; single-threaded exporter, stalled scraper
blocks; get_hash() lives in botlog (placement); apply_loglevel does not clear
subsystem debug sets (rehash does); reload_config() skips import-time validation
(empty command_prefix hazard live after rehash); CONFIG_PATH resolves against
CWD; parse_args() at import exits during import (argv-pinning convention).

Owner requirement (STATED 2026-08-15): docs must be PRINTABLE - everything,
including docs/internals/, wired into the Sphinx toctree so it lands in the
xelatex PDF build. P3 gate: scripts/build-docs.sh must produce BOTH HTML and
PDF green; check the PDF for overflowing tables/code blocks (LaTeX-specific
failure HTML never shows).

Agent-reported (batch A, module contract; fec0 check done on live interpreter):
base.resolve_public() passes IPv6 site-local fec0::/10 while _netsafe.ip_is_blocked
blocks it - the two SSRF guards disagree (security concern, surface to owner);
fetch_json scalar timeout bounds per-read not wall time (slow-drip holds a worker
past cancel); cred() catches only ImportError; units.deg_to_card/fmt_dt/fmt_short
have zero production callers (provider tree carries its own deg_to_card);
example.py teaches an admin-bypass on API cooldown that does not exist and
overstates input bounding; _netsafe docstring understates its dependents.
Test gaps: safe_open hop-limit + no-Location branches; compressed-body cap.

Agent-reported (batch B, weather/geocode/location): weather docstrings undercount
commands (8 listed vs 15 registered; "Seven lines" vs 8); cmd_alerts double-resolves;
-n <nick> flag only matches as the entire arg; geocode Nominatim paths run blocking
r.raw.read() on the event loop (only _get offloaded; _zippo shows the right pattern);
transport failures cached as 24h negatives same as not-found; no process-wide 1req/s
Nominatim throttle (per-user gates only); location.cmd_regloc logs nick-to-location
pairs into the bot log where .forgetme cannot purge them (PRIVACY concern - surface);
regloc/myloc skip rate_limited() before geocoding. Test gaps: -n opt-out refusal,
pollen flag aliases, geocode cache TTL/LRU/negative round-trip, location handlers.

Agent-reported (batch E, reference/lookup): arXiv fetched over plain http (only
cleartext URL in module); rate-limit gate AFTER usage reply in dictionary/
urbandictionary/translate/search (empty-arg spam bypasses limiter; other modules
gate first - inconsistent ordering); translate rides unofficial gtx endpoint;
search keyless path regex-scrapes unversioned DDG HTML; scholar split_flags
silently eats any -word token; pkginfo eager requests import contra lazy-import
pattern; ghinfo lacks the traversal guard pkginfo has and 403 rate-exhaustion is
unmessaged. Test gaps: dictionary/urbandictionary/translate/search fully
untested; scholar handler layer; reflookup rtfm parser.

Agent-reported (batch C, net/sec): probe .ssl mangles bare IPv6 literals (partition
at first colon); probe docstring states stale guard mechanism; ipinfo rides cleartext
http to ip-api.com (free-tier constraint, on-path forgeable); dnsutils accepts ten
record types but advertises six; non-ASCII dash/arrow glyphs (U+2013/U+00D7/U+2192)
in dnsutils/netcalc/secinfo replies - conflicts with owner no-dash preference;
weather_user_agent secret doubles as bot-wide HTTP UA across five modules (naming
drift). Test gaps: secinfo .pwn PM-only refusal unpinned (the guard against password
amplification into a channel), HIBP/Tor size-cap branches, no test_ipinfo/netcalc.

Agent-reported (batch F, science/space; moon math spot-checked live): reddit
rate-gate after replies (same ordering class as batch E group); astro2 dead
negative-age branch and Meeus-not-Fliegel docstring; scinews topic list drift
(no cs/astro tags, six others undocumented) and last-link-wins Atom parsing can
fetch media enclosures; all-feeds-failed cached 120s; iss unguarded float() on
upstream coords escapes to dispatcher catch-all; iss plain-HTTP endpoints.
Test gaps: no tests at all for iss/apod/spacex/hn/xkcd/reddit.

Implementation defect (VERIFIED by orchestrator): mathx cmd_isprime runs
_isprime synchronously on the event loop (mathx.py:503; cmd_bignum uses
to_thread) and composites surviving 2^20 trial division fall into unbounded
_pollard_rho - a pasted 100-digit semiprime hangs the entire bot. Any-user DoS.

Agent-verified (batch D, by direct call): physcalc _rc_from_bands computes
5-band resistor codes wrongly (tolerance band consumed as multiplier;
first tolerance-pop condition provably dead) AND tests/test_physcalc.py
test_five_band asserts the defective value - change-detector locking a bug in.
calc reinterprets 1e10 as 1*e*10 (Euler); physcalc year divisor mixes sidereal
and Julian years (.ly self-inconsistent); encode .unicode ! unreachable;
mathx _bignum int_max_str_digits set/restore races process-global.

Agent-verified (batch H, social/util): privacy cmd_forgetme clears opt-out
before user_purge so an untracked user gets a false "tracking in 1 channel(s)
(erased now)" (the "*" sentinel row is counted); privacy docstring claims
PM-only but optout/optin never require PM; NO privacy command is rate-limited.
linktitle logs every announced/skipped URL with channel at INFO - user browsing
activity in the bot log (pairs with regloc PII log finding). notes mutates dict
unlocked while to_thread save iterates it; channels founder attribution can
misattribute overlapping verifications in the 15s window.
Test gaps: ALL TEN social/util modules have no test files.

Agent-reported (batch I, fun): qr advertised 1000-char cap collides with the
400-char strip_ctrl default so long inputs emit silently broken QR links
(agent-verified defect); bored's intended bold is stripped by its own
strip_ctrl call; bofh and dice skip rate_limited() (add to the cross-module
rate-gate inconsistency family); dice en-dash output pinned by tests;
nine modules carry a dead local _strip_ctrl alias; fml scraper coupled to a
Tailwind class string. Test gaps: entire batch except dice untested; games.py
absent from the async-handler contract test.

SECURITY DEFECT (VERIFIED empirically by orchestrator, reproduced): stocks.py
_try_providers appends str(exception) to the IRC "all providers failed" reply;
urllib3 transport errors embed the full request query including token=/apikey=,
so a network outage while keys are configured publishes every finance API key
to the channel. sender.redact_secrets is log-only and does not scrub PRIVMSG.
Fix shape (owner decision): errors.append name + exception class only, never
str(e); same class-only rule for the log.warning URL-bearing pattern the agent
found across imdb/lastfm/youtube/steam/twitch (log-only leak, lower severity).

Agent-reported (batch G, media/finance): steam _save_ids unbound-local masks
mkstemp errors; twitch search_streams dead code; twitch client_secret as URL
query params on OAuth POST [unverified vs current Twitch docs]; steam regsteam
mutates dict outside lock during to_thread save; dnd/crypto collapse outages
into "no match"; crypto/fx rate-gate BEFORE usage reply (inverse of the batch
E/F ordering drift - both directions exist); lastfm discards profile on
recenttracks failure. Test gaps: steam registry, twitch token lifecycle,
stocks failover order and its key-leaking aggregation.

OPERATIONAL DEFECT (VERIFIED by orchestrator via gh + lock header): CI Tests
workflow RED on main since 2026-08-13 (runs 31669351519, 31848012132,
31848249036). requirements.lock header says "pip-compile with Python 3.14" -
violates scripts/regen-lockfile.sh resolve-on-3.10 contract; marker-gated
typing_extensions>=4.4 absent, so every Python <3.13 CI leg fails the
--require-hashes install. Fix: regenerate lock on 3.10 per the script (owner
decision - dependency surface, not doc work). Related pre-existing memory item:
bcrypt 4.3.0-installed vs 5.0.0-pinned env drift.
Secondary: tests.yml Windows legs swallow the install failure (three pip
commands in one pwsh run: block, no fail-fast) and fail later confusingly.

Agent-reported (tests/CI batch): pyproject asyncio_mode=auto + pytest-asyncio
dev extra contradicts the suite's manual-loop convention (async tests would
no-op locally, run in CI); FakeBot re-declared in 7 places with no fidelity
check; pip-audit covers lock only (extras floors unchecked); lint job
hand-enumerates py_compile files (same shape as the v3/v4 broken-wheel
py-modules omission); 44 of ~90 modules without behavioral tests; no
end-to-end dispatch test; console.py untested; both entry points excluded
from the coverage gate.

RECON GAP (orchestrator's own error, corrected): initial recon globbed
weather_providers/*.py and counted 5 files. The 32 provider implementations
live in sub-packages weather_providers/<id>/ - 135 files, 4427 lines, NOT
covered by the first assignment. Assigned separately (batches WP1-WP4).

Agent-reported (weather_providers core): record_call docstring contradicts
Dispatcher.dispatch (which calls it every attempt); record_call counts attempts
not HTTP requests so multi-hop providers under-count quota; _f_pollendotcom
reads [weather_providers].weather_user_agent while modules/weather.py reads
[weather].user_agent (ini-only installs send empty UA); weatherapi/weatherstack
monthly caps stored in per-day limit field; derive_missing humidity==0 hits
log(0) ValueError and is mis-scored as provider failure; DEFAULT_RELIABILITY
ranks meteomatics for nowcast (not implemented) and omits stormglass from
current (silent rank 99 - the exact shape the metno test exists to catch);
test_every_registered_capability_is_ranked uses an empty ConfigParser so keyed
providers never register and that gap escapes; _session_cache keyed by id(loop)
never evicted with one module-level lock shared across loops; two independent
stream-and-cap implementations with different defaults (1MiB vs 256KiB).
Provider count 32 verified three ways (dirs, _reg() calls, pinned test).

Count drift (orchestrator-verified, to fix in Layer 1 rewrite):
- README.md:42 says "72 command modules"; actual = 70 modules registering
  commands (75 .py files in modules/, minus __init__, base, geocode, units,
  _netsafe which register none). Verified by instantiating BotModule subclasses.
- README.md:505 "pytest suite is 40 modules" is CORRECT (40 tests/test_*.py).
- docs/modules.md:221 "~20 of 32 providers are key-gated" - verify against the
  provider factories during the modules.md rewrite.
Ground truth for this reconstruction: 70 command modules, 165 primary module
commands, 4 core public + 23 core admin commands, 32 weather providers,
40 pytest files (1738 passed / 3 skipped) + run_tests.py (213 passed).

Implementation defect (VERIFIED by orchestrator): noaa_coops/tides.py fetch()
requests date=today and takes the FIRST "H" and FIRST "L" of the day with no
time filtering, populating next_high_time/next_low_time. For most of the day
".tides" reports extremes that already passed. Same pattern in tidecheck
(trusts undeclared upstream ordering, never filters past extremes).

Agent-reported (WP4 government/specialist providers): noaa_coops re-downloads
the ~3500-station list (8MB cap) per call, no cache; nearest-station is
great-circle only so inland points resolve to coastal stations; swpc takes
data[-1] as latest Kp without checking time_tag and downloads the ~2MB OVATION
grid per request; swpc aurora distance ignores the 0/359 longitude seam;
nifc counts non-wildfire categories as active fires (requested fields never
read), ignores exceededTransferLimit so busy regions truncate silently, and
returning empty outside US coverage STOPS the dispatch chain so non-US queries
never fall through to firms; firms counts detection pixels not fires and
discards the confidence column; gdacs prefers htmldescription so raw HTML can
reach an IRC line, and its Green/Orange/Red level fills a CAP-severity field
making severity ranking a no-op; sunrisesunset sends no timezone or date
parameter while claiming local times, and an empty results object counts as a
dispatch success. Naming corrections: capability methods are
get_space_weather/get_astronomy; nasapower implements get_historical (met means
only, no irradiance); gdacs implements generic get_alerts.

=====================================================================
ARCHITECTURAL DEFECT - CROSS-CUTTING (VERIFIED by orchestrator)
Dispatcher fallback is silently disabled for 11 of 13 capabilities.
=====================================================================
_dispatch.py:417 decides "provider returned nothing, try the next one" with:
    if result is None or (hasattr(result, "is_empty") and result.is_empty())
Only WeatherResult and HourlyResult implement is_empty(). Enumerated live:

  has is_empty : WeatherResult, HourlyResult
  LACKS it     : AlertsResult, AirQualityResult, AstronomyResult,
                 HistoricalResult, MarineResult, NowcastResult, UVResult,
                 PollenResult, WildfireResult, SpaceWeatherResult, TideResult

For those 11 capabilities a hollow result counts as SUCCESS, ends the chain,
and no lower-ranked provider is ever tried. Each provider looks correct in
isolation; the defect lives in the interaction, which is why per-file review
kept reporting it as separate bugs. Independently-reported symptoms that are
all THIS one cause:
  - tomorrowio alerts returns empty AlertsResult on 401/403 (free tier) and
    thereby suppresses nws/gdacs/eccc alerts  <- SAFETY RELEVANT: severe
    weather warnings silently not shown
  - nws marine returns all-None MarineResult, suppressing openmeteo waves
  - openweathermap air_quality returns hollow result on empty list
  - openmeteo astronomy has no moon fields, shadowing weatherapi (rank 3)
  - nifc empty outside US coverage stops fallback to firms
  - sunrisesunset status OK with empty results counts as success
Note the codebase already KNOWS this shape: tests/test_provider_fixes.py pins
a tomorrowio air_quality fix that raises instead of returning empty. The fix
was applied per-provider instead of at the contract.
Fix shape (OWNER DECISION, not applied): give every *Result an is_empty(), or
invert the dispatcher guard to require an explicit non-empty signal. Either is
a behavior change to a live weather path - not a documentation edit.
=====================================================================

Agent-reported (WP1/WP2 forecast providers): metno hour labels UTC while other
providers are location-local; weatherkit sends its Apple bearer JWT to a
detailsUrl taken from the response body with no host allowlist; weatherkit
signs ES256 + reads the .p8 key synchronously on the event loop; weatherstack
invalid-key envelope returns status=None so mark_auth_failure never fires and a
dead key costs a request per dispatch; meteomatics/stormglass literal "Current"
description permanently blocks fill_gaps; accuweather _LOC_CACHE has no TTL so
a wrong key is permanent; worldweatheronline raises on no-data (trips breaker)
where stormglass was already fixed away from that pattern; multiple
truthiness-guard bugs dropping legitimate 0 values (nws pressure, wwo
visibility, weatherbit high/low, tomorrowio weather code); quota docstring
conflicts (weatherstack 250 vs 1000/mo, pirateweather 20k vs 10k, weatherbit
500/day vs 50).

LEDGER SELF-CORRECTION (agent caught orchestrator error): earlier entry said
"two code comments claim fsync". Verified: exactly ONE (admin_cmds.py:270).
The second stale claim is different - audit_log.py module docstring says
"append-binary mode" while record() opens text mode "a". Both real, distinct.

Implementation defect (VERIFIED by orchestrator): modules/health.py registers
"uptime", which is also in AdminCommandsMixin._CORE. _dispatch() resolves _CORE
first, so health's public .uptime is permanently unreachable while its
help_lines() still advertises it. Only shadowed name across the whole command
set (checked programmatically against every module's COMMANDS).

Implementation defect (VERIFIED): internets.py _save_shadow_bans passes
_shadow_bans/_shadow_ban_reasons to json.dump in a to_thread worker with NO
lock, while cmd_shadow_ban/cmd_shadow_unban mutate them on the loop. Same
family as notes.py/steam.py. Repo-wide: os.fsync appears NOWHERE (verified) -
no writer fsyncs, including all six module JSON stores.

Agent-reported (state/logging/metrics): module stores (seen/tells/notes/
reminders/steamids/shadow_bans) have no checksum envelope and no quarantine -
corrupt file is loaded as empty then overwritten; only store.py's three
datasets are recoverable. config.ini.example omits [tell]/[notes]/[remind]
file keys and [bot] shadow_bans_file though code reads them. config.ini is
fail-closed at 0600 while internets.log - which holds the location/URL privacy
leaks - gets umask default with no check. .forgetme cannot reach .bak,
.corrupt.*, rotated audit segments, or rotated logs. cmd_rehash's `if lvl:`
guard silently skips the entire logging reset AND the reply on an invalid
level (or NOTSET==0). audit_log docstring names the key sidecar audit.key;
code derives <path>.key i.e. audit.log.key. commands_total counts dispatch
attempts, not completions. linktitle logs URL+channel but NOT nick (ledger
wording refined: per-channel, not per-user).

Agent-reported (irc-protocol/command-reference): inbound PING matched with
startswith("PING") so a prefixed PING would go unanswered (asymmetric with
PONG handling); _CHAN_RE hard-codes #&+! instead of reading ISUPPORT
CHANTYPES (only CHANMODES and PREFIX are consumed from 005); channels.cmd_users
is admin-gated for hostmask PII but its help entry advertises it as public;
config.py CLI epilog implies --debug takes multiple subsystems, apply_debug
takes exactly one.

Internals-doc corrections applied (conflicts found by Layer 1 agents, verified
by orchestrator, source wins): tests.md 41->40 test files, ~90->75 modules
denominator, 2706->2705 run_tests lines; scinews.md ~130->173 feed URLs;
health.md now documents .uptime as unreachable instead of public.

Agent-reported (integrations/testing): nasa_api_key is read by apod.py and
astro2.py but registered in NEITHER KNOWN_SECRETS nor CONFIG_LOCATIONS
(verified) - works via get() and INTERNETS_NASA_API_KEY but is invisible to
`secret_store list`/status() and migrate will not relocate it. weather_user_agent
is read by 49 of 75 module files as the global UA and is a FAIL-CLOSED gate:
geocode._ua_has_contact() disables all Nominatim geocoding when it lacks an
@domain or http(s):// - so .w/.regloc/.myloc fail with no obvious cause;
in the providers layer it reaches exactly one provider and _http.py injects no
UA at all. config.ini.example defines none of [imdb][lastfm][youtube][stocks]
[twitch][search][ipintel][satpass][apod], so every per-module ini fallback in
the code is unreachable on a fresh install. weatherstack docstring still calls
it plaintext HTTP - the modules were fixed to https (comments confirm the old
access_key-in-cleartext leak). pirateweather/_codes.py safe_get_json ALREADY
implements the key-redaction shape that stocks.py needs - fix precedent exists
in-repo. CONTRIBUTING.md says tests.yml has three jobs; it has four.

Agent-reported (operations/admin/troubleshooting): numerics 403/405/471/474/
475/476 drop a channel from active_channels and rewrite channels.json with NO
log line (473 does log) - channels vanish across restarts with only the file as
evidence; corrupt shadow_bans.json degrades to empty with a warning, so an
unclean restart silently un-bans everyone.

CONFLICT resolved (source wins): internals/internets.md claimed the ACCOUNT
branch writes an audit record; no audit_log.record() call exists in
internets.py at all (only admin_cmds.py writes audit records). To fix in P3.

Agent-reported (WP3 air quality/pollen/UV; all 32 providers now documented):
purpleair applies EPA/Barkjohn correction coefficients (defined on pm2.5_cf_1)
to the ATM variant the API returns for the generic pm2.5 field - wrong variant,
worst during smoke episodes (agent cited Barkjohn 2021 + PurpleAir API docs);
openaq never reads parameter.units so gas readings in ppm/ppb are labelled
ug/m3 by weather.py; pollendotcom makes an uncached direct Nominatim reverse
call per invocation, bypassing the geocode cache that exists for Nominatim's
policy; airnow quota 500 is per-HOUR stored in a per-day counter (~24x
understated); waqi/iqair signal bad key inside a 200 body so the dispatcher's
401/403 auth fast-trip never fires and a dead credential burns a request per
dispatch forever; airnow/waqi raise on no-coverage (health failure, breaker
trips) while pollen providers return None for the same condition; waqi
interpolates lat/lon into the URL path rather than params; currentuvindex
"today" is the UTC day so peak-UV is wrong for offset users.
TWO MORE INSTANCES of the is_empty() defect class: openaq (station without
PM2.5 -> aqi=None accepted as success, stops chain, prints "AQI N/A" while
openmeteo/iqair behind it are never tried) and pollendotcom (non-coercible
index -> chain stops, prints "No pollen data" with openmeteo untried).
REFINEMENT of my earlier UA finding: the two ini sections do diverge, but
pollen.fetch() fails OPEN with a hardcoded non-identifying UA while
weather.py fails CLOSED - not "sends an empty UA" as first recorded.

SECURITY DEFECT (VERIFIED by orchestrator): botlog.py:217 logs
"config.ini is world-readable - consider: chmod 640 config.ini" while
secret_store.perms_ok() requires mode == 0o600 EXACTLY and fails closed.
An operator who follows the bot's own printed advice makes [secrets]
unreadable; the bot then runs keyless with one error line. Self-contradicting
guidance between two subsystems. Pairs with the already-recorded 0400 finding
(stricter modes also refused).

Ledger precision fix (agent-verified): the architectural defect block says
"11 of 13 capabilities". Correct form: CAPABILITY_METHODS has 14 entries and
base.py has 13 result dataclasses; 11 of 13 result types lack is_empty(),
covering 11 of 14 capabilities (current/forecast/hourly are covered).

Agent-reported (providers rewrite): DEFAULT_RELIABILITY["air_quality"] ranks
accuweather at 10 though AccuWeatherProvider defines no get_air_quality (inert,
same class as the meteomatics/nowcast entry) - found by hasattr sweep over all
32 provider classes; config.ini.example provider_priority lists 30 of 32 ids
(pollendotcom and google_pollen absent) under a comment claiming to list all 32.
Programmatic ground truth: 32 providers, 12 keyless / 20 keyed.

P3 CITATION VERIFICATION (content-based, not line remapping):
Built scripts/verify-doc-citations.py - parses each cited file's AST and
confirms the cited SYMBOL exists (incl. instance attributes assigned in
methods), with path-affinity resolution for ambiguous basenames (base.py,
__init__.py, air_quality.py exist many times over).
Instrument was corrected twice before its verdict was trusted: (1) arbitrary
basename resolution produced 72 false failures; (2) instance attributes and
prose placeholders produced 3 more. Trap-tested: a deliberately broken symbol
and an out-of-range line are both caught, then removed.
RESULT: 1026 citations - 864 symbol citations VERIFIED, 0 failures, 0 missing
files, 0 out-of-range line citations. 162 legacy line-number citations remain
(47 of them in deployment.md, which is still queued for rewrite); they are
range-valid but cannot be content-verified mechanically.
Doc citation errors found and FIXED by this pass: internets.py - main ->
_main (4, console.md); internets.py - _handle_line -> _process (4, across
linktitle/seen/tell); modules/weather.py - _format_air_quality -> _format_aqi
(2, openaq.md); modules/weather.py - Weather.__init__ ->
WeatherModule.on_load() (pollendotcom.md).

Agent-reported (security-model/configuration rewrite): [seen] max_age_days IS
configurable (old configuration.md claimed hardcoded - internals/seen.md was
already right); non-boolean [metrics] enable ABORTS startup (getboolean sits
outside the try that catches host/port errors); health.cmd_health tells a
non-admin to "try .uptime instead" which is ALSO admin-gated - and health's own
public .uptime is the shadowed registration, so both halves are dead ends;
config.ini.example also omits [seen]; KNOWN_SECRETS=41 vs CONFIG_LOCATIONS=40
(sasl_password is the orphan); provider classification refined: 20 key-gated,
11 keyless, 1 (pollendotcom) UA-gated but unconditionally registered.
