# Knowledge Recovery Binder

This section documents knowledge that currently exists only in the code, in
commit messages, or in the heads of the original developers. Each item is
something a new maintainer would have to reverse-engineer from source without
this document.

---

## Hidden invariants

### 1. The `_nick` value used for SASL is the runtime nick, not the config nick

`internets.py:914` passes `self._nick` to `sasl_plain_payload`, not the
startup constant `NICKNAME`. On a 433 nick-collision (nick already taken), the
bot appends `_` or a random suffix. SASL PLAIN authenticates as the bumped
nick, not the intended one. This is correct: NickServ knows the bot's account
by its registered nick, but SASL PLAIN's `authzid` field (first NUL-delimited
segment) is the session's current nick. Sending the original nick while the
server assigned a different one would fail authentication.

### 2. The `_prefix_modes` symbol map from ISUPPORT is parsed and discarded

`parse_isupport_prefix` returns `(mode_set, symbol_map)`. The caller in
`internets.py` keeps only the mode set and discards the symbol map (bound to
`_`). The NAMES parser (`parse_names_entry`) uses a hardcoded `~&@%+` set
instead. On a network advertising a prefix symbol outside this set, `lstrip`
leaves the symbol attached and the nick in `_chanops` carries it. This is the
one place the discarded symbol map would have earned its keep.

### 3. `_NICKSERV_WAIT_TICKS * _NICKSERV_TICK` = the NickServ identification window

The deferred rejoin waits up to `40 * 0.25 = 10 seconds`
(`internets.py:177-178`) for NickServ identification to complete before
rejoining saved channels. This is not documented anywhere as a 10-second
window; you have to multiply the two constants. The wait uses
`_stop.wait(timeout=_NICKSERV_TICK)` per tick so a shutdown during the wait
breaks out immediately.

### 4. NAMES only adds ops, never removes them

`internets.py:996` uses `setdefault(chan, set())` and adds nicks to the chanop
set. It never clears the set before processing a NAMES reply. A NAMES refresh
on an already-joined channel cannot remove someone who was deopped in the
interim. Removal happens only through MODE (the `-o`/`-a`/`-q` path) or when
the channel is dropped entirely on PART/KICK. This means the `_chanops` set is
a superset of reality after enough time without MODE traffic.

### 5. The console's `help` command is dispatched but not listed in its own output

`console.py:87` dispatches the `help` command. `_CONSOLE_HELP` (the string it
prints) lists `debug`, `loglevel`, `status`, `shutdown`/`quit` but not `help`
itself.

### 6. `.rehash` clears admin sessions only on the happy path

`cmd_rehash` (`admin_cmds.py:483`) has two early-return paths (config reload
failure, bad hash prefix) that return before reaching `self._authed.clear()`.
Admin sessions survive both. This means a config-file syntax error preserves
the current auth state rather than defensively clearing it.

### 7. Rate limiter's channel flood gate does not record over-budget attempts

`RateLimiter.channel_check` (`store.py:554-559`): when a channel is over its
burst budget, the new attempt is refused WITHOUT being recorded into the
sliding window. This is deliberate: recording it would let an attacker keep
the window pinned full indefinitely by spamming after the limit trips. The
window drains naturally and recovers.

### 8. `_auth_fails` is unbounded within a single lockout window

The brute-force lockout tracks failures per nick. Pruning happens only when
the dict exceeds `_AUTH_CLEANUP_THRESHOLD = 50` entries, and only discards
entries older than `_AUTH_LOCKOUT`. A flood of distinct attacker-controlled
nicks, each making one attempt within the same 300s window, all stay in the
dict at once. The dict size within a single window is bounded only by the
number of unique nicks that can reach `.auth` during that window.

### 9. Postal codes never fall through to free-text geocoding

`geocode.py`'s postal-code classifier (`_postal_kind`) returns a non-None kind
for anything matching a postal pattern. The main `geocode()` function routes
postal codes to structured lookups and never enters the free-text word-drop
loop. This is deliberate: free-text search on a bare postal code returns
wrong-country garbage (documented examples: "08000" to a random Ohio motel).
If a postal lookup produces wrong results, fix the classifier or the structured
resolver, never route it back through the free-text path.

### 10. `user_max_age_days` is floored at 1 in `Store.__init__`

`store.py:124-125`: a configured value of 0 or negative would set the prune
cutoff to `now` and wipe every tracked user (including their opt-out flags)
on the first flush. The floor prevents this. The same defense is duplicated
in `config.py:100-103` for the rate-limiter cooldowns.

---

## Implicit contracts

### Module handler signature: `async def cmd_x(self, nick, reply_to, arg)`

`arg` is `None` when the user types just the command with no argument, not an
empty string. Modules must check `if not arg:` (which catches both `None` and
empty) or `if arg is None:` specifically. The dispatch path in `internets.py`
passes `None` explicitly when no argument text follows the command word.

### `on_raw(line)` must be fast and must not raise

`on_raw` is called synchronously on the event-loop thread for every inbound
IRC line. A slow implementation blocks all dispatch. A raised exception is
caught by the fanout loop (`internets.py:878-882`) so one module cannot break
the pipeline, but it is logged as an error. Modules should do minimal work in
`on_raw` and defer anything expensive.

### `on_raw` never receives PING or PONG lines

Both branches in `_process` return before the module fanout at line 878. A
module's `on_raw` that expects to see keepalive traffic will never fire. A
line consisting only of tags (empty string after `strip_tags`) does reach
`on_raw`.

### `setup(bot)` is the only module entry point

There is no class auto-discovery. The loader calls `mod.setup(self)` and
expects a `BotModule` instance back. A file with no top-level `setup`
function is rejected at load.

### `COMMANDS` values must name `async def` methods

`BotModule.__init_subclass__` (`base.py:220`) validates at class-definition
time that every value in `COMMANDS` names a method on the class and that it
is a coroutine function. A typo or a sync handler raises `TypeError` at
import/load, not at first invocation. This is verified by
`inspect.iscoroutinefunction` (not the deprecated `asyncio` alias).

### Provider methods must accept `**kw`

The dispatcher forwards caller kwargs verbatim. A provider method without
`**kw` raises `TypeError` on an unrelated caller's kwarg (e.g., `area=` for
alerts) and is logged as a provider bug.

### `strip_ctrl` is the sanitizer, not the sender

The sender strips only `\r\n\x00` as a transport backstop. `strip_ctrl`
(`base.py:177`) strips the full C0 range plus DEL. Any upstream-derived text
reaching an IRC line must go through `strip_ctrl`. A module that
intentionally includes `\x02` (bold) in assembled output must strip
individual untrusted fields through `strip_ctrl` first, then strip only
transport bytes (`\r\n\x00`) from the assembled line.

### `fetch_json` returns `None` on 404 only when `allow_404=True`

Without the flag, a 404 raises `HTTPError`. Modules that use lookup-or-miss
semantics (dictionary word, Pokemon name, GreyNoise unseen IP) pass
`allow_404=True` and check for `None`.

### `cred()` returns empty string for unconfigured secrets, not `None`

A module checking `if not cred(...)` catches both missing and placeholder
values. `is_configured()` typically delegates to this check to hide the
module's commands from `.help` when no key is set.

---

## Ordering requirements

### Import-time execution order

`config.py` -> `secret_store.py` -> `botlog.py` -> everything else. Both
`config.py` and `botlog.py` can `sys.exit(1)` before the event loop starts
(missing config file, bad password hash prefix, invalid mode strings).

### Registration then MOTD then channels

The read loop (`internets.py:1182`) sends `PASS`/`CAP LS`/`NICK`/`USER` first.
After the MOTD end (numeric 376 or 422), it: ends CAP if still busy, applies
user modes, falls back to NickServ `IDENTIFY` if SASL didn't already identify,
sends `OPER` if configured, and starts the keepalive and rejoin background
tasks. The rejoin task waits up to 10s for NickServ identification before
rejoining saved channels, so services have time to cloak before the bot joins
channels and exposes its real host.

### Graceful shutdown order

`graceful_shutdown` (`internets.py:537`): save channels -> unload all modules
(each gets `on_unload` to flush state) -> stop the store flush thread with a
final write -> enqueue QUIT at priority 0 -> sleep 2s for the sender to drain
-> stop sender -> close socket -> cancel background tasks -> stop metrics ->
flush logging handlers. This order is load-bearing: modules must unload before
the store stops (so they can flush through the store), and logging handlers
must flush last (so shutdown events are captured).

### Lock release before `execv` on restart

`_main` releases the process lock before `os.execv` (`internets.py:1458-1468`).
`execv` preserves the PID, so leaving the lockfile in place would make the new
process image see its own old PID as a live holder and refuse to start.

---

## Timing assumptions

### Keepalive: 90s ping interval, 240s pong timeout

The bot sends `PING` every 90s. If no `PONG` is received within 240s, it
assumes a half-open connection and forces a reconnect. These constants
(`_PING_INTERVAL`, `_PONG_TIMEOUT`) are hardcoded, not configurable.

### Backoff: 15s, 30s, 60s, 120s, 240s, then capped at 300s

Reconnect backoff is `min(15 * 2^attempt, 300)` with +/-25% jitter via
`random.SystemRandom`. The jitter prevents thundering herd on a network
split affecting multiple bots. Attempt counter resets to 0 on every
successful connect.

### Store flush: every 30s

The background flush thread writes dirty datasets every 30 seconds. Worst-case
data loss on a hard crash is ~30s of user-tracking timestamps. Channel and
location changes are also flushed on shutdown/restart/signal.

### Command timeout: 60s

Each command handler runs under `asyncio.wait_for(..., timeout=60)`. A
wedged handler is cancelled after 60s, freeing one of the 50 task slots. The
user receives a timeout notice.

### Weather dispatch: 45s chain, 30s per call

The full provider fallback chain has a 45s budget. Each individual provider
call is capped at 30s (or the remaining chain budget, whichever is less).
Both nest under the 60s command timeout.

### Circuit breaker: 5 failures in 60s, 60s cooldown

A provider that fails 5 consecutive times within a 60s window is removed
from the dispatch chain. After a 60s cooldown, it re-enters as `half_open`
and gets one probe call. Success -> closed (normal). Failure -> open (another
60s cooldown). Auth failures (401/403) trip the breaker immediately.

### Geocode cache: 24h TTL, 1000-entry LRU

Per Nominatim ToS. Negative results (failed lookups) are also cached to
prevent repeated hammering.

---

## Things "everyone knows"

### The bot runs from a deployed copy, not the repo checkout

The live instance runs from `~/Desktop/bot (copy 1)` (per project memory),
not from `~/Internets`. Deploys copy files to that directory. Check the
process's `cwd` to confirm which copy is live.

### `config.ini` and `config.ini.example` are different files with different purposes

`config.ini.example` is the committed, credential-free template. `config.ini`
is the gitignored live config with real values and the `[secrets]` section.
Never edit `config.ini.example` with real values. Never commit `config.ini`.

### `.restart` does a full process restart via `os.execv`

It is not a soft reload. The entire Python interpreter is replaced. All
`sys.modules` cache is cleared. This is the only way to pick up changes to
helper modules, `config.py` constants, core files, or dependencies.

### `config.local.ini` overlays `config.ini`

It is read after `config.ini` and wins on conflicts. It is the place for
non-secret personal overrides (e.g., `password_hash`). It is never committed.

### The bot refuses to start as root

`_entry()` checks `os.geteuid() == 0` on POSIX and exits unless
`INTERNETS_ALLOW_ROOT=1` is set. This is a safety measure, not a hard
requirement.

### IRC credentials are never sent on plaintext connections

`_tls_or_refuse` gates every credential send. On a plaintext connection,
the bot logs CRITICAL and refuses. This is not configurable.

### Module names are lowercase alphanumeric plus underscore

The loader regex is `^[a-z][a-z0-9_]*$`. A module named `MyModule.py` or
`my-module.py` will be rejected.

### Weather providers register only when their key is present

A keyed provider whose credential is missing in the secret store returns
`None` from its factory and is never registered with the dispatcher. It
does not appear in `.providers` output. Keyless providers (NWS, Open-Meteo,
MET Norway, etc.) always register.

### The `provider_priority` config key is an ordering, not an allowlist

Omitting a provider from the list does not disable it. It registers and
sorts last. To actually exclude a keyed provider, remove its key.
