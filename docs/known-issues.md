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
containing `str(exception)` for each one. A `requests` transport error embeds the
full request URL, and these providers carry credentials in the query string
(`token=`, `apikey=`). A network outage while keys are configured therefore
prints the keys into the channel.

`sender.redact_secrets` does not save this: it applies to log output, not to
`PRIVMSG` bodies.

**Verified:** reproduced directly. A connect timeout to a `token=` URL yields
exception text containing the token.

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

The template autoloads 67 modules, six of which record user-derived data:
`seen`, `tell`, `linktitle`, `notes`, `remind`, `steam`. `privacy` is not among
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

**Verified:** parsed the template's autoload list; confirmed the six collectors
present and `privacy` absent.

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
including `audit_log.record()` and all six module JSON stores. `store.py`'s three
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
