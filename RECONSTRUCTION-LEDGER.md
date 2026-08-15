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
- [ ] internets.py
- [x] admin_cmds.py
- [x] sender.py
- [x] protocol.py
- [x] console.py
- [ ] config.py
- [ ] botlog.py
- [x] store.py
- [ ] secret_store.py
- [ ] hashpw.py
- [ ] audit_log.py
- [ ] process_lock.py
- [ ] metrics.py

Packages:
- [ ] weather_providers/ (5 files)
- [ ] modules/ (75 files, batched by help-menu category)
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
