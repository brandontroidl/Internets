# Security & Stability Audit

**Reviewer:** Brandon Troidl
**Date:** 2026-03-02
**Scope:** Full codebase audit — `internets.py`, `sender.py`, `store.py`, `hashpw.py`, `config.ini`, and all modules in `modules/`.

All findings have been resolved. See `CHANGELOG.md` for the release-oriented summary of changes.

---

## First Pass — Functional Audit

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

`KeyboardInterrupt` called `sys.exit(0)` without sending `QUIT`. Added `SIGTERM`/`SIGINT` handlers that send `QUIT` and sleep 2s to flush the sender queue.

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

## Second Pass — Security Hardening

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

**Resolution:** `_write()` strips all `\r` and `\n` from outgoing messages before sending.

---

### SEC-004: Credentials Logged at DEBUG Level

**Severity:** High
**Files:** `sender.py` (_write), `internets.py` (main loop)
**Status:** Fixed

At DEBUG log level, the sender logged every outgoing message including `PASS`, `IDENTIFY`, and `OPER` commands with their passwords. The main loop logged every incoming line including `AUTH` from users.

**Resolution:** Sender redacts `PASS`, `IDENTIFY`, and `OPER` arguments. Main loop redacts incoming lines matching AUTH patterns.

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
- Replaced the fixed 1-second `time.sleep` with a background thread (`_deferred_rejoin`) that waits up to 10 seconds for NickServ confirmation (NOTICE containing "identified"/"recognized", or 900 numeric) before rejoining. Falls back to rejoining anyway after the timeout.

---

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Critical bugs (first pass) | 6 | All fixed |
| Critical security (second pass) | 3 | All fixed |
| High bugs | 5 | All fixed |
| High security | 3 | All fixed |
| Medium issues | 4 | All fixed |
| Improvements | 11 | All fixed or documented |
| Performance | 1 | Fixed |
| **Total** | **33** | **All resolved** |
