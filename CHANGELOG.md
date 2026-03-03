# CHANGELOG — Code Review Findings

**Reviewer:** Claude (Principal Engineer review)
**Date:** 2026-03-02
**Scope:** Full codebase audit — `internets.py`, `sender.py`, `store.py`, `hashpw.py`, `config.ini`, and all modules in `modules/`.

---

## Critical — Must Fix

### BUG-001: Remote Code Execution via `eval()` in Calculator Module

**File:** `modules/calc.py:21`

The calculator uses `eval()` with `{"__builtins__": {}}` as a sandbox. This is not a sandbox. The empty `__builtins__` dict has been a known bypass for over a decade. Any user in any channel the bot sits in can achieve arbitrary code execution on the host:

```
.cc ().__class__.__bases__[0].__subclasses__()[140].__init__.__globals__['system']('id')
```

The specific subclass index varies by Python version, but the technique is universal. This is a textbook RCE on an Internet-facing service.

**Fix:** Replace `eval()` with `ast.literal_eval` for simple expressions, or implement a proper recursive-descent parser over a restricted AST. A minimal safe approach:

```python
import ast, operator

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow,  ast.Mod: operator.mod,
    ast.USub: operator.neg,
}

def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _GLOBALS and callable(_GLOBALS[node.func.id]):
            args = [_safe_eval(a) for a in node.args]
            return _GLOBALS[node.func.id](*args)
    raise ValueError("unsupported expression")

def _calc(expr):
    tree = ast.parse(expr, mode="eval")
    return _safe_eval(tree.body)
```

**Severity:** Critical. This is exploitable today by any IRC user in any channel the bot occupies.

---

### BUG-002: Registration Commands Re-sent on Every `recv` Cycle

**File:** `internets.py:436-443`

The registration block (`PASS`, `CAP LS`, `NICK`, `USER`) is guarded by `if not identified`, but `identified` only becomes `True` after the bot sees numeric 376 (end of MOTD) or 422 (no MOTD). The server sends dozens of lines during MOTD. Each `recv` returns a chunk, the loop iterates, `identified` is still `False`, and the bot sends `NICK`/`USER` again. On a typical MOTD, this fires 10–30 duplicate registration attempts before the 376 arrives. Most IRCds will kill the connection for flooding.

**Fix:** Add a `registered` flag that is set immediately after sending registration commands, independent of `identified`:

```python
buf, identified, registered = "", False, False

while True:
    try:
        if not registered and self.sock:
            if SERVER_PW:
                self.send(f"PASS {SERVER_PW}", priority=0)
            self.send("CAP LS 302", priority=0)
            self._cap_busy = True
            self.send(f"NICK {self._nick}", priority=0)
            self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)
            registered = True
        # ... rest of loop
```

Reset `registered = False` alongside `identified = False` in the reconnect handler.

**Severity:** Critical. The bot likely cannot connect to most IRC servers in its current state.

---

### BUG-003: CAP LS Parsing Destroys All Capabilities After the First

**File:** `internets.py:499, 514`

The regex `re.split(r"[\s=][^\s]*", params)` is intended to strip `=value` suffixes from capability tokens like `sasl=PLAIN`. What it actually does is split on any whitespace-or-equals character followed by greedy non-whitespace, which consumes entire capability names:

```python
>>> re.split(r"[\s=][^\s]*", "multi-prefix sasl=PLAIN away-notify")
['multi-prefix', '', '']
```

Every capability after the first is destroyed. `DESIRED_CAPS & set(...)` will never match more than one cap.

**Fix:**
```python
offered = {cap.split("=", 1)[0] for cap in params.split()}
wanted = DESIRED_CAPS & offered
```

Same fix needed on line 514 (CAP NEW handler).

**Severity:** Critical. The bot can never negotiate more than one IRCv3 capability.

---

### BUG-004: Nick Collision Recovery Loops Forever

**File:** `internets.py:535`

```python
self._nick = self._nick.rstrip("_") + "_"
```

`rstrip("_")` strips all trailing underscores, then appends one. After the first collision (`Internets` → `Internets_`), a second 433 produces `Internets_` → `Internets_` (unchanged). If `Internets_` is also taken, the bot enters an infinite 433 loop.

**Fix:**
```python
self._nick = self._nick + "_"
```

**Severity:** High. Bot cannot recover from a double nick collision.

---

### BUG-005: Bot Ignores Its Own JOINs/PARTs/KICKs After Nick Collision

**File:** `internets.py:559, 568, 577, 600`

After a nick collision, `self._nick` becomes `Internets_` (or similar), but all self-detection comparisons use the original `NICKNAME` constant:

```python
if nick.lower() == NICKNAME.lower():  # should be self._nick.lower()
```

This means the bot won't recognize its own JOINs (so `active_channels` isn't updated), won't recognize its own PARTs/KICKs (so it thinks it's still in channels it left), and won't recognize PMs directed at it.

**Fix:** Replace all `NICKNAME.lower()` comparisons in `_process()` with `self._nick.lower()`.

**Severity:** High. All channel tracking breaks after any nick collision.

---

### BUG-006: MOTD Detection Uses Substring Match Instead of Numeric Parse

**File:** `internets.py:454`

```python
if not identified and ("376" in line or "422" in line):
```

This is a substring search on the raw IRC line. A PRIVMSG containing "376" or "422", a nick containing those digits, or a server name containing them will all false-positive. The bot would prematurely attempt to rejoin channels and identify to NickServ before registration is complete.

**Fix:**
```python
if not identified and re.match(r":\S+ (376|422) ", line):
```

**Severity:** High. Unlikely in practice but trivially triggerable by a hostile user once the registration bug is fixed.

---

## High — Should Fix

### BUG-007: PING Handler Crashes on Colon-less PING

**File:** `internets.py:486`

```python
self.send("PONG " + line.split(":", 1)[1], priority=0)
```

RFC 2812 permits `PING servername` without a colon prefix. `split(":", 1)` produces a single-element list, and `[1]` raises `IndexError`. The bot disconnects on any colon-less PING.

**Fix:**
```python
payload = line.split(" ", 1)[1] if " " in line else ""
self.send(f"PONG {payload}", priority=0)
```

Or more defensively: `self.send("PONG " + (line.split(":", 1)[1] if ":" in line else line.split(" ", 1)[-1]), priority=0)`

**Severity:** High on servers that send colon-less PINGs (some do).

---

### BUG-008: Auth Session Persists Across Nick Changes

**File:** `internets.py` — `_authed` set

The `_authed` set stores nicks at authentication time. When a user changes their nick (`NICK` message), the old nick remains in `_authed`. If someone else takes the old nick, they inherit the admin session. The bot processes `NICK` messages (line 589) for user tracking but never updates `_authed`.

**Fix:** In the NICK handler, check if the old nick is in `_authed` and replace it:

```python
if m.group(1) in self._authed:
    self._authed.discard(m.group(1))
    self._authed.add(m.group(3))
```

**Severity:** High. Privilege escalation via nick takeover.

---

### BUG-009: `channels_load()` Reads Without Lock

**File:** `store.py:54`

`channels_load` calls `self._load()` without acquiring `self._lock`, but `channels_save` writes under the lock. A concurrent save during a load could yield a partially-written file, resulting in a JSON parse error and an empty channel list (the `except` clause returns `default`, which is `[]`). On reconnect, the bot would forget all its channels.

**Fix:** Wrap in `with self._lock:`.

**Severity:** Medium-High. Race window is small but consequences are total channel loss.

---

### BUG-010: No Thread Safety on `_modules` and `_commands` Dicts

**File:** `internets.py`

`dispatch()` reads `self._modules` and `self._commands` on spawned threads. `load_module()` / `unload_module()` mutate them from other threads (admin commands are also dispatched on threads). Python's GIL makes individual dict reads atomic, but a `.reloadall` that iterates and mutates `_commands` while dispatches are reading it can produce `RuntimeError: dictionary changed size during iteration` or stale references.

**Fix:** Use a `threading.Lock` around all reads and writes to `_modules` and `_commands`, or use a copy-on-write pattern (replace the entire dict atomically).

**Severity:** Medium. Will manifest under load during module reloads.

---

## Medium — Recommended

### PERF-001: Store Reads Entire JSON File From Disk on Every Operation

**File:** `store.py`

Every `user_join`, `user_part`, `user_quit`, `loc_get`, `loc_set` call reads the full JSON file from disk, deserializes it, optionally modifies it, and writes it back. On an active channel with frequent joins/parts, this is hundreds of disk round-trips per minute. The user registry (`users.json`) grows unboundedly since entries are never pruned.

**Recommendation:** Cache data in memory, write to disk on a debounced timer (e.g., every 30 seconds or on graceful shutdown), and add a TTL-based eviction for stale user entries. Alternatively, use SQLite, which handles concurrent reads/writes correctly.

---

### IMPROVE-001: `hmac.compare_digest` Exists

**File:** `hashpw.py:137-144`

The `_ct_eq` function is a hand-rolled constant-time comparison. Python's `hmac.compare_digest` does the same thing, is implemented in C, and is actually constant-time (the Python loop is subject to bytecode-level timing variations).

**Fix:**
```python
import hmac
# Replace _ct_eq(actual, expected) with:
hmac.compare_digest(actual, expected)
```

---

### IMPROVE-002: No Graceful Shutdown / QUIT on SIGTERM

**File:** `internets.py:625-636`

`KeyboardInterrupt` calls `sys.exit(0)` without sending `QUIT` to the server. The bot appears to ghost. A `SIGTERM` handler should flush the send queue and issue `QUIT`.

**Recommendation:**
```python
import signal

def _shutdown(signum, frame):
    log.info(f"Received signal {signum}, shutting down.")
    try:
        bot.send("QUIT :Shutting down", priority=0)
        time.sleep(1)
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
```

---

### IMPROVE-003: No NAMES Response Handling

**File:** `internets.py`

The bot tracks users via JOIN/PART/QUIT/NICK, but never processes 353 (`RPL_NAMREPLY`) or 366 (`RPL_ENDOFNAMES`). When the bot joins a channel, it has no knowledge of who is already there until each user speaks or triggers a trackable event. The `channel_users` data is incomplete by design.

**Recommendation:** Parse 353 responses after JOIN to populate the initial user list.

---

### IMPROVE-004: Translate Module Uses Undocumented Google Endpoint

**File:** `modules/translate.py:13`

`translate.googleapis.com/translate_a/single` is an internal Google endpoint. It has no SLA, no rate limit documentation, and Google regularly breaks or blocks it. Consider using LibreTranslate (self-hostable, FOSS) or documenting the fragility.

---

### IMPROVE-005: Urban Dictionary Module Reads `[weather]` Config Section

**File:** `modules/urbandictionary.py:37`

```python
self._ua = self.bot.cfg["weather"]["user_agent"]
```

The UD module has no dependency on weather configuration. If someone removes the `[weather]` section (say, they only want UD and dice), the module crashes. Should either read from a `[bot]` section `user_agent` key, or have its own default.

---

### IMPROVE-006: `_split_msg` Can Break Multi-byte Characters

**File:** `internets.py:142-146`

The split slices at a fixed byte offset (`_MAX_BODY`), which can land in the middle of a multi-byte UTF-8 sequence. The `errors="replace"` on decode masks this by inserting replacement characters, but the user sees garbled text at chunk boundaries for any message containing CJK, emoji, or accented characters.

**Fix:** Find the last valid UTF-8 character boundary at or before `_MAX_BODY`:

```python
def _split_msg(self, msg):
    enc = msg.encode("utf-8", errors="replace")
    while enc:
        chunk = enc[:self._MAX_BODY]
        # Back up to the last valid UTF-8 char boundary
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        if not chunk:
            chunk = enc[:self._MAX_BODY]  # fallback: force split
        yield chunk.decode("utf-8", errors="replace")
        enc = enc[len(chunk):]
```

---

## Low — Nice to Have

### IMPROVE-007: `join` and `part` Commands Lack Admin Gating

**File:** `modules/channels.py:24, 36`

Any user in any channel can make the bot join or leave arbitrary channels. This should probably require admin auth, or at least channel operator status.

---

### IMPROVE-008: Dice Rolls Array Spams Channel on High Counts

**File:** `modules/dice.py:25`

`.d 100d100` produces a message containing a 100-element list. At ~4 characters per roll, that's ~500 bytes just for the rolls array, which will be split across multiple IRC messages. Consider truncating the individual rolls display for counts above, say, 20.

---

### IMPROVE-009: No Rate Limiter Cleanup

**File:** `store.py:109-135`

The `_flood` and `_api` dicts grow without bound. Every unique nick that sends a command gets an entry that is never evicted. On a long-running bot in a busy network, this is a slow memory leak. Add periodic cleanup of entries older than their cooldown window.

---

### IMPROVE-010: `_validate_hash` Prefix Parsing is Brittle

**File:** `internets.py:74`

```python
prefix = h.split("$")[0] if "$" in h else ""
```

An accidentally pasted hash like `$2b$12$...` (raw bcrypt without the `bcrypt$` wrapper) would extract an empty string before the first `$`, and the bot would exit with a confusing error about prefix `''`. The error message should clarify the expected format.

---

### IMPROVE-011: `os.execv` Restart Doesn't Flush Sender Queue

**File:** `internets.py:315`

`os.execv` replaces the process immediately. The "Restarting ..." PRIVMSG may not actually be sent before the process dies, because the sender thread is daemon and `execv` doesn't wait for it. Add a brief `time.sleep()` after the QUIT send, or explicitly flush the queue.

Note: There is a `time.sleep(1)` on line 310, but it occurs before the QUIT send on line 312, not after it.

---

## Summary

| Severity | Count | Action |
|----------|-------|--------|
| Critical | 6 | Fix before deployment |
| High     | 4 | Fix in next release |
| Medium   | 1 | Schedule for improvement |
| Improvement | 11 | Address as time permits |
