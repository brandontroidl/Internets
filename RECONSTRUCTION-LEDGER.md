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
