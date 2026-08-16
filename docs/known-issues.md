# Known issues

Defects and concerns found by reading the source during the 2026-08 documentation
reconstruction. Nothing here has been fixed: each item changes runtime behavior,
and the decision to change it belongs to the maintainer. The documentation
elsewhere in this corpus describes what the code **does**, and links here rather
than describing broken behavior as working.

Each entry gives the symbol, what happens, how it was verified, and the shape a
fix would take. "Verified" means reproduced or confirmed against source by
reading it, not inferred from a comment.

Severity is judged on user impact, not on how hard the fix is.

---

## 1. Provider failures publish API keys to the channel

**Symbol:** `modules/stocks.py - _try_providers()`

When every configured finance provider fails, the handler builds an IRC reply
containing `str(exception)` for each one. A `requests` error embeds the full
request URL, and these providers carry credentials in the query string
(`token=`, `apikey=`), so the key is printed into the channel.

The trigger is not rare. `raise_for_status()` renders the prepared URL, so an
expired, revoked, or quota-exhausted key produces exactly this on the next
`.stock`. That makes the defect self-amplifying: the most likely moment for
every provider to fail at once is a key rotation, and a botched rotation
publishes the replacement key the same way. Verified output shape:
`401 Client Error: Unauthorized for url: https://finnhub.io/api/v1/quote?symbol=AAPL&token=<key>`.

`sender.redact_secrets` does not save this: it applies to log output, not to
`PRIVMSG` bodies.

**Verified:** reproduced directly, on both a connect timeout and a 401, each
yielding exception text containing the token.

**Fix shape:** aggregate `type(e).__name__` and the provider name, never
`str(e)`. `weather_providers/pirateweather/_codes.py - safe_get_json()` already
implements this pattern in-repo and is the model to copy. The same
URL-bearing-exception habit appears in `log.warning` calls in `imdb`, `lastfm`,
`youtube`, `steam`, and `twitch`; those leak to the log rather than the channel,
so they are lower severity but the same defect.

---

## 2. Weather fallback is disabled for eleven of fourteen capabilities

**Symbol:** `weather_providers/_dispatch.py - Dispatcher.dispatch()`

The guard that decides "this provider returned nothing, try the next one" is:

```python
if result is None or (hasattr(result, "is_empty") and result.is_empty()):
```

Only `WeatherResult` and `HourlyResult` implement `is_empty()`. The other eleven
result dataclasses in `weather_providers/base.py` do not, so an empty result from
those providers counts as **success**, ends the chain, and no lower-ranked
provider is tried.

The most serious consequence is alert suppression. `.alerts` ranks providers with
`nws` first, but `weather_providers/tomorrowio/alerts.py - fetch()` deliberately
degrades a 401/403 (its alerts endpoint is paid-tier) into an empty
`AlertsResult`. On a free Tomorrow.io key that empty result ends the chain and
severe-weather alerts from every other provider are silently not shown.

Six further symptoms share this one cause: `nws` marine returning an all-`None`
`MarineResult` and hiding Open-Meteo waves; `openweathermap` air quality on an
empty list; `openmeteo` astronomy having no moon fields and shadowing
`weatherapi`; `nifc` returning empty outside US coverage instead of falling
through to `firms`; `openaq` printing "AQI N/A" with working providers untried;
`pollendotcom` printing "No pollen data" the same way.

**Verified:** enumerated the dataclasses live. 2 of 13 implement `is_empty()`;
11 of 13 do not, covering 11 of the 14 entries in `CAPABILITY_METHODS`.

**Fix shape:** two options. Add `is_empty()` to the remaining eleven result
types, or invert the dispatcher guard so a result must positively signal
non-emptiness to end the chain. The second fails closed and cannot be forgotten
by the next result type someone adds. Note the codebase already applied the
per-provider version of this fix once: `tests/test_provider_fixes.py` pins a
Tomorrow.io air-quality path changed to raise rather than return empty.

---

## 3. `.isprime` can hang the entire bot

**Symbol:** `modules/mathx.py - MathxModule.cmd_isprime()`

The handler calls `_isprime()` synchronously on the event loop; the sibling
`cmd_bignum` correctly uses `asyncio.to_thread`. A composite that survives
`_smallest_factor`'s 2^20 trial-division cap falls into `_pollard_rho`, which has
no iteration bound. A pasted 100-digit semiprime (the input cap allows it) stalls
every user's commands, not just the caller's.

The 60-second command timeout does not help: `asyncio.wait_for` cannot interrupt
a synchronous call already running on the loop.

**Verified:** read the call path; `cmd_isprime` has no `to_thread`, and
`_pollard_rho`'s outer `while True` has no attempt limit.

**Fix shape:** wrap in `asyncio.to_thread` like `cmd_bignum`, and bound
`_pollard_rho` by attempts or wall time.

---

## 4. The shipped autoload template collects data but omits the privacy module

**Symbol:** `config.ini.example` `[bot] autoload`

The template autoloads 67 modules, seven of which record user-derived data:
`seen`, `tell`, `linktitle`, `notes`, `remind`, `steam`, and `location` (which
stores saved locations through the core store rather than its own file, and
logs nick-to-location pairs). `privacy` is not among
them. A deployment that copies the template verbatim therefore tracks users and
ships no `.forgetme`, `.optout`, `.optin`, or `.privacy` command. The erasure
mechanism exists and works; it is switched off by default while collection is
switched on.

Two related gaps make the erasure incomplete even when `privacy` is loaded:

- `.forgetme` cannot reach the bot log. `modules/linktitle.py` logs announced
  URLs with their channel at INFO, and `modules/location.py - cmd_regloc()` logs
  nick-to-location pairs. Neither is erasable, and `internets.log` is written
  with the default umask while `config.ini` is fail-closed at 0600.
- `modules/privacy.py - cmd_forgetme()` clears the opt-out flag before calling
  `Store.user_purge()`, which makes `Store.set_opt_out()` create a `"*"` sentinel
  row that the purge then counts. An untracked user is told "tracking in 1
  channel(s) (erased now)".

**Verified:** parsed the template's autoload list and confirmed `privacy` is
absent. On the count: five modules keep their own store (`seen`, `tell`,
`notes`, `remind`, `steam`); `location` stores through the core store; and
`linktitle` persists nothing but writes announced URLs to the log. All seven
are autoloaded.

**Fix shape:** add `privacy` (and `health`) to the template autoload; decide
whether the two log sites should log at DEBUG, omit the identifier, or be
covered by a log-scrubbing pass.

---

## 5. Audit-chain verification accepts downgraded records

**Symbol:** `audit_log.py - AuditLog.verify()`

Each record declares its own version. `v: 2` records are verified with HMAC under
the audit key; records without a `v` field fall back to plain SHA-256 so that
pre-3.0.0 logs still verify. Because the record chooses which scheme validates
it, an actor who can write `audit.log` can rewrite the chain from any position as
unversioned records and `verify()` still reports the chain intact - no key
required.

Two further limits on tamper evidence: `verify()`, `.audit`, and the record
counter read only the live `audit.log` and never the rotated segments, so a
rotated segment can be altered or deleted undetected; and rotation stamps to
one-second granularity with `Path.rename()`, which silently overwrites, so two
rotations within the same second destroy the earlier segment.

**Verified:** read `verify()`; the scheme is selected by `obj.get("v")` with no
positional constraint.

**Fix shape:** refuse a legacy record once any `v: 2` record has been seen, or
pin a cutover index. Note this bounds the damage rather than eliminating it:
an actor holding both the log and its key can still truncate the tail, which
needs an external append-only sink to detect.

---

## 6. CI has been red on `main` since 2026-08-13

**Symbol:** `requirements.lock` header vs `scripts/regen-lockfile.sh`

The lockfile header records that it was generated with Python 3.14, but the
regeneration script's contract is to resolve on 3.10 (the lowest supported
version) precisely so that marker-gated transitive dependencies are captured.
Resolving on 3.14 dropped `typing_extensions>=4.4`, which `aiohttp` needs below
Python 3.13, so every Python <3.13 leg fails `pip install --require-hashes`.

A second defect hides the first on Windows: the workflow's install step runs
three `pip` commands in one `run:` block, and under `pwsh` there is no fail-fast,
so the failing install reports success and the job fails later in pytest with a
confusing `ModuleNotFoundError`.

**Verified:** `gh run list` shows failures on the last three `main` pushes; the
lockfile header states Python 3.14 and contains no `typing_extensions`.

**Fix shape:** regenerate the lock on 3.10 per the script. Separately, split the
install step or set `$ErrorActionPreference` so Windows fails fast.

---

## 7. The bot's own advice breaks its secret store

**Symbol:** `botlog.py` startup permission warning vs `secret_store.py - perms_ok()`

On a world-readable config the bot logs: `config.ini is world-readable -
consider: chmod 640 config.ini`. But `perms_ok()` requires mode **exactly**
`0600` and fails closed otherwise. An operator who follows the printed advice
makes `[secrets]` unreadable, and the bot then runs keyless with a single error
line.

The same equality check refuses modes that are *stricter* than 0600: a
read-only `0400` config also fails, and `get()` silently returns defaults.

**Verified:** read both sites; the advice string says 640, the check compares
`mode != 0o600`.

**Fix shape:** change the advice to `chmod 600`, and relax the check to "no group
or other bits" rather than exact equality.

---

## 8. Tide times are the day's first extremes, not the next ones

**Symbol:** `weather_providers/noaa_coops/tides.py - fetch()`

The request asks for `date=today` and the parser takes the first `H` and first
`L` of the returned day, assigning them to `next_high_time` and `next_low_time`
with no filtering against the current time. For most of the day `.tides` reports
extremes that already passed. `weather_providers/tidecheck/tides.py` has the same
shape, trusting an undeclared upstream ordering.

**Verified:** read the request parameters and the selection loop.

**Fix shape:** filter to extremes later than now, and request tomorrow as well so
a late-evening query still has a next high and low.

---

## 9. `.uptime` from the health module never runs

**Symbol:** `modules/health.py - HealthModule.COMMANDS` vs
`admin_cmds.py - AdminCommandsMixin._CORE`

Both register `uptime`. `IRCBot._dispatch()` resolves `_CORE` first, so the
module's public handler is unreachable while its `help_lines()` still advertises
it. `IRCBot.load_module()` only checks module-against-module collisions, so
nothing warns at load time.

The core `.uptime` is admin-gated, which makes the dead end complete:
`HealthModule.cmd_health()`'s refusal message tells a non-admin to "try `.uptime`
instead", and that command is also refused.

**Verified:** compared `_CORE` against every module's `COMMANDS` map
programmatically; `uptime` is the only shadowed name in the whole command set.

**Fix shape:** rename or remove the module registration, and extend the loader's
collision check to cover `_CORE`.

---

## 10. `.health` cannot read two of the fields it reports

**Symbol:** `modules/health.py - HealthModule.cmd_health()`

It reads `store._dirty_locations` and `store._dirty_channels`; the attributes are
named `_dirty_locs` and `_dirty_chans`. Both are fetched with a `getattr`
default, so the command prints `?` for those two datasets permanently instead of
failing.

**Verified:** compared both names against `store.py`.

**Fix shape:** correct the two attribute names. This is the cheapest fix in this
document and a good argument for a test on `.health`, which currently has none.

---

## 11. PurpleAir applies its correction to the wrong measurement

**Symbol:** `weather_providers/purpleair/_codes.py - epa_correct()` with
`weather_providers/purpleair/air_quality.py` `_FIELDS`

The EPA/Barkjohn correction coefficients are defined on the `pm2.5_cf_1`
variant, but the request asks for the generic `pm2.5` field, which the PurpleAir
v1 API returns as the ATM variant for outdoor sensors. The correction is applied
to a measurement it was not derived for, and the divergence between the two
variants is largest during heavy smoke, which is when the reading matters most.

Related: no QC is applied. The `confidence` field and the A/B channel pair are
available in the same response and are not requested, so a single failing sensor
drives the whole reading.

**Verified:** read the requested field list against the correction function.

---

## 12. Concurrency and durability gaps

Grouped because each is the same shape: state mutated on the event loop while a
worker thread serializes it, with no lock.

| Symbol | Issue |
|---|---|
| `internets.py - _save_shadow_bans()` | `_shadow_bans` / `_shadow_ban_reasons` are dumped in a `to_thread` worker with no lock at all while the shadow-ban commands mutate them |
| `modules/notes.py - NotesModule._do_add()` | mutates `_notes` outside `_lock` while `_save_notes()` serializes it |
| `modules/steam.py - SteamModule.cmd_regsteam()` | mutates `_ids` outside `_lock` during a concurrent save |

In each case a mid-iteration mutation raises inside the worker and the save is
silently skipped.

Separately: **`os.fsync` appears nowhere in the codebase.** No writer syncs,
including `audit_log.record()`, the five module-owned JSON stores (`seen`,
`tell`, `notes`, `steam`, `remind`), and the core-owned `shadow_bans.json`. `store.py`'s three
datasets survive a torn write through their checksum envelope and quarantine
path; the module stores have no envelope, so a corrupt file is logged, loaded as
empty, and overwritten by the next save.

---

## 13. Lower-severity items

Carried here so they are not lost. Each is confirmed against source.

- **`store.py - Store._write()`** writes `<name>.bak` with `Path.write_bytes`
  and no `chmod`, so the backup of a 0600 PII file gets umask-default
  permissions on first creation.
- **`base.py - resolve_public()`** permits IPv6 site-local `fec0::/10`, which
  `modules/_netsafe.py - ip_is_blocked()` blocks. The two SSRF guards disagree;
  verified on the live interpreter.
- **`modules/qr.py - QRModule.cmd_qr()`** advertises a 1000-character input cap
  that cannot be reached (the dispatcher refuses arguments over 400) and then
  truncates its assembled URL at `strip_ctrl`'s 400-char default, emitting a
  broken QR link.
- **`_dispatch.py - DEFAULT_RELIABILITY`** ranks `meteomatics` for `nowcast` and
  `accuweather` for `air_quality`; neither implements the capability. Conversely
  `stormglass` implements `get_weather` but is absent from the `current` table,
  so it silently sorts at rank 99. The test that should catch this configures an
  empty `ConfigParser`, so no keyed provider registers and the gap escapes.
- **Six of ten default metrics have no update call site** and render as constant
  zero: `provider_calls_total`, `provider_quota_used`, `module_loaded`,
  `provider_active`, `sender_queue_depth`, `authed_admins_count`. Do not build a
  dashboard on them.
- **`config.py - reload_config()`** does not re-apply import-time validation, so
  a `.rehash` can install an empty `command_prefix` - the state the startup guard
  exists to prevent - and every channel message becomes a command.
- **`admin_cmds.py - cmd_rehash()`** guards its logging reset with `if lvl:`,
  so an invalid level (or a literal `NOTSET`, which is `0`) silently skips the
  reset, the subsystem clear, and the confirmation reply.
- **A failed `.reload` leaves the module unloaded.** `reload_module()` unloads
  then loads; a syntax error in the file being edited deregisters its commands
  with nothing to restore them.
- **`secret_store.py`** has no registry entry for `nasa_api_key`, which
  `modules/apod.py` and `modules/astro2.py` both read. It works via `get()` and
  its environment override, but it is invisible to `secret_store list` and
  `migrate` will not relocate it. Its module docstring also still claims
  "encryption-at-rest"; storage is plaintext under 0600.
- **`secret_store.py - _cmd_init()`** reports `len(text)` from a `str` as a byte
  count, so `python -m secret_store init` announces roughly 13370 bytes for a
  file that is 14296 bytes on disk (the template contains non-ASCII box-drawing
  characters). Cosmetic.
- **`config.ini.example`** omits sections the code reads: `[tell] file`,
  `[notes] file`, `[remind] file`, `[seen]`, `[bot] shadow_bans_file`, and the
  per-module sections `[imdb] [lastfm] [youtube] [stocks] [twitch] [search]
  [ipintel] [satpass] [apod]`.
- **Numerics 403/405/471/474/475/476** drop a channel from `active_channels` and
  rewrite `channels.json` with no log line (473 does log), so channels vanish
  across restarts with only the file as evidence. A corrupt `shadow_bans.json`
  degrades to empty with a warning, silently un-banning everyone.
- **Timezone handling is inconsistent across providers.** Several label hours in
  UTC or in the bot host's zone rather than the queried location's:
  `metno` hourly and nowcast, `pirateweather`, `weatherkit`, `tomorrowio`,
  `meteomatics`, `currentuvindex` ("today" is the UTC day),
  `weatherapi` astronomy (uses the host's date).
- **Providers disagree on the no-data contract.** `worldweatheronline` and
  `airnow` raise on an uncovered location, which records a health failure and can
  trip the circuit breaker; `nws/_scope.py` and the pollen providers return
  `None` and take no penalty. `nws` is the reference implementation.

---

## 14. A packaged install cannot load modules or bootstrap its config

**Symbols:** `config.py` `MODULES_DIR`, `internets.py - IRCBot.load_module()`,
`pyproject.toml` package data

`MODULES_DIR` defaults to the relative path `modules`, resolved against the
current working directory, and `load_module()` loads by file path from it. A
wheel or sdist install puts the package under `site-packages`, so on a
package-only install every autoload entry fails with `'modules/<name>.py' not
found` until `[bot] modules_dir` is pointed at the installed location by hand.

Compounding it, `config.ini.example` is in neither the wheel nor the sdist, and
`secret_store.py - _cmd_init()` reads that template from the working directory,
so `python -m secret_store init` cannot bootstrap a packaged install either.

**Verified:** confirmed `MODULES_DIR`'s CWD-relative default in `config.py` and
the absence of `config.ini.example` from the built wheel in `dist/`.

**Fix shape:** resolve `MODULES_DIR` against the package location when the
CWD-relative path does not exist. This is the same hand-enumerated-packaging
failure family as the `py-modules` omission that shipped broken wheels in 3.0.0
and 4.0.0.

The repository currently holds two positions on the missing template.
`scripts/verify_install.sh` states it is "deliberately not shipped in the wheel -
the bot is installed from a checkout", which would make the packaged install an
unsupported shape rather than a defect. Decide which is intended and make the
other artifact agree: either ship the template as package data, or say in the
README and `docs/deployment.md` that a wheel install is not a supported
deployment.

---

## 15. The bot log is unprotected and holds the data `.forgetme` cannot reach

**Symbols:** `botlog.py` handler setup, `modules/linktitle.py`,
`modules/location.py - cmd_regloc()`

`config.ini` is fail-closed at 0600, but `internets.log` is created with the
default umask and no permission check or warning. It carries nick-to-location
pairs from `.regloc` and announced-URL-plus-channel records from `linktitle`,
neither of which `.forgetme` can erase, and rotated log segments are outside its
reach as well.

**Verified:** read the handler construction and both logging sites.

**Fix shape:** set `UMask=0077` in the service unit as an immediate mitigation;
longer term, drop the identifiers from those two log lines or move them to
DEBUG.

---

## 16. The most privacy-invasive admin command is the only unaudited one

**Symbol:** `admin_cmds.py - AdminCommandsMixin.cmd_fingerprint()`

`.fingerprint` cross-references everything the bot knows about a nick: hostmask,
channel membership, the `seen` record including its stored message context, tell
counts, note count, and audit-log mentions. It reads `seen.json`, `tells.json`,
and `notes.json` directly.

Nineteen call sites in `admin_cmds.py` write an audit record. This handler is
not one of them, so the one command that assembles a complete profile of a user
leaves no trace that it was run or against whom.

The output is delivered to the admin by notice rather than to the channel, so
this is an accountability gap rather than a disclosure one.

**Verified:** counted `_audit()` call sites by walking the file's AST (19) and
confirmed none falls inside this handler.

**Fix shape:** call `self._audit(nick, "fingerprint", target)` like every
sibling. Consider whether the target nick belongs in the record, which is itself
a privacy decision.

---

## 17. Two retention controls disagree about what zero means

**Symbols:** `store.py - Store.__init__()`, `modules/seen.py - SeenModule._prune_stale()`,
`config.py` logging handler construction

Three retention knobs, three behaviors at zero:

| Setting | Zero means |
| --- | --- |
| `[bot] user_max_age_days` | floored to 1 day; pruning cannot be disabled |
| `[seen] max_age_days` | pruning disabled entirely, records kept forever |
| `[logging] max_bytes` | rotation disabled; the log grows without limit |

The first fails safe. The second and third fail open, and the third compounds
the log-privacy issue in item 15: the file that holds unerasable user data is
also the one whose growth bound can be silently switched off.

`[seen]` has no section in `config.ini.example` at all, so its 180-day default
is the shipped behavior and an operator cannot change it from the template.

**Verified:** read all three sites; confirmed `[seen]` absent from the template.

**Fix shape:** decide one convention for zero across retention settings and
apply it. If zero is to mean "disabled", say so in the template next to each
key; if it is to mean "default", floor it as `store.py` does.

---

## 18. Opting out creates a record that never expires

**Symbols:** `store.py - Store.set_opt_out()`, `store.py - Store._prune_users()`

Calling `.optout` for a nick the bot has never seen creates a sentinel row in a
synthetic `"*"` channel, and the pruner skips any row flagged `opted_out`. The
exemption is correct in intent: pruning an opt-out flag would silently re-enrol
the user. The consequence is that exercising a privacy control creates the one
record in the store with unbounded retention, removable only by `.forgetme`.

This is also why `.forgetme` miscounts for an untracked user, per item 4.

**Verified:** read both methods.

**Fix shape:** none required if documented, and it now is (see the privacy
notice and `docs/data-retention.md`). If a bounded form is wanted, keep opt-out
flags in a separate store from the tracking rows.

---

## 19. Outbound and observability blind spots

Grouped: each is a place where the system does something the operator cannot
see. Verified against source.

- **Queued output is discarded uncounted on reconnect.**
  `internets.py - IRCBot._connect()` builds a fresh `Sender`, and
  `sender.py - Sender.start()` assigns a new queue. Anything still queued from
  the disconnected period is garbage-collected rather than dropped through
  `_safe_put()`, so the drop counters never see it. Treat
  `internets_dropped_messages_total` as a floor, not a count.
- **The thread pool behind `asyncio.to_thread` is unbounded by any project
  setting and invisible.** Nothing calls `loop.set_default_executor()`, so the
  default pool is `min(32, cpu_count + 4)` - five workers on a single-core host.
  Blocking handlers queue behind it with no metric, log line, or command
  exposing depth, while the task cap of 50 suggests far more headroom than
  exists.
- **Geocoding runs outside the weather chain budget.** `modules/weather.py`
  awaits `geocode()` before the dispatcher starts its 45 s chain budget. The
  worst case is several sequential Nominatim requests at 10 s each, which can
  approach the 60 s command timeout on its own. The cache makes this rare rather
  than bounded.
- **The provider circuit breaker counts failures, not latency.** A provider that
  answers successfully but slowly never trips it while consuming most of the
  chain budget on every command. `ProviderHealth.avg_latency` is already
  computed and simply not exported.
- **Audit write failure is unmeasurable.** `audit_log.py - AuditLog.record()`
  increments its counter only after a successful append, and the caller swallows
  the error at warning level. A full disk looks identical to an idle bot at the
  exporter.
- **`.help all` costs about a minute of the global outbound budget.** The grid
  is roughly 44 to 50 lines; at a burst of 5 then one per 1.5 s it takes near a
  minute to arrive and occupies a quarter of the 200-slot queue. The handler
  returns immediately because `preply` only enqueues, so the command timeout
  never applies and nothing records the delivery delay.
- **`on_raw` fans out to every loaded module on the event loop for every
  inbound line.** With the shipped autoload that is roughly 67 calls per line,
  of which four modules do any work; the rest inherit a no-op.
- **`metrics.py` implements only counters and gauges.** Any latency objective
  needs a new metric type, not just a new call site.

---

## 20. Twitch replies carry upstream data with no sanitization

**Symbol:** `modules/twitch.py`

The module imports `BotModule`, `fetch_json`, and `help_row` from `.base` and
nothing else. It has no local sanitizer, so `display_name`, `game_name`, and
`title` from the Twitch API are interpolated straight into bolded reply lines.

Every other module that echoes upstream text passes it through
`modules/base.py - strip_ctrl()` or a local equivalent. Control bytes in a
hostile or compromised upstream response therefore reach IRC intact from this
module, where the sender strips only CR, LF, and NUL. Colour and formatting
codes survive, and so does anything that abuses them to forge the shape of a
line.

**Verified:** read the import line and the three interpolation sites.

**Fix shape:** import `strip_ctrl` and apply it to every upstream-derived field,
matching the convention the other modules already follow. The broader lesson is
in `docs/output-conventions.md`: sanitization is opt-in, and nothing in the
loader or the dispatcher enforces it.

Related smaller output defects found in the same sweep, none of them security
relevant: `modules/weather.py` uses U+03BC where `modules/physcalc.py` uses
U+00B5 for the same unit prefix (identical glyph, unequal comparison), and
`modules/remind.py` emits U+23F0, the only emoji in the bot's output.

---

## 21. The sanitizer completeness gate cannot see the modules it does not name

**Symbol:** `tests/run_tests.py`, the canonical-sanitizer test

The gate reads the source text of six hard-coded modules (`search`, `seen`,
`tell`, `stocks`, `remind`, `location`) and asserts the literal string
`strip_ctrl` appears in each, with `weather` allowed its own `_sanitize`. Its
comment says it "catches a future module (or a removed call) that drifts".

It cannot. Two limits:

- The list is fixed, so a module outside it is invisible. That is precisely how
  `modules/twitch.py` shipped with no sanitizer at all (item 20) while this
  gate stayed green.
- Within a listed module the assertion is a substring test over the whole file,
  satisfied by an import line, a comment, or a docstring. It proves a mention,
  not that every emitting path sanitizes.

**Verified:** read the test body.

**Fix shape:** enumerate the module population from disk rather than a literal
list, and assert on the emitting call sites rather than file text. That is the
same reverse-direction principle the documentation gates use: derive the
population, then check each member.

---

## Test gaps worth closing first

Not defects, but the reason several of the above went unnoticed.

- No end-to-end dispatch test exists: no test drives a raw IRC line through
  parsing, dispatch, a handler, and out to the sender. Both entry points
  (`internets.py`, `console.py`) are also excluded from the coverage gate, so the
  coverage number cannot reveal the hole.
- `console.py` has no test file at all.
- 44 of the 75 files under `modules/` have no behavioral test, including every
  module in the social and utility group, where the two privacy defects live.
- `tests/test_physcalc.py - TestRc.test_five_band` asserts the output of a
  miscalculated 5-band resistor decode, locking the defect in as expected
  behavior. This is the one place a test is actively harmful.
- `tests/test_dispatcher.py` tests `weather_providers/_dispatch.py`, not the
  bot's command dispatch, despite the name.
