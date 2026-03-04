# Security & Stability Audit

**Auditor:** Brandon Troidl
**Date:** 2026-03-02 (initial), 2026-03-03 (follow-up), 2026-03-04 (async architecture review, correctness pass)
**Scope:** Full codebase audit — `internets.py`, `sender.py`, `store.py`, `hashpw.py`, `protocol.py`, `config.ini`, and all modules in `modules/`.

All findings have been resolved. See `CHANGELOG.md` for the release-oriented summary of changes.

---

## First Pass — Functional Audit (2026-03-02)

### BUG-001: Remote Code Execution via `eval()` in Calculator Module

**Severity:** Critical
**File:** `modules/calc.py:21`
**Status:** Fixed

The calculator uses `eval()` with `{"__builtins__": {}}` as a sandbox. This is not a sandbox. The empty `__builtins__` dict has been a known bypass for over a decade. Any user in any channel the bot sits in can achieve arbitrary code execution on the host:

```
.cc ().__class__.__bases__[0].__subclasses__()[140].__init__.__globals__['system']('id')
```

The specific subclass index varies by Python version, but the technique is universal. This is a textbook RCE on an Internet-facing service.

**Resolution:** Replaced `eval()` with a recursive AST walker (`_safe_eval`) that only permits numeric literals, whitelisted math functions, and basic arithmetic operators. No attribute access, no builtins, no comprehensions, no string operations.

---

### BUG-002: Registration Commands Re-sent on Every `recv` Cycle

**Severity:** Critical
**File:** `internets.py:436-443`
**Status:** Fixed

The registration block (`PASS`, `CAP LS`, `NICK`, `USER`) is guarded by `if not identified`, but `identified` only becomes `True` after the bot sees numeric 376 (end of MOTD) or 422 (no MOTD). The server sends dozens of lines during MOTD. Each `recv` returns a chunk, the loop iterates, `identified` is still `False`, and the bot sends `NICK`/`USER` again. On a typical MOTD, this fires 10–30 duplicate registration attempts before the 376 arrives. Most IRCds will kill the connection for flooding.

**Resolution:** Added a `registered` flag that is set immediately after sending registration commands, independent of `identified`. Reset on reconnect.

---

### BUG-003: CAP LS Parsing Destroys All Capabilities After the First

**Severity:** Critical
**File:** `internets.py:499, 514`
**Status:** Fixed

The regex `re.split(r"[\s=][^\s]*", params)` is intended to strip `=value` suffixes from capability tokens like `sasl=PLAIN`. What it actually does is split on any whitespace-or-equals character followed by greedy non-whitespace, which consumes entire capability names:

```python
>>> re.split(r"[\s=][^\s]*", "multi-prefix sasl=PLAIN away-notify")
['multi-prefix', '', '']
```

Every capability after the first is destroyed.

**Resolution:** Replaced with `{cap.split("=", 1)[0] for cap in params.split()}`. Same fix applied to CAP NEW handler.

---

### BUG-004: Nick Collision Recovery Loops Forever

**Severity:** Critical
**File:** `internets.py:535`
**Status:** Fixed

```python
self._nick = self._nick.rstrip("_") + "_"
```

`rstrip("_")` strips all trailing underscores, then appends one. After the first collision (`Internets` → `Internets_`), a second 433 produces `Internets_` → `Internets_` (unchanged). Infinite loop.

**Resolution:** Changed to `self._nick = self._nick + "_"`. Now: `Internets` → `Internets_` → `Internets__`.

---

### BUG-005: Bot Ignores Its Own JOINs/PARTs/KICKs After Nick Collision

**Severity:** Critical
**File:** `internets.py:559, 568, 577, 600`
**Status:** Fixed

After a nick collision, `self._nick` becomes `Internets_`, but all self-detection comparisons use the original `NICKNAME` constant:

```python
if nick.lower() == NICKNAME.lower():  # should be self._nick.lower()
```

The bot won't recognize its own JOINs (so `active_channels` isn't updated), won't recognize its own PARTs/KICKs (so it thinks it's still in channels it left), and won't recognize PMs directed at it.

**Resolution:** All comparisons in `_process()` changed to `self._nick.lower()`.

---

### BUG-006: MOTD Detection Uses Substring Match Instead of Numeric Parse

**Severity:** Critical
**File:** `internets.py:454`
**Status:** Fixed

```python
if not identified and ("376" in line or "422" in line):
```

Substring search on the raw IRC line. A PRIVMSG containing "376", a nick containing those digits, or a server name containing them will all false-positive. The bot would prematurely attempt to rejoin channels and identify to NickServ.

**Resolution:** Replaced with `re.match(r":\S+ (376|422) ", line)`.

---

### BUG-007: PING Handler Crashes on Colon-less PING

**Severity:** High
**File:** `internets.py:486`
**Status:** Fixed

```python
self.send("PONG " + line.split(":", 1)[1], priority=0)
```

RFC 2812 permits `PING servername` without a colon prefix. `split(":", 1)` produces a single-element list, and `[1]` raises `IndexError`.

**Resolution:** Handler now checks for colon presence and falls back to space-split.

---

### BUG-008: Auth Session Persists Across Nick Changes

**Severity:** High
**File:** `internets.py` — `_authed` set
**Status:** Fixed

The `_authed` set stores nicks at authentication time. When a user changes their nick (`NICK` message), the old nick remains in `_authed`. If someone else takes the old nick, they inherit the admin session.

**Resolution:** NICK handler now migrates `_authed` from old nick to new nick.

---

### BUG-009: `channels_load()` Reads Without Lock

**Severity:** High
**File:** `store.py:54`
**Status:** Fixed

`channels_load` calls `self._load()` without acquiring `self._lock`, but `channels_save` writes under the lock. Concurrent access could yield partial JSON and an empty channel list.

**Resolution:** Wrapped in `with self._lock:`.

---

### BUG-010: No Thread Safety on `_modules` and `_commands` Dicts

**Severity:** High
**File:** `internets.py`
**Status:** Fixed

`dispatch()` reads `self._modules` and `self._commands` on spawned threads while `load_module()` / `unload_module()` mutate them from other threads. A `.reloadall` during dispatch can produce `RuntimeError: dictionary changed size during iteration`.

**Resolution:** Added `_mod_lock = threading.Lock()` protecting all reads and writes to both dicts. `dispatch()` takes a snapshot under lock before spawning threads.

---

### PERF-001: Store Reads Entire JSON File From Disk on Every Operation

**Severity:** Medium
**File:** `store.py`
**Status:** Fixed

All data is now loaded once at startup and mutated in memory. A background thread flushes dirty datasets to disk every 30 seconds. Each dataset (locations, channels, users) has its own lock, eliminating cross-dataset contention. `graceful_shutdown` and `.restart` force an immediate flush. Public API unchanged.

---

### IMPROVE-001: `hmac.compare_digest` Exists

**File:** `hashpw.py:137-144`
**Status:** Fixed

Hand-rolled `_ct_eq` function replaced with `hmac.compare_digest`, which is implemented in C and actually constant-time.

---

### IMPROVE-002: No Graceful Shutdown / QUIT on SIGTERM

**File:** `internets.py:625-636`
**Status:** Fixed

`KeyboardInterrupt` called `sys.exit(0)` without sending `QUIT`. Added `SIGTERM`/`SIGINT` handlers that send `QUIT` and flush the sender queue before exit.

---

### IMPROVE-003: Limited NAMES Response Handling

**File:** `internets.py`
**Status:** Partially addressed

The bot now parses 353 (`RPL_NAMREPLY`) to extract channel operator status (~, &, @) for the chanop tracking system. However, the general user roster (tracked in `users.json`) is still only populated from observed events. Users present before the bot joined won't appear in `.users` until they trigger an observable event.

---

### IMPROVE-004: Translate Module Uses Undocumented Google Endpoint

**File:** `modules/translate.py:13`
**Status:** Documented (known limitation)

`translate.googleapis.com/translate_a/single` is an internal Google endpoint with no SLA. Documented in README as a known fragility.

---

### IMPROVE-005: Urban Dictionary Module Reads `[weather]` Config Section

**File:** `modules/urbandictionary.py:37`
**Status:** Fixed

UD module now falls back to `"Internets/1.0"` default User-Agent if `[weather]` config section is missing.

---

### IMPROVE-006: `_split_msg` Can Break Multi-byte Characters

**File:** `internets.py:142-146`
**Status:** Fixed

Split now backs up to the last valid UTF-8 character boundary by checking for `10xxxxxx` continuation bytes. CJK, emoji, and accented characters no longer garble at chunk boundaries.

---

### IMPROVE-007: Channel Founder Verification via IRC Services

**File:** `modules/channels.py` (full rewrite), `modules/base.py`, `internets.py`, `config.ini`
**Status:** Implemented

Previously any user could `.join` or `.part` the bot from any channel. Now both commands require the user to be either a bot admin or the registered channel founder, verified asynchronously via IRC services.

Verification flow:

1. Bot sends `WHOIS nick` → extracts NickServ account (330 numeric).
2. Bot sends `PRIVMSG ChanServ :INFO #channel` → extracts founder name.
3. Compares account == founder (case-insensitive).
4. 15-second timeout with graceful fallback messaging.

Services compatibility tested: Anope, Atheme, Epona, X2, X3 — any service that responds to `INFO #channel` with a `Founder:` or `Owner:` line. The services bot nick is configurable via `services_nick` in `config.ini`.

Infrastructure: Added `on_raw(line)` hook to `BotModule` base class for raw IRC traffic interception.

---

### IMPROVE-008: Dice Rolls Array Spams Channel on High Counts

**File:** `modules/dice.py:25`
**Status:** Fixed

`.d 100d100` now shows only the first 10 individual rolls with a count note instead of dumping all 100 values.

---

### IMPROVE-009: No Rate Limiter Cleanup

**File:** `store.py:109-135`
**Status:** Fixed

Stale entries in `_flood` and `_api` dicts are now purged every 5 minutes, preventing unbounded memory growth.

---

### IMPROVE-010: `_validate_hash` Prefix Parsing is Brittle

**File:** `internets.py:74`
**Status:** Documented

An accidentally pasted raw bcrypt hash like `$2b$12$...` (without the `bcrypt$` wrapper) would extract an empty prefix. The error message now clarifies the expected format. Not changed structurally since the failure mode is correct (reject invalid hash) — just confusing.

---

### IMPROVE-011: `os.execv` Restart Doesn't Flush Sender Queue

**File:** `internets.py:315`
**Status:** Fixed

Reordered to send `QUIT` first, then `time.sleep(2)` to flush, then `os.execv`. Previously the sleep occurred before the QUIT send.

---

## Second Pass — Security Hardening (2026-03-02)

### SEC-001: Admin Password Logged in Plaintext

**Severity:** Medium
**File:** `internets.py` (command dispatch log)
**Status:** Fixed

The command log line wrote `cmd='auth' arg='theActualPassword'` to both the log file and stdout. Auth/deauth args are now redacted as `[REDACTED]`.

---

### SEC-002: Path Traversal → Remote Code Execution in `load_module`

**Severity:** Critical
**File:** `internets.py` (load_module)
**Status:** Fixed

`.load ../../evil` constructs `modules/../../evil.py`, escaping the modules directory and loading (executing) arbitrary Python files anywhere on the filesystem. This is a direct RCE vector — it amplifies any single credential compromise into full system access.

**Resolution:** Module names validated against `^[a-z][a-z0-9_]*$`. No slashes, dots, or path components allowed.

---

### SEC-003: IRC Command Injection via CRLF

**Severity:** Critical
**File:** `sender.py` (_write)
**Status:** Fixed

The sender wrote raw `msg + "\r\n"` to the socket. If any `msg` contains embedded `\r\n` (from module output, crafted channel names, etc.), the IRC server interprets it as multiple commands. An attacker could inject arbitrary IRC protocol commands.

**Resolution:** `_write()` strips all `\r`, `\n`, and `\x00` from outgoing messages before sending.

---

### SEC-004: Credentials Logged at DEBUG Level

**Severity:** High
**Files:** `sender.py` (_write), `internets.py` (main loop)
**Status:** Fixed

At DEBUG log level, the sender logged every outgoing message including `PASS`, `IDENTIFY`, and `OPER` commands with their passwords. The main loop logged every incoming line including `AUTH` from users.

**Resolution:** Sender redacts `PASS`, `IDENTIFY`, `OPER`, and `AUTHENTICATE` arguments. Main loop redacts incoming lines matching AUTH patterns.

---

### SEC-005: Admin Sessions Persist Across Reconnects

**Severity:** Critical
**File:** `internets.py` (reconnect handler)
**Status:** Fixed

On disconnect/reconnect, `_authed` was not cleared. Users who authenticated on the old connection remained admins, but their nicks may now belong to entirely different people on the new connection.

**Resolution:** `_authed` cleared on every disconnect with a log entry noting how many sessions were dropped.

---

### SEC-006: No Brute-Force Protection on Auth

**Severity:** High
**File:** `internets.py` (cmd_auth)
**Status:** Fixed

No rate limiting or lockout for failed password attempts beyond the global 3-second flood gate. An attacker could try passwords every 3 seconds indefinitely.

**Resolution:** After 5 failed attempts, auth is locked out for 5 minutes per nick. Counter resets after lockout expires or on successful auth.

---

### BUG-011: Empty Prefix Crashes With IndexError

**Severity:** Medium
**File:** `internets.py` (command parsing)
**Status:** Fixed

Sending just the prefix character (e.g., `.` with nothing after) produces an empty `parts` list. `parts[0]` throws `IndexError`, crashing the processing thread.

**Resolution:** Added `if parts:` guard.

---

### BUG-012: `channel_users()` Reads Without Lock

**Severity:** Medium
**File:** `store.py:107`
**Status:** Fixed

Same class of bug as BUG-009. `channel_users()` read without `self._lock` while write operations held the lock.

**Resolution:** Wrapped in `with self._lock:`.

---

### BUG-013: Non-Atomic JSON Writes

**Severity:** High
**File:** `store.py` (_save)
**Status:** Fixed

`Path(path).write_text()` is not atomic. A crash mid-write (power loss, OOM kill, SIGKILL) leaves the JSON file truncated or empty. All locations/channels/users lost on next startup.

**Resolution:** Writes go to a temporary file in the same directory, then `os.replace()` atomically moves it into place.

---

### BUG-014: `_require_admin` Uses Stale NICKNAME Constant

**Severity:** Low
**File:** `internets.py` (_require_admin, cmd_help)
**Status:** Fixed

Auth hint and help header used the global `NICKNAME` constant instead of `self._nick`. After a nick collision, these displayed the wrong nick.

**Resolution:** Changed to `self._nick`.

---

### BUG-015: Calculator DoS via `factorial()` and Deep Nesting

**Severity:** High
**File:** `modules/calc.py`
**Status:** Fixed

`factorial(99999)` hangs the handler thread for minutes computing a number with 456,000+ digits. Deeply nested expressions (`sin(sin(sin(...)))` × 55+) exhaust the Python call stack.

**Resolution:** Factorial input capped at 170 (max that fits in float64). AST evaluator depth limited to 50.

---

### BUG-016: Channels Not Rejoined After Reboot

**Severity:** High
**File:** `internets.py` (_rejoin_channels, _process)
**Status:** Fixed

Channels are correctly saved to `channels.json` on invite/join, but rejoin after reboot/reconnect fails silently for two reasons:

1. **Invite-only channels (`+i`):** The original invite expires when the bot disconnects. On reconnect, `JOIN #channel` gets 473 (ERR_INVITEONLYCHAN). The bot ignored 473 entirely — no retry, no log, no error.

2. **NickServ timing race:** The bot only waited 1 second after `IDENTIFY` before sending JOINs. If NickServ hasn't confirmed yet, channels requiring registered nicks (`+R`) or ChanServ access lists reject the JOIN.

**Resolution:**

- Added 473 handler: on invite-only rejection, bot sends `PRIVMSG ChanServ :INVITE #channel`. ChanServ re-invites the bot (if the bot's NickServ account has channel access), triggering the existing `_on_invite` → `JOIN` flow.
- Added 471/474/475 handlers: log the rejection and remove the channel from saved channels (user must re-invite).
- Replaced the fixed 1-second `time.sleep` with a deferred rejoin task that waits up to 10 seconds for NickServ confirmation (NOTICE containing "identified"/"recognized", or 900 numeric) before rejoining. Falls back to rejoining anyway after the timeout.

---

## Third Pass — Data Architecture & Protocol Fixes (2026-03-03)

### ARCH-001: Hybrid Weather Data Source Merging

**Severity:** N/A (feature)
**Files:** `modules/nws.py`, `modules/weather.py`
**Status:** Implemented

Previously the weather module returned NWS data as a pre-formatted string for US locations and Open-Meteo as a separate pre-formatted string for non-US. If NWS returned null fields (common for temperature, humidity, or visibility at some stations), the user saw "N/A" even though Open-Meteo had the data.

**Resolution:** Both `nws.current()` and `_om_current()` now return structured `WeatherDict` dicts with consistent keys (`temp_c`, `feels_c`, `humidity`, `wind_kph`, etc.). A `_merge_current()` function combines them — NWS values take priority, Open-Meteo fills gaps. A single `_format_current()` function produces the output string. For US locations, both sources are queried and merged. For non-US, only Open-Meteo is used. NWS heat index and wind chill labels are preserved through the merge.

---

### BUG-017: MODE Arg Consumption Uses Hardcoded List, Ignores ISUPPORT

**Severity:** High
**File:** `internets.py` (MODE processing)
**Status:** Fixed

The MODE parser hardcoded which modes consume parameters (`h, v, b, e, I, k, l`), but the server sends `CHANMODES=beI,kL,lH,...` in 005 ISUPPORT. Modes `L` (type B — always takes a parameter) and `H` (type C — parameter on set only) were unknown to the parser. A mode change like `+Loq #target nick1 nick2` would fail to consume the `L` parameter, causing `+o` to read `#target` as the nick instead of `nick1`. This corrupted the chanop tracking set — the bot would think `#target` was an operator nick.

The ISUPPORT `PREFIX` value (e.g., `(qaohv)~&@%+`) was also never parsed — the bot hardcoded `qaohv` and never updated from the server's actual prefix mode list.

**Resolution:** Added 005 ISUPPORT parsing that populates `_chanmode_types` and `_prefix_modes` from the server's actual capabilities. The MODE parser now uses these dicts. Handles all four CHANMODES types:
- **A** (list modes, always take a parameter): `b`, `e`, `I`
- **B** (always take a parameter): `k`, `L`
- **C** (parameter on set, none on unset): `l`, `H`
- **D** (never take a parameter): `i`, `m`, `n`, `p`, `s`, `t`

Plus all PREFIX modes (parameter on both set and unset). These helpers were extracted to `protocol.py` as `parse_isupport_chanmodes()`, `parse_isupport_prefix()`, and `parse_mode_changes()` for unit testing.

---

### BUG-018: `active_channels` Set Modified From Multiple Threads

**Severity:** Medium
**File:** `internets.py`
**Status:** Fixed

`active_channels` was a plain `set` modified from the main loop thread, dispatch threads (via `_on_invite`), and the `_deferred_rejoin` thread. `sorted(self.active_channels)` could yield `RuntimeError: Set changed size during iteration` under concurrent access. CPython's GIL masks this in practice but it's not guaranteed and will break on alternative Python implementations.

**Resolution:** Replaced with `ChannelSet`, a thread-safe container with `threading.Lock` protecting all operations. All iteration sites now use `snapshot()` which returns a frozen copy.

---

### BUG-019: Gusts Displayed When Wind Is Zero

**Severity:** Low
**File:** `modules/weather.py` (`_format_current`)
**Status:** Fixed

```python
if gusts and gusts > wind_kph * 1.3:
```

When `wind_kph` is 0, `0 * 1.3 = 0`, so any nonzero gust value passes the check. The output would show "Calm (gusts 5.0km/h)" which is contradictory. Also, `gusts` being `0.0` is falsy in Python, which would hide the gust display even if the check was otherwise correct — but that's benign since zero gusts shouldn't be displayed anyway.

**Resolution:** Added explicit `wind_kph > 0` guard: `if gusts is not None and gusts > 0 and wind_kph > 0 and gusts > wind_kph * 1.3`.

---

### CLEANUP-001: Stale `fmt_dt` Import in `nws.py`

**Severity:** N/A
**File:** `modules/nws.py`
**Status:** Fixed

After the dict refactor (ARCH-001), `fmt_dt` was no longer used in `nws.py` but was still imported. Removed.

---

## Fourth Pass — Async Architecture & Type Safety (2026-03-04)

### ARCH-002: Full Async Conversion

**Severity:** N/A (architecture)
**Files:** All
**Status:** Implemented

The bot was previously built on a synchronous socket + daemon threads model. Every command handler ran in a new thread via `asyncio.to_thread()`, the sender used a `queue.PriorityQueue` with a blocking drain loop, and the keepalive/rejoin were standalone `threading.Thread` instances. While functional at IRC scale, this model made it difficult to reason about concurrency and required careful locking.

**Resolution:** Full conversion to single-event-loop asyncio:

- `internets.py`: Connection via `asyncio.open_connection()`. Line reading via `asyncio.StreamReader.readline()` with 300s timeout. Signal handling via `loop.add_signal_handler()`. All 15 core command handlers converted to `async def`.
- `sender.py`: Rewritten as async drain loop over `asyncio.PriorityQueue` + `StreamWriter.drain()`. Token-bucket uses `asyncio.sleep()`. `enqueue()` remains thread-safe via `loop.call_soon_threadsafe()`.
- `modules/base.py`: Docstring updated to document that all handlers are coroutines.
- `modules/geocode.py`: `geocode()` is now `async def` with `await asyncio.to_thread()` for HTTP calls.
- `modules/nws.py`: All functions (`get_grid`, `current`, `forecast`, `hourly`, `alerts`, `discussion`) converted to `async def`.
- `modules/weather.py`: All command handlers converted to `async def`. HTTP-dependent functions (`_om_current`, `_om_forecast`) converted to `async def`.
- `modules/location.py`: All command handlers converted to `async def`.
- `modules/calc.py`: Handler converted to `async def`. Pure computation runs directly in event loop.
- `modules/dice.py`: Handler converted to `async def`. Pure computation runs directly in event loop.
- `modules/translate.py`: Handler converted to `async def` with `asyncio.to_thread()` for HTTP.
- `modules/urbandictionary.py`: Handler converted to `async def` with `asyncio.to_thread()` for HTTP.
- `modules/channels.py`: Handlers converted to `async def`. Verification timeout GC converted from `threading.Thread` to `asyncio.create_task()`.
- `_run_cmd()`: Now `await handler(nick, reply_to, arg)` directly instead of `await asyncio.to_thread(handler, ...)`.
- Console: Uses `asyncio.to_thread(input)` for non-blocking stdin, running as an asyncio task alongside the main bot task via `asyncio.wait(return_when=FIRST_COMPLETED)`.

---

### ARCH-003: Type Annotations Throughout

**Severity:** N/A (quality)
**Files:** All
**Status:** Implemented

All files now use `from __future__ import annotations` with PEP 604 union syntax (`str | None` instead of `Optional[str]`). Every public function, method, and class attribute is annotated. Module `setup()` functions return typed `BotModule` subclasses. Internal helpers (`_DebugFilter`, `_setup_logging`, `_get_hash`, `_validate_hash`, signal handler, `_run_console`) all have proper signatures.

---

### ARCH-004: SASL PLAIN Authentication

**Severity:** N/A (feature)
**Files:** `internets.py`, `protocol.py`
**Status:** Implemented

When the server advertises SASL support in CAP LS and a NickServ password is configured, the bot authenticates via SASL PLAIN during capability negotiation — before registration completes. This eliminates the timing race between NickServ IDENTIFY and `+R` channel joins / ChanServ access lists. If SASL fails (902/904/905 numerics), the bot falls back to traditional NickServ IDENTIFY after MOTD. `AUTHENTICATE` payloads are redacted in sender logs.

`sasl_plain_payload()` extracted to `protocol.py` for unit testing.

---

### ARCH-005: `protocol.py` Extraction

**Severity:** N/A (quality)
**Files:** `protocol.py` (new, 111 lines)
**Status:** Implemented

Pure protocol helper functions extracted from `internets.py`:
- `strip_tags()` — Remove IRCv3 message tags from raw lines
- `parse_isupport_chanmodes()` — Parse CHANMODES= from 005 ISUPPORT
- `parse_isupport_prefix()` — Parse PREFIX= from 005 ISUPPORT
- `parse_mode_changes()` — Parse MODE changes with correct arg consumption
- `parse_names_entry()` — Parse a single entry from 353 NAMES reply
- `sasl_plain_payload()` — Encode SASL PLAIN `\0nick\0password` as base64

No bot state, no I/O, no side effects — fully unit-testable.

---

### ARCH-006: Exponential Reconnect Backoff

**Severity:** N/A (reliability)
**File:** `internets.py`
**Status:** Implemented

Previously used fixed 15s/30s delays between reconnect attempts, which would hammer the server during extended outages. Replaced with exponential backoff: 15s, 30s, 60s, 120s, 240s, capped at 300s (5 minutes). Attempt counter resets on successful connection. Applied to both initial connection and mid-session reconnects.

---

### ARCH-007: User Pruning

**Severity:** N/A (maintenance)
**Files:** `store.py`, `config.ini`
**Status:** Implemented

User tracking entries older than `user_max_age_days` (default 90, configurable in `config.ini`) are automatically pruned during store flushes. Prevents unbounded `users.json` growth on busy networks with high nick churn.

---

### ARCH-008: Test Suite

**Severity:** N/A (quality)
**File:** `tests/run_tests.py` (new)
**Status:** Implemented

73 tests with no external dependencies (no pytest required, but compatible with it). Coverage:

- Protocol parsing: ISUPPORT CHANMODES, ISUPPORT PREFIX, MODE changes with multi-type args, NAMES entries with multi-prefix, SASL payload encoding, tag stripping
- Store: location CRUD, channel save/load, user tracking (join/part/quit/rename), flush-to-disk, atomic write verification, user pruning of stale entries
- Calculator: basic arithmetic, division, powers, implicit multiplication, math functions, factorial with cap, exponent bomb blocked, division by zero, unknown names rejected, nesting depth limit, `log2`/`log10` names preserved from implicit multiplication
- Dice: single die, XdN format, XdN+M modifiers, invalid format rejection, count limits, large-roll display truncation
- Weather: merge (both None, primary None, fallback None, primary wins, NWS heat index label preserved), format (complete dict, None input, calm wind, gusts threshold, no N/A, feels-like suppression)
- Units: temperature (C/F), wind direction cardinal, wind speed (kph/mph), distance (km/mi), pressure (mb/in), datetime formatting
- Sender: CRLF/NUL injection stripped, credential redaction for PASS/IDENTIFY/OPER/AUTHENTICATE
- Password hashing: scrypt round-trip, invalid hash format rejection, empty hash handling
- ChannelSet: thread-safe add/discard/contains, snapshot returns independent copy, iteration safety
- Backoff: exponential curve with cap at 300s
- Async sender: enqueue + drain produces output, priority 0 bypasses token bucket, thread-safe enqueue from executor
- Async handlers: all 7 module command handler classes confirmed as coroutines, all 15 core command handlers confirmed as coroutines, geocode/nws/weather async functions verified, sync pure functions (`_merge_current`, `_format_current`) confirmed non-async

---

## Fifth Pass — Correctness & Edge Cases (2026-03-04)

### BUG-020: Admin Auth is Case-Sensitive

**Severity:** Medium
**File:** `internets.py` (`_authed`, `is_admin`)
**Status:** Fixed

`is_admin()` checks `nick in self._authed`, but IRC nicks are case-insensitive per RFC 2812. If the server echoes a nick with different casing than what was used during authentication, admin status silently fails. This also affects `cmd_deauth` and the NICK migration handler.

**Resolution:** All `_authed` operations (`add`, `discard`, `in`) now normalize to `nick.lower()`. `is_admin()` checks `nick.lower() in self._authed`.

---

### BUG-021: Hostmask Capture Loses `user@` Portion

**Severity:** Medium
**File:** `internets.py` (JOIN, NICK, PRIVMSG regexes)
**Status:** Fixed

The regex pattern `![^@]+@(\S+)` captures only the hostname after `@`, discarding the ident/username. The stored `hostmask` field in `users.json` contains just the hostname (e.g. `some.host`), but the CHGHOST handler correctly stores `user@host` (e.g. `ident@some.host`). The `.users` command output shows `nick!hostname` instead of the expected `nick!user@hostname`.

**Resolution:** Changed the capturing group in JOIN, NICK, and PRIVMSG regexes from `![^@]+@(\S+)` to `!(\S+)`, capturing the full `user@host` string. All three regexes now produce the same format as the CHGHOST handler.

---

### BUG-022: Premature `active_channels.add` Before JOIN Confirmation

**Severity:** Medium
**File:** `internets.py` (`_on_invite`, `_deferred_rejoin`)
**Status:** Fixed

Both `_on_invite` and `_deferred_rejoin` added channels to `active_channels` and saved to disk immediately after sending `JOIN`, before the server confirmed the join. If the server rejected the `JOIN` (invite-only, banned, full, etc.), phantom channel entries persisted in `active_channels` and `channels.json`. The error handlers for 471/474/475 could clean up some cases, but the premature add was conceptually wrong — `_on_join` already handles both add and save when the server echoes the JOIN back.

**Resolution:** Removed `active_channels.add()` and `channels_save()` from `_on_invite` and `_deferred_rejoin`. The server-confirmed `_on_join` callback is now the sole point where channels enter the active set and are saved.

---

### BUG-023: Missing JOIN Error Handlers for 403, 405, 476

**Severity:** Low
**File:** `internets.py` (`_process`)
**Status:** Fixed

The bot handled 473 (ERR_INVITEONLYCHAN), 471 (ERR_CHANNELISFULL), 474 (ERR_BANNEDFROMCHAN), and 475 (ERR_BADCHANNELKEY), but not 403 (ERR_NOSUCHCHANNEL), 405 (ERR_TOOMANYCHANNELS), or 476 (ERR_BADCHANMASK). If a saved channel was deleted from the network or the channel mask was malformed, the bot would silently fail to join with no cleanup.

**Resolution:** Added 403, 405, and 476 to the existing error handler. All six numerics now log a warning and remove the channel from `active_channels` and saved channels.

---

### BUG-024: Task Done Callback Crashes After `_tasks.clear()`

**Severity:** Medium
**File:** `internets.py` (`_dispatch`)
**Status:** Fixed

During reconnect, the main loop cancels all tasks and calls `self._tasks.clear()`. When cancelled tasks subsequently complete and their `done_callback` fires, `self._tasks.remove(task)` raises `ValueError` because the task was already removed by `clear()`. This could crash the event loop's callback handling.

**Resolution:** Changed the done callback from `self._tasks.remove` to a lambda that checks `t in self._tasks` before removing.

---

### BUG-025: `channels.py` Uses Deprecated `asyncio.get_event_loop()`

**Severity:** Low
**File:** `modules/channels.py` (`on_load`)
**Status:** Fixed

The channels module's `on_load()` used `asyncio.get_event_loop()` to create the cleanup task. This API is deprecated since Python 3.10 and emits `DeprecationWarning` in some configurations. Since `on_load()` is always called from within a running event loop (via `autoload_modules()` in `run()`), `asyncio.get_running_loop()` is the correct modern API.

**Resolution:** Replaced with `asyncio.get_running_loop()`.

---

## Sixth Pass — DevSecOps Hardening (March 4 2026)

**Auditor:** Brandon Troidl  
**Scope:** Protocol compliance, input validation, DoS resistance, information disclosure, TLS configuration, log injection.

---

### SEC-007: Log Injection via Unsanitized IRC Content

**Severity:** Medium  
**File:** `internets.py` (logging setup)  
**Status:** Fixed

IRC messages containing embedded `\r\n` sequences could be written directly into log files, enabling log injection attacks that forge log entries. An attacker sending `innocent\r\n2026-03-04 [INFO] internets: Admin auth granted: hacker` would create a fake audit trail.

**Resolution:** Added `_SafeFormatter` — a custom `logging.Formatter` subclass that strips all CR, LF, and NUL characters from log messages before formatting. Applied globally to all handlers during `_setup_logging()`.

---

### SEC-008: Error Information Disclosure to IRC

**Severity:** Medium  
**Files:** `internets.py` (`_run_cmd`, `load_module`, `unload_module`)  
**Status:** Fixed

Raw Python exception messages were sent back to IRC users in two ways: (1) module load/unload errors returned `f"Error loading '{name}': {e}"` which could expose file paths, class names, or import errors; (2) unhandled exceptions in command handlers were logged but gave no user feedback, or worse, module handlers might propagate tracebacks in error replies.

**Resolution:** `_run_cmd` now catches all exceptions and sends a generic `"internal error processing '<cmd>' — see log for details"` NOTICE. `load_module` and `unload_module` error messages now say `"see log for details"` instead of including the raw exception string. Full details remain in the log for administrators.

---

### SEC-009: TLS 1.0/1.1 Not Blocked

**Severity:** High  
**File:** `internets.py` (`_connect`)  
**Status:** Fixed

The SSL context used `ssl.create_default_context()` without setting a minimum TLS version. While modern servers typically negotiate TLS 1.2+, a downgrade attack or misconfigured server could result in connecting over deprecated TLS 1.0 or 1.1, which have known cryptographic weaknesses.

**Resolution:** Added `ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2` immediately after context creation, blocking any connection attempt below TLS 1.2.

---

### BUG-026: IRC 512-Byte Line Limit Not Enforced

**Severity:** Medium  
**File:** `sender.py` (`_write_line`)  
**Status:** Fixed

RFC 2812 §2.3 limits IRC messages to 512 bytes including the trailing `\r\n`. The sender had no line length enforcement — long PRIVMSG lines (e.g. weather output with many fields) could exceed 512 bytes, causing the server to silently truncate or drop the message. Truncation mid-UTF-8 sequence would also produce mojibake on the receiving end.

**Resolution:** `_write_line` now encodes the message to UTF-8, checks if the length exceeds 510 bytes (512 minus `\r\n`), and truncates with UTF-8-safe boundary detection (backs up past incomplete multi-byte sequences). Added `_MAX_IRC_LINE = 512` class constant.

---

### BUG-027: PRIVMSG/NOTICE Target Not Validated

**Severity:** Medium  
**File:** `internets.py` (`privmsg`, `notice`)  
**Status:** Fixed

Neither `privmsg()` nor `notice()` validated the target parameter. A crafted target containing a space (e.g. `"#chan :injected PRIVMSG"`) could inject additional IRC protocol parameters. While the CRLF injection fix in the sender (SEC-003) prevents full command injection, space-based parameter injection within a single line was still possible.

**Resolution:** Both `privmsg()` and `notice()` now reject empty targets and targets containing spaces, logging a warning and returning without sending.

---

### BUG-028: Module Loader Follows Symlinks Outside `MODULES_DIR`

**Severity:** Medium  
**File:** `internets.py` (`load_module`)  
**Status:** Fixed

While module names were validated against `^[a-z][a-z0-9_]*$` (SEC-002), the loader did not check whether the resolved path remained within the modules directory. A symlink `modules/evil.py -> /etc/passwd` (or any other file) would pass the name regex and be loaded. On a shared host, an attacker with write access to the modules directory could create such a symlink.

**Resolution:** After checking `path.exists()`, the loader now resolves the real path via `path.resolve()` and verifies it starts with `MODULES_DIR.resolve()`. If the resolved path escapes the modules directory, the load is blocked with a log warning.

---

### BUG-029: Config File World-Readable Warning

**Severity:** Low  
**File:** `internets.py` (startup)  
**Status:** Fixed

`config.ini` contains server passwords, NickServ passwords, OPER credentials, and the admin password hash. On a multi-user system, the default file creation mask (`umask 022`) leaves the file world-readable (`-rw-r--r--`). There was no warning about this.

**Resolution:** At startup, the bot now `stat()`s `config.ini` and logs a WARNING if the world-read bit (`0o004`) is set, suggesting `chmod 640 config.ini`.

---

### BUG-030: Unbounded Concurrent Command Tasks (DoS)

**Severity:** High  
**File:** `internets.py` (`_dispatch`)  
**Status:** Fixed

Every incoming command created a new `asyncio.Task` with no upper bound. An attacker sending hundreds of slow commands (e.g. `.weather` lookups to slow APIs) could exhaust memory and event loop resources. The flood limiter only caps one command per 3 seconds per nick, but many nicks (or a botnet) could overwhelm the bot.

**Resolution:** Added `_MAX_TASKS = 50` class constant. `_dispatch` now counts active command tasks (those whose name starts with `"cmd-"`) and rejects new commands with a NOTICE when the cap is reached. The cap is generous enough for normal usage but prevents resource exhaustion.

---

### BUG-031: No Input Length Cap on Command Arguments

**Severity:** Medium  
**File:** `internets.py` (`_dispatch`)  
**Status:** Fixed

Command arguments had no length limit. A user could send `.cc <10KB expression>` or `.weather <enormous string>`, which would be passed to the handler, geocoder, or AST parser at full size. While individual handlers have some limits (calc caps nesting depth, factorial caps at 170), the raw argument was unbounded.

**Resolution:** Added `_MAX_ARG_LEN = 400` class constant. `_dispatch` rejects arguments exceeding this limit with a NOTICE before any handler is invoked. The limit is generous for all normal commands (longest typical input is a full address or multi-word search term).

---

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Critical bugs (first pass) | 6 | All fixed |
| Critical security (second pass) | 3 | All fixed |
| High bugs (all passes) | 8 | All fixed |
| High security (second/sixth pass) | 4 | All fixed |
| Medium issues (all passes) | 14 | All fixed |
| Low issues | 5 | All fixed |
| Improvements | 11 | All fixed or documented |
| Performance | 1 | Fixed |
| Architecture & features (third/fourth pass) | 8 | All implemented |
| Cleanup | 1 | Fixed |
| **Total** | **61** | **All resolved** |
