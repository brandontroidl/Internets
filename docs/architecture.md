# Internets Runtime Architecture

Maintainer manual for the bot core: process structure, the `IRCBot` lifecycle, the
line-parse/dispatch pipeline, the `Sender`, the module loader and its hot-reload
limits, the `Store`, the process lock, and logging. Every claim here cites the file
and line it came from. Read the code alongside it.

Files: `internets.py`, `sender.py`, `store.py`, `config.py`, `modules/base.py`,
`process_lock.py`, `botlog.py`.

---

## 1. Process structure and the event loop

One process, one asyncio event loop, plus a small number of OS threads.

Entry is `_entry()` (`internets.py:1446`). It runs a drop-root guard (refuses to start
as euid 0 unless `INTERNETS_ALLOW_ROOT=1`, `internets.py:1468`), acquires a
`ProcessLock` on `./internets.pid`, and runs `asyncio.run(_main(lock))` inside the
`with` block (`internets.py:1482-1484`). `LockHeld` aborts with exit 1
(`internets.py:1490`). `KeyboardInterrupt` exits 130 (`internets.py:1489`).

`_main()` (`internets.py:1337`) builds the `IRCBot`, optionally starts the Prometheus
exporter when `[metrics] enable = true` (`internets.py:1345`), then creates up to two
top-level tasks:

- `console` - only when `--no-console` is unset AND stdin is an interactive TTY
  (`internets.py:1362`; the TTY check is `console.should_skip_console()`). Skipping on a
  non-TTY refuses admin-equivalent console access to whatever is piped in. That is a
  security reason and it is the only one - there is no EOF spin loop to prevent, since
  the dispatch loop returns on the first `EOFError` (`console.py:81-82`).
- `bot` - `bot.run()` (`internets.py:1367`).

`asyncio.wait(..., FIRST_COMPLETED)` (`internets.py:1369`) blocks until either exits.
If the console exits first while the bot is still running, `_main` calls
`bot.request_shutdown("Console exited")` and waits up to 10s for a graceful stop rather
than cancelling the bot task (which would skip `graceful_shutdown`,
`internets.py:1373-1382`).

Console teardown gotcha: the console is parked in `input()` on a dedicated
`threading.Thread(daemon=True)` (`console.py:144`), blocked on a `read(0)` syscall that
cancelling the asyncio task does not interrupt. It is deliberately NOT an
`asyncio.to_thread` worker: such a worker is non-daemon and on the default executor, so
`asyncio.run`'s `shutdown_default_executor()` would wait forever for it. `_main` closes
`sys.stdin` (`internets.py:1397`) to unblock the read, and the thread being a daemon
guarantees cleanup completes even if it does not return. Full reasoning in section 10.2.

After all tasks drain, if `bot._restart_flag` is set, `_main` flushes and closes log
handlers, releases the process lock, and re-execs: `os.execv` on POSIX, a
`subprocess.Popen` self-relaunch on Windows (`internets.py:1412-1443`). The lock is
released BEFORE `execv` because `execv` preserves the PID; leaving the lockfile in place
would make the new image see its own old PID as a live holder and refuse to start
(`internets.py:1419-1429`).

### Threads

- Event-loop thread: all protocol processing, dispatch, sending, signal callbacks.
- `store-flush` daemon thread: periodic disk writes (`store.py:141`).
- to_thread workers: module handlers offload blocking I/O (HTTP via `requests`, disk,
  password hashing) with `asyncio.to_thread` (`modules/base.py:206`). These run on the
  default executor, off the loop.
- `console-input` daemon thread: parked in `input()` (`console.py:144`). Explicitly a
  raw `threading.Thread(daemon=True)`, **not** an `asyncio.to_thread` worker - see
  section 10.2 for why that distinction is load-bearing rather than stylistic.

Cross-thread mutation is guarded by explicit locks (`threading.Lock`), not by relying on
the GIL, so the design holds under free-threaded / GIL-disabled Python. Locks:
`_mod_lock`, `_auth_lock` (guards `_authed` AND `_nick_hosts`), `_chanops_lock`
(`internets.py:224-226`); the three `Store` dataset locks; the `RateLimiter` lock; the
`Sender._seq_lk`.

---

## 2. IRCBot lifecycle

### Construction (`internets.py:219`)

`__init__` wires the `Store` from `[bot]` file paths (`internets.py:242`), the
`RateLimiter(FLOOD_CD, API_CD)` (`internets.py:248`), loads the shadow-ban set from
`shadow_bans.json` (`internets.py:261`), and zeroes the metrics dict (reconnects,
dropped_messages, command_timeouts, oversized_lines, sasl_failures, unexpected_errors -
`internets.py:281`). `_loop`, `_sender`, `_reader`, `_writer`, `_stop` are created later
in `run()`.

ISUPPORT-derived state starts with safe RFC defaults: `_chanmode_types`
(`internets.py:237`) and `_prefix_modes = set("qaohv")` (`internets.py:241`); both get
overwritten from the server's `005` line.

### `run()` (`internets.py:1103`)

1. Captures the running loop and creates `self._stop = asyncio.Event()`
   (`internets.py:1104-1105`).
2. POSIX signal handlers (`internets.py:1109-1119`): `SIGTERM`/`SIGINT` ->
   `_on_signal`; `SIGHUP` -> `_on_sighup`. On Windows the loop signal API is
   unsupported, so it relies on `KeyboardInterrupt` + the console
   (`internets.py:1120-1123`).
3. `autoload_modules()` (`internets.py:1125`) loads each name in `[bot] autoload`.
4. Initial connect loop with jittered backoff (`internets.py:1129-1145`): retries
   `_connect()` until success or `_stop`, sleeping on `_stop.wait()` so a shutdown during
   backoff breaks out immediately.
5. Registration + read loop (`internets.py:1150-1291`).
6. `await self.graceful_shutdown()` on exit (`internets.py:1292`).

### `_connect()` (`internets.py:704`)

Reads `[irc] ssl` (default true) and `ssl_verify` (default true). Records
`self._tls_active` for the credential-send guard (`internets.py:708`; see
`_tls_or_refuse`, `internets.py:686`). TLS context defaults to TLS 1.3 minimum;
`INTERNETS_ALLOW_TLS12=1` lowers it to 1.2 with a loud warning
(`internets.py:721-728`). `ssl_verify=false` disables hostname + cert checks and warns
on every connect (`internets.py:729-742`).

The socket is `asyncio.open_connection(SERVER, PORT, ssl=ssl_ctx, limit=self._READ_LIMIT)`
(`internets.py:746`). `_READ_LIMIT = 8192` (`internets.py:162`) is the inbound stream
buffer cap. A server line longer than this triggers `readline()` to raise (handled as an
oversized line, see below) - it is the inbound counterpart to the `Sender`'s outbound
`MAX_QUEUE`, and the two are unrelated bounds.

`_connect` resets per-connection state, sets `_last_pong = time.monotonic()` for the
keepalive clock (`internets.py:752`), clears `_chanops`, stops any old `Sender`, and
creates a fresh `Sender(self._loop, on_drop=self._bump_dropped_metric)` then `.start`s it
on the new writer (`internets.py:755-757`). The `on_drop` callback is what makes the
shutdown summary's `dropped=` count real rather than always zero
(`internets.py:297-305`).

### Registration and the read loop (`internets.py:1150`)

On first pass (`registered` false): optionally `PASS` (gated on TLS), then `CAP LS 302`,
`NICK`, `USER` (`internets.py:1153-1159`). All at priority 0.

The read loop does NOT block naively on `readline()`. It races two tasks with
`asyncio.wait(FIRST_COMPLETED)` (`internets.py:1166-1174`):

- `read_task` = `asyncio.wait_for(self._reader.readline(), timeout=self._READ_TIMEOUT)`
  (`_READ_TIMEOUT = 300`, `internets.py:163`).
- `stop_task` = `self._stop.wait()`.

This makes `.shutdown` / SIGINT react immediately instead of waiting up to ~5 minutes for
the next server line (`internets.py:1160-1165`). If `_stop` won, the read task is
cancelled/drained and the loop breaks (`internets.py:1179-1189`).

Read outcomes:
- Read timeout -> raised as `ConnectionResetError("Read timeout ...")`
  (`internets.py:1192`), routing into the reconnect handler.
- Oversized line -> `ValueError`/`LimitOverrunError`: increments `oversized_lines`,
  drains to the next newline so the truncated tail is not parsed as a spurious line, and
  `continue`s (`internets.py:1195-1208`).
- Empty bytes -> `ConnectionResetError("Server closed connection")`
  (`internets.py:1209`).
- Otherwise decode (`errors="replace"`), strip CRLF, skip blank, log (AUTH lines
  redacted, `internets.py:1212-1214`), and call `self._process(line)`.

MOTD gate: on the first `376`/`422` (`_RE_MOTD`, `internets.py:1216`) the bot ends CAP if
still busy, applies `user_modes`, falls back to NickServ `IDENTIFY` if SASL did not
already identify (`internets.py:1221-1223`), sends `OPER` if configured, and starts the
`keepalive` and `rejoin` background tasks (`internets.py:1226-1227`). The `identified`
flag makes this fire once per connection.

### Reconnect (`internets.py:1229`)

Catches `ConnectionResetError`, `ConnectionAbortedError`, `BrokenPipeError`,
`ssl.SSLError`, `OSError`. If `_stop` is set, breaks instead of reconnecting
(`internets.py:1230`). Otherwise: increments `reconnects`, computes whether the failure is
permanent (SASL hard-failed AND >=3 SASL failures AND no NickServ fallback,
`internets.py:1242-1244`), cancels and clears all background tasks, stops the sender,
clears `_authed` and `_nick_hosts` under `_auth_lock` (`internets.py:1249-1254`), and
resets `identified/registered`. A permanent failure logs CRITICAL and breaks
(`internets.py:1256-1259`); otherwise an inner loop retries `_connect()` with jittered
backoff until success or `_stop` (`internets.py:1261-1278`).

Backoff: `_backoff(attempt)` is `min(15 * 2**attempt, 300)` (`internets.py:109-115`);
`_backoff_jittered` adds +/-25% equal jitter via `random.SystemRandom`
(`internets.py:124-134`). So 15s, 30s, 60s, 120s, 240s, then capped at 300s, each spread
by jitter to avoid a thundering herd. `attempt` resets to 0 on every successful connect.

`asyncio.CancelledError` breaks the loop cooperatively (`internets.py:1279`). Any other
exception increments `unexpected_errors`, logs with traceback, and sleeps
`_UNEXPECTED_SLEEP_S` (5s) on `_stop.wait()` before retrying (`internets.py:1283-1291`).

### Keepalive (`internets.py:763`)

Every `_PING_INTERVAL` (90s) it checks `time.monotonic() - self._last_pong`. If that
exceeds `_PONG_TIMEOUT` (240s) the link is presumed half-open: it closes the writer to
force a reconnect now and returns (the read loop sees the dead transport,
`internets.py:770-779`). Otherwise it sends `PING :<server>` at priority 0. `_last_pong`
is refreshed by inbound PONG handling in `_process` (`internets.py:842-845`).

### Graceful shutdown (`internets.py:527`)

Ordered, each step guarded so one failure does not abort the rest:

1. Save channel list to disk first (`internets.py:530`).
2. Unload all modules so they flush their own state (`internets.py:533-537`).
3. `self._store.stop()` - stops the flush thread and forces a final write
   (`internets.py:540`).
4. Enqueue the QUIT at priority 0 (bypasses the token bucket, `internets.py:546`).
5. `await asyncio.sleep(_SHUTDOWN_DRAIN_S)` (2.0s) to let the sender drain the QUIT
   (`internets.py:549`).
6. Stop the sender (`internets.py:552`).
7. Close the socket (`internets.py:556-561`).
8. Cancel remaining background tasks and gather them (`internets.py:563-567`).
9. Stop the metrics server if running (`internets.py:569-574`).
10. Log the metrics summary and flush all log handlers (important before `execv`, which
    skips atexit handlers, `internets.py:575-585`).

### Signals and the use-time prefix read

`request_shutdown` (`internets.py:515`) is idempotent and thread-safe: first reason wins,
sets `_quit_msg`, and `call_soon_threadsafe(self._stop.set)`. The `_shutdown_initiated`
guard stops a second SIGINT during a clean shutdown from rewriting the QUIT message.

`_on_signal` (`internets.py:1294`) ignores a repeat signal once shutdown is in flight and
otherwise calls `request_shutdown`.

`_on_sighup` (`internets.py:1309`) is rehash. It calls `config.reload_config()` (which
re-reads BOTH `config.ini` and `config.local.ini` in order, `config.py:43-64`) and then
clears admin sessions defensively. It deliberately does NOT reload the import-time
credential constants `NS_PW`/`OPER_PW`/`SERVER_PW` - a live credential reload is out of
scope, and the log says so (`internets.py:1319-1332`).

Because config values that are read at USE time DO pick up a rehash, `_cmd_prefix()`
(`internets.py:589`) reads `cfg["bot"]["command_prefix"]` live on every dispatch instead
of using the frozen import-time `CMD_PREFIX`. Without this, a `command_prefix` change via
rehash would take effect for modules (which read `cfg` live) but leave the core dispatch
frozen on the old prefix. It falls back to `CMD_PREFIX` only if the key is absent.

---

## 3. Line parse and dispatch pipeline

### `_process(line)` (`internets.py:827`)

1. `strip_tags(line)` FIRST (`internets.py:834`) - strips an IRCv3 `@tag` block so the
   PING/PONG and every later regex still match on a server-time-tagged line. A tagged PING
   left unanswered would ping-timeout the link.
2. PING -> reflect `PONG :<payload[:400]>` at priority 0 and return
   (`internets.py:835-839`).
3. PONG (command at token 0 or 1) -> `_last_pong = monotonic()` and return
   (`internets.py:842-845`).
4. Shadow-ban prefix filter (`internets.py:852-862`): if the source nick is shadow-banned,
   set `skip_module_fanout` so modules' `on_raw` never sees the line (the banned user is
   invisible to `.seen`/`.tell`/etc). A malformed prefix falls through (logged at debug) so
   modules still see the line.
5. Module `on_raw` fanout over a snapshot of loaded modules, unless skipped; each call is
   try-wrapped so one module cannot break the pipeline (`internets.py:863-867`).
6. `_handle_cap` / `_handle_numeric` / `_handle_membership` / `_handle_privmsg`, first
   match wins (`internets.py:868-871`).

`_handle_cap` (`internets.py:873`) drives CAP LS/ACK/NAK/NEW, SASL PLAIN (`AUTHENTICATE`,
903 success, 902/904/905 failure), and CAP END fallbacks. SASL uses the runtime `_nick`,
not the startup constant, so a 433-bumped nick authenticates as its real session identity
(`internets.py:895-899`). 904/905 set `_sasl_failed_permanently` (`internets.py:915`).

`_handle_numeric` (`internets.py:928`) handles 433 nick-collision (append `_`, then a
random suffix once the length budget is hit, `internets.py:929-932`), 005 ISUPPORT
(`CHANMODES`/`PREFIX` reparse), 473 invite-only (ask services for INVITE), join-error
numerics (discard the channel from the saved set), OPER 381/491, NickServ 900/NOTICE
identify confirmation, 353 NAMES op-tracking, and channel MODE op changes. Op state lives
in `_chanops` under `_chanops_lock`.

`_handle_membership` (`internets.py:984`) maps CHGHOST/ACCOUNT/INVITE/JOIN/PART/KICK/
QUIT/NICK to store updates and `_chanops`/`_nick_hosts` maintenance. Identity-change
security: QUIT and NICK both DROP any admin session bound to the old nick rather than
migrating it, so a nick-takeover cannot inherit an authed session
(`internets.py:1042-1046`, `internets.py:1056-1062`).

### `_handle_privmsg(line)` (`internets.py:1069`)

Parses `:nick!user@host PRIVMSG target :text`. Updates `_nick_hosts[nick]` under
`_auth_lock` (`internets.py:1075`) - this is the live hostmask that admin auth is checked
against. CTCP (`\x01`) is ignored (`internets.py:1077`). `is_pm` is `target == self._nick`;
`reply_to` is the nick in PM else the channel.

Command extraction (`internets.py:1082-1094`): if the text starts with the live prefix,
the first token is the command. In PM ONLY, a bare leading token that matches a known
command also dispatches (so `weather 10001` works in PM without the `.`). The valid-command
set is `_CORE | _commands` under `_mod_lock`. Auth/deauth args are redacted in the log
(`internets.py:1096`). Only known commands reach `_dispatch`.

### `_dispatch(...)` (`internets.py:601`)

Gates, in order:

1. Shadow-banned nick -> silent drop. No reply, no rate-limit consumption, no audit entry;
   the banned user cannot distinguish ignored from offline (`internets.py:607-609`).
2. `auth`/`deauth` outside PM -> told to use PM, abort (`internets.py:610-611`).
3. `flood_limited(nick)` -> NOTICE "slow down", abort. Admins bypass this gate (the
   `is_admin` flag passes through to `RateLimiter.flood_check`, `internets.py:376`).
4. Channel-flood gate for non-PM: `channel_limited(reply_to)` catches coordinated floods
   across many distinct nicks that the per-nick limit cannot see. Silent (log only) so the
   bot does not spam the channel about throttling (`internets.py:617-621`).
5. Arg length > `_MAX_ARG_LEN` (400) -> NOTICE, abort (`internets.py:622`).
6. `_active_cmd_tasks >= _MAX_TASKS` (50) -> NOTICE "bot is busy", abort. This is an O(1)
   counter check, not an O(n) scan of `_tasks` (`internets.py:627-631`).
7. Resolve handler: `_CORE` (built-in admin/meta commands) first, else `_commands` under
   `_mod_lock` (`internets.py:633-640`).
8. If resolved: increment `_active_cmd_tasks` and stats, bump the Prometheus
   `commands_total`, create an `asyncio.Task` running `_run_cmd`, append to `_tasks`, and
   register a done-callback that decrements the counter and removes the task
   (`internets.py:641-661`).

Every command runs as its own task; the bot does not await handlers inline.

### `_run_cmd(...)` (`internets.py:663`)

Wraps the handler in `asyncio.wait_for(..., timeout=self._CMD_TIMEOUT)` (60s) so a wedged
handler cannot permanently hold one of the 50 task slots and eventually starve every
command including admin ones (`internets.py:667-670`). `TimeoutError` -> increment
`command_timeouts`, NOTICE the user (`internets.py:671-675`). `CancelledError` is
re-raised (it is shutdown, not a timeout, `internets.py:676`). Any other exception ->
increment `unexpected_errors`, log with traceback, send a GENERIC error notice (no stack
trace or internal state to IRC, `internets.py:679-682`).

---

## 4. Sender (`sender.py`)

An async drain loop over an `asyncio.PriorityQueue` with token-bucket rate limiting.

### Queue and priorities

`PriorityQueue(maxsize=MAX_QUEUE)` with `MAX_QUEUE = 200` (`sender.py:44`,`49`). Items are
`(priority, seq, msg)` (`sender.py:140`). `priority` 0 is protocol traffic (PONG, CAP,
NICK, QUIT) and bypasses the token bucket; priority 1 is normal output (PRIVMSG, NOTICE,
JOIN) and is rate-limited. `seq` is a monotonic counter guarded by `_seq_lk`
(`sender.py:137-139`) that makes the heap a stable FIFO within a priority and keeps the
non-comparable `msg` string out of the heap comparison.

`enqueue()` is thread-safe (`sender.py:135`): modules call it from to_thread workers, so
it never touches the queue directly - it `call_soon_threadsafe(self._safe_put, item)` to
hop onto the loop thread (`sender.py:141`).

### `_safe_put` and overflow (`sender.py:91`)

On the loop thread, tries `put_nowait`. On `QueueFull`:

- Priority 0 MUST NOT be dropped (losing a PONG causes a ping-timeout disconnect and a
  reconnect storm worse than the overflow). It reaches into the heap (`_q._queue`), finds
  the worst (highest priority/seq) entry, evicts it, re-heapifies, counts the eviction as a
  drop, and inserts the priority-0 item (`sender.py:105-126`). If eviction somehow fails it
  logs loudly and never silently drops the priority-0 message (`sender.py:127-130`).
- Priority >0 is dropped with a warning and counted (`sender.py:131-133`).

### Drop accounting (`sender.py:77`)

`_drop()` bumps the Prometheus `dropped_messages_total` AND, if the bot wired one, calls
the `on_drop` callback. The bot passes `_bump_dropped_metric` (`internets.py:756`) so its
in-process `dropped_messages` counter - the honest source for the shutdown summary - is
real. The callback runs on the loop thread inside `_drop` and is exception-guarded so a
counter bump can never break sending (`sender.py:85-89`).

### Drain loop and token bucket (`sender.py:206`)

`CAPACITY = 5` burst, `REFILL = 1.5s` per token (~40 msg/min sustained, `sender.py:42-43`).
The loop `await`s `self._q.get()` with a 0.25s timeout; on timeout it replenishes tokens
even while idle and loops (`sender.py:212-219`). For each dequeued item it replenishes,
then for priority >0 it spins (`asyncio.sleep(0.05)`) until a token is available and
consumes one; priority 0 skips the wait entirely (`sender.py:225-232`). Then `_write_line`
+ `await writer.drain()` (`sender.py:234-241`).

### `_write_line` (`sender.py:181`)

Transport hardening on every outgoing line: strips embedded CR/LF/NUL (protocol-injection
defense, `sender.py:184`), enforces the 512-byte RFC 2812 line limit reserving 2 for CRLF
and trimming on a UTF-8 boundary (`sender.py:186-192`), and redacts credentials from the
log by matching `_REDACT_OUT` prefixes (`PASS`, `OPER`, the NickServ/ChanServ IDENTIFY and
REGISTER spellings, `NS IDENTIFY`, `AUTHENTICATE`, ...) case-insensitively
(`sender.py:148-176`,`193-198`). The wire still carries the real value; only the log line
is redacted. `_write_line` only buffers; the `drain()` in the loop flushes to the OS.

`start()` replaces the queue and resets `_seq` (`sender.py:59-65`); `stop()` cancels the
drain task and awaits it (`sender.py:67`).

---

## 5. Module loader and hot-reload

### Load / unload / reload

`load_module(name)` (`internets.py:452`), all under `_mod_lock`:

1. Validate the name against `^[a-z][a-z0-9_]*$` (`internets.py:454`).
2. Reject if already loaded (`internets.py:456`).
3. Require `modules/<name>.py` to exist (`internets.py:459`).
4. Symlink/escape guard: `path.resolve().relative_to(MODULES_DIR.resolve())` - blocks a
   module path that escapes the modules directory (`internets.py:462-464`).
5. `spec_from_file_location("modules.<name>", path)` + `module_from_spec` +
   `spec.loader.exec_module(mod)` (`internets.py:466-468`). Require `setup`, call
   `mod.setup(self)` to build the `BotModule` instance.
6. Command-conflict check: reject if any command in `inst.COMMANDS` is already owned by a
   different module (`internets.py:472-474`).
7. `on_load()`, register the instance and its commands into `_modules`/`_commands`
   (`internets.py:475-478`).

`unload_module` (`internets.py:487`) calls `on_unload()`, removes the module's commands and
the instance. `reload_module` (`internets.py:504`) is unload-then-load. `cmd_reloadall`
(`admin_cmds.py:409`) snapshots the loaded names and reloads each.

The `BotModule.COMMANDS` contract is validated at class-definition time
(`__init_subclass__`, `modules/base.py:220`): each mapped method must exist and be an
`async def`, turning a typo or a sync handler into a startup `TypeError` instead of a
runtime failure when a user first runs the command.

### The hot-reload gotcha (read before editing helpers)

`exec_module` is used deliberately INSTEAD of `importlib.reload`, and the new module object
is never inserted into `sys.modules`. So each load/reload re-executes the command file
fresh from disk - edits to `modules/weather.py` ARE picked up by `.reload weather`.

The asymmetry: command modules import shared helpers with normal relative imports, e.g.
`from .geocode import geocode` in `modules/weather.py:21` and `modules/location.py:5`. That
import goes through the standard import machinery, which DOES cache `modules.geocode` in
`sys.modules`. Re-executing `weather.py` re-runs its `from .geocode import geocode`, but
the machinery finds the already-cached `modules.geocode` and rebinds to it without
re-reading `geocode.py` from disk.

Consequence: `.reload weather` and `.reloadall` refresh the COMMAND modules but NOT helper
modules like `geocode` or `units`. An edit to `modules/geocode.py` is invisible until a
full process restart. Use `.restart` (`admin_cmds.py:424`), which sets `_restart_flag` and
requests shutdown; `_main` then `execv`s a brand-new interpreter (`internets.py:1412-1443`)
with an empty `sys.modules`, so every file is re-read.

---

## 6. Store (`store.py`)

In-memory state with a periodic background flush. Three independent datasets - locations,
channels, users - each with its own lock so a weather location read never blocks behind a
user-tracking write (`store.py:127-129`).

### Construction and load

`__init__` (`store.py:118`) floors `user_max_age` at 1 day (a 0/negative value would make
the prune cutoff `== now` and wipe every tracked user plus their opt-out flags on the first
flush, `store.py:124-125`), `_read`s each file once, and starts the `store-flush` daemon
thread (`store.py:141-143`).

### Schema, checksum, quarantine

Two on-disk shapes (`store.py:42-52`):
- v1 (legacy): the bare payload.
- v2 (current): `{"schema": 2, "checksum": "<sha256>", "data": <payload>}`.

`_read` (`store.py:149`): rejects files over `_MAX_FILE_SIZE` (10 MB, `store.py:147`), JSON-
loads, then `_unwrap` (`store.py:83`). `_unwrap` validates a v2 envelope's SHA-256 over the
canonical JSON of `data` and raises `_StoreRejected` on a wrong schema, missing checksum, or
mismatch; a v1 bare payload is returned unchanged and re-wrapped on the next flush. `_read`
also rejects a payload whose type differs from the expected default (a list where a dict was
expected, or vice versa - `store.py:164-166`).

On ANY load failure (`OSError`, JSON error, `_StoreRejected`) the file is NOT silently reset
to empty. `_quarantine` (`store.py:176`) renames it to `<name>.corrupt.<unixts>` and the
dataset starts from the default. This is the key durability invariant: a corrupt or
truncated file is preserved for manual recovery instead of being overwritten by the next
flush, which would otherwise lose saved locations, channel-rejoin state, and privacy opt-out
flags.

### Atomic write with .bak (`store.py:191`)

`_write`: write a v2 envelope to a temp file in the same directory, `chmod 0600` BEFORE the
rename (so the final file - which holds user ZIPs and nick/hostmask/timestamp PII - is never
even momentarily world-readable, POSIX only, `store.py:204-213`), copy the current good file
to `<path>.bak` as a one-deep backup, then `os.replace(tmp, path)` (atomic on POSIX). The
temp file is cleaned up on any failure path (`store.py:226-231`).

### Flush loop and pruning

`_flush_loop` (`store.py:238`) is `self._stop.wait(timeout=_FLUSH_INTERVAL)` (30s) then
`flush()`. A flush exception is logged and swallowed so the persistence thread never dies and
silently stops all future saves (`store.py:242-247`). `flush()` (`store.py:249`) writes only
dirty datasets, each under its own lock; the users write runs `_prune_users` first
(`store.py:259-263`). `stop()` (`store.py:265`) sets the event and does one final flush.

`_prune_users` (`store.py:272`) removes user entries whose `last_seen` is older than
`user_max_age`, EXCEPT records with `opted_out` true - an opt-out is a privacy preference that
must outlive the inactivity window, or the bot would silently resume tracking a user who asked
it not to (`store.py:283-288`). Empty channel dicts are removed.

### User tracking and opt-out

`user_join` records nick/hostmask/first_seen/last_seen and seeds `opted_out=False`
(`store.py:345`). `user_part`/`user_quit`/`user_rename` stamp `last_seen` and re-key on a nick
change (`store.py:363-414`). `user_purge` (`store.py:381`) hard-deletes every record of a nick
across all channels for the `.forgetme` privacy command. `set_opt_out` (`store.py:427`) flips
the flag on every tracked record and, if the nick is untracked, creates a sentinel entry in a
synthetic `"*"` channel so the preference survives a restart before the user next speaks.

### RateLimiter (`store.py:464`)

Lives in `store.py` and backs the dispatch gates. Three windows, all under one lock: per-nick
`flood_check` (default 3s, admins bypass), per-nick `api_check` (default 10s, throttles the
geocode/weather API paths), and per-channel `channel_check` (sliding window, default 20
commands per 10s) that catches coordinated multi-nick floods. Cooldowns are floored at 1s so a
0/negative config value cannot silently disable the limiter (`store.py:489-490`). When a
channel is over budget it refuses WITHOUT recording the new attempt, so an attacker cannot keep
the window pinned full by spamming after the limit hits (`store.py:554-559`).

---

## 7. Process lock (`process_lock.py`)

Single-instance guard so two bots never race on the JSON state files and corrupt them. The
lockfile stores `pid|start_time|hostname` (`process_lock.py:209`).

`acquire()` (`process_lock.py:142`) resolves the path against the CURRENT cwd (resolution is
deferred from `__init__` to `acquire`, `process_lock.py:131-138`), then if a lockfile exists
decides staleness:

- Same host: `_pid_is_alive` via `os.kill(pid, 0)` - alive raises nothing, dead raises
  `ProcessLookupError`/ESRCH; `PermissionError` is treated as live (conservative,
  `process_lock.py:61-81`).
- Different host: cannot probe it, so refuse conservatively (`process_lock.py:161-165`). The
  operator deletes the file by hand if sure.
- Live -> raise `LockHeld`. Dead -> remove and continue. Unknown (Windows without `psutil`) ->
  fail open with a warning. Corrupt/unreadable -> remove and continue
  (`process_lock.py:166-193`).

Creation is atomic via `os.open(..., O_CREAT | O_EXCL | O_WRONLY)`; losing the race raises
`LockHeld` (`process_lock.py:195-206`). `release()` (`process_lock.py:220`) re-reads the file
and only unlinks if the recorded PID is still ours, so it never deletes another instance's lock.

Restart interaction (see Section 1): `execv` preserves the PID, so `_main` releases the lock
before re-exec; otherwise the new image would see its own preserved PID as a live holder and
`LockHeld`.

---

## 8. Logging (`botlog.py`)

The `internets` root logger is configured at import time (`botlog.py:112`), set to DEBUG with
all handlers cleared and rebuilt. Per-subsystem child loggers (`internets.conn`,
`internets.dispatch`, `internets.modules`, `internets.signal`, `internets.shutdown`,
`internets.sasl`, etc., `internets.py:71-76`) inherit from it and give operators per-subsystem
debug control.

Handlers (`botlog.py:121-140`): a `RotatingFileHandler` on `LOG_FILE` (max_bytes default 5 MB,
backup_count default 3), a `StreamHandler` to stdout, and - only if `[logging] debug_file` or
`--debug-file` is set - a second rotating handler capturing everything at DEBUG regardless of
the base level.

`_SafeFormatter` (`botlog.py:28`) strips C0 controls (except TAB), DEL, and C1 controls from
`record.msg` and `record.args` on a COPY of the record, defending against log injection via
user-controlled `%s` interpolation. Tracebacks survive because they render into `exc_text`
later, not into `msg`/`args`.

`DebugFilter` (`botlog.py:64`) is attached to the file and console handlers and passes a record
if its level >= `base_level`, OR `global_debug` is on, OR its logger name matches an enabled
subsystem. `.loglevel`/`.debug` (and the console equivalents) drive it through `apply_loglevel`
/`apply_debug` (`botlog.py:237-303`).

Startup validation at import (`botlog.py:180-229`):
- `_validate_hash`: an empty `password_hash` is NOT fatal (auth is disabled with a warning,
  intentional for first run); a non-empty hash with an unrecognized algorithm prefix is
  fail-closed via `sys.exit(1)`, because an unknown prefix would make `verify_password` raise on
  every auth attempt and silently disable admin commands.
- A world-readable `config.ini` triggers a chmod warning (POSIX, `botlog.py:213-219`).
- `user_modes`/`oper_modes`/`oper_snomask` are validated against `^[a-zA-Z+\- ]*$`; an invalid
  value is fail-closed via `sys.exit(1)` (`botlog.py:221-227`).

Log-flush discipline: handlers are flushed in `graceful_shutdown` (`internets.py:583-585`) and
again before `execv` in `_main` (`internets.py:1408-1411`,`1416-1418`), because `execv` replaces
the process image without running atexit handlers - unflushed log records would be lost across a
restart.

## 9. Protocol helpers (`protocol.py`)

Pure functions over strings. No bot state, no I/O, no logging, no imports beyond
`base64` and `re`. Extracted from `internets.py` so the bot class holds
orchestration and state while parsing stays independently testable
(`tests/test_protocol.py`, plus the protocol block in `tests/run_tests.py`).

The read loop decodes with `errors="replace"` (`internets.py:1210`), so every
string reaching these functions is already valid UTF-8 with U+FFFD substituted
for undecodable bytes. That is what lets the wire-facing parsers be written
without defensive decoding.

### 9.1 The five wire-facing parsers are total

`strip_tags`, `parse_isupport_chanmodes`, `parse_isupport_prefix`,
`parse_mode_changes` and `parse_names_entry` return a value for any `str`. They
do not raise, do not log, and have no error branch to test. A hostile or
truncated line from the server degrades to an empty or partial result rather
than an exception, so a malformed line cannot take the read loop down.

`sasl_plain_payload` (`protocol.py:105-108`) is the exception and is not
wire-facing: it encodes with `.encode("utf-8")` (`protocol.py:107`), which
raises `UnicodeEncodeError` on any surrogate codepoint. Its inputs are
`self._nick` and the configured NickServ password (`internets.py:899`), not
server output, and the `errors="replace"` decode above means a surrogate cannot
arrive from the wire in the first place.

### 9.2 Where the parsers sit in the pipeline

`_process` (`internets.py:833`) calls `strip_tags` first (`internets.py:834`),
then dispatches in a fixed order:

| step | line | effect |
|---|---|---|
| `strip_tags` | 834 | IRCv3 tag prefix removed |
| PING | 835-839 | replies PONG, **returns** |
| PONG | 843-845 | marks link live, **returns** |
| module `on_raw` fan-out | 866 | every loaded module sees the line |
| `_handle_cap` / `_handle_numeric` / `_handle_membership` | 868-870 | first match **returns** |
| `_handle_privmsg` | 871 | fallthrough |

Two consequences a module author needs:

- **Modules never receive PING or PONG lines.** Both branches return before the
  fan-out at 866. Do not write an `on_raw` that expects to see keepalive
  traffic.
- A line consisting only of tags becomes `""` after `strip_tags`. It survives
  the PING/PONG checks and still reaches every module's `on_raw`, so `on_raw`
  implementations must tolerate an empty string.

### 9.3 ISUPPORT parsing and the caller's storage decision

`parse_isupport_chanmodes` (`protocol.py:21-36`) splits `CHANMODES=A,B,C,D` into
`{mode: type}`. Missing trailing groups are tolerated (`protocol.py:33`); a
fifth or later group is ignored, because the loop is over the four literal
labels (`protocol.py:32`).

`parse_isupport_prefix` (`protocol.py:39-51`) parses `PREFIX=(modes)symbols`.
The regex is `re.match` (`protocol.py:45`), so it is anchored at string start -
any leading junk before `(` yields `(set(), {})`. The symbol map is zipped to
`min(len(modes), len(symbols))` (`protocol.py:50`), so a mismatched token
truncates rather than raising.

The parsers themselves are safe. **The risk is in what the caller does with the
result.** Both assignments are unconditional once the outer regex matched:

```python
if cm: self._chanmode_types = parse_isupport_chanmodes(cm.group(1))   # internets.py:935
if pm: self._prefix_modes, _ = parse_isupport_prefix(pm.group(1))     # internets.py:937
```

A 005 line carrying no `CHANMODES=`/`PREFIX=` token is harmless - the guard
skips the call and the RFC-safe constructor defaults survive
(`internets.py:237-241`). But a token that is *present and malformed* replaces
those defaults with the degraded parse. For `PREFIX`, a value that fails the
anchored match stores an empty `_prefix_modes`, and `op_modes = {"o","a","q"} &
self._prefix_modes` (`internets.py:971`) is then empty, so MODE-driven chanop
tracking silently stops. See section 9.6.

`_chanmode_types` and `_prefix_modes` are also **not reset on reconnect**.
`_connect` clears `_caps` and `_chanops` (`internets.py:748-754`) but leaves the
ISUPPORT tables holding the previous connection's values until a new 005
arrives.

The symbol map from `parse_isupport_prefix` is discarded at the call site
(`internets.py:937` binds it to `_`). Only the mode set is kept, and it is used
on the MODE path (`internets.py:971`). The NAMES path does not consult it - see
9.5.

### 9.4 `parse_mode_changes`: parameter alignment

`parse_mode_changes` (`protocol.py:54-89`) turns a MODE string into
`[(adding, mode_char, param)]`. Getting this wrong desynchronises every
following parameter, which is why the ISUPPORT types are parsed at all.

Three behaviours worth knowing:

- **`adding` defaults to `True`** (`protocol.py:66`), so a mode string with no
  leading sign is treated as additive. The caller's regex requires a leading
  `+`/`-`, so this is unreachable from the wire but matters if you call the
  function directly.
- **`prefix_modes` is consulted before `chanmode_types`** (`protocol.py:80` vs
  `:83`). A mode char in both tables is unconditionally treated as
  parameter-taking, whatever its declared ISUPPORT type.
- **Argument exhaustion is sticky.** `take_param` increments `arg_idx` even when
  `args` is already exhausted (`protocol.py:71-72`), so once the parameters run
  out every subsequent parameter-taking mode also gets `None`. The index is
  never rewound.

At the call site the result is filtered hard: only `{"o","a","q"}` intersected
with the advertised prefix modes (`internets.py:971`), and only changes carrying
a truthy parameter (`internets.py:979`), drive `_chanops`. Halfop and voice are
parsed and then discarded.

### 9.5 `parse_names_entry` and its hardcoded prefix set

`parse_names_entry` (`protocol.py:92-102`) strips the literal set `~&@%+`
(`protocol.py:97`) and reports op status for `~`, `&`, `@` only
(`protocol.py:101`) - halfop and voice are not chanops. An entry that is
entirely prefix characters returns `(entry, False)` rather than an empty nick
(`protocol.py:98-99`).

That set is a literal, not the PREFIX symbol map the bot already parsed and
threw away. On a network advertising a prefix symbol outside `~&@%+`, `lstrip`
leaves the symbol attached and the returned "nick" carries it into `_chanops`
(`internets.py:966-967`). This is the one place the discarded symbol map would
have earned its keep.

### 9.6 Gotchas

- **A malformed `PREFIX=` token disables op tracking silently.** Covered in 9.3.
  There is no log line and no fallback to the constructor default; ops simply
  stop being recorded. If chanop state is ever mysteriously empty on one
  network, check the 005 line before anything else.
- **NAMES only ever adds ops.** `internets.py:964` uses
  `setdefault(chan, set())` and never clears the channel's set first, so a NAMES
  refresh on an already-joined channel cannot remove someone deopped in the
  interim. Removal happens through MODE (`internets.py:979`), PART/KICK
  bookkeeping, or `_on_part` dropping the channel entirely.
- **The MODE branch is channel-only.** `internets.py:970` requires the target to
  start with `#`, `&`, `+` or `!`, so a user-MODE line is not parsed here.
- Adding a parser here means adding it to `tests/test_protocol.py`. These
  functions are pure, so they are the cheapest thing in the repo to test
  exhaustively, and the read loop's resilience depends on them staying total.

## 10. Interactive console (`console.py`)

An optional stdin REPL for the operator at the terminal running the bot. Enabled
by default when stdin is a TTY, suppressed by `--no-console` or automatically
when stdin is not interactive (`internets.py:1362-1367`).

### 10.1 The console is an unauthenticated admin surface

`run_console` logs a deliberate warning on entry (`console.py:121-126`): the
console grants admin-equivalent capability - `debug`, `loglevel`, `status`,
`shutdown` - with **no authentication at all**. There is no password prompt and
no `is_admin` check anywhere in this module, because the trust boundary is
physical access to the process's stdin, not an IRC identity.

That is why `should_skip_console` (`console.py:42-58`) exists and why it fails
safe: it returns `True` when `sys.stdin.isatty()` is false (`console.py:56`), and
also on `AttributeError`/`ValueError` - no stdin at all, or already closed
(`console.py:57-58`). Under systemd, in a container without `-it`, or with stdin
redirected from a file, whatever bytes arrive on stdin would otherwise be
executed with that capability.

`_print_status` also discloses live auth state: it prints the currently
authenticated admin nicks to stdout (`console.py:162-164`).

### 10.2 Why a daemon thread and not `asyncio.to_thread`

This is the load-bearing design decision in the module, documented at
`console.py:109-117`. It is restated here because the obvious "cleanup" reverts
it and reintroduces a hang that is tedious to diagnose.

`input()` parks its thread on a blocking read that nothing short of process death
interrupts. An `asyncio.to_thread` worker runs on the default executor and is
**not** a daemon, so `asyncio.run()`'s cleanup calls
`loop.shutdown_default_executor()` and waits forever for that input-blocked
worker to return. The observed symptom of the older design was the whole process
hanging on the last shutdown log line until the operator hit Ctrl-C.

So the thread is created explicitly:

```python
t = threading.Thread(target=_wrap, daemon=True, name="console-input")  # console.py:144
```

A daemon thread cannot hold up interpreter shutdown, so cleanup completes even if
it never returns. Do not convert this to `asyncio.to_thread`.

### 10.3 Crossing back into the event loop

`run_console` creates an `asyncio.Event` and awaits it (`console.py:129`,
`:147`); the worker sets it through `loop.call_soon_threadsafe(done.set)` inside
a `finally` (`console.py:136-142`). That call is wrapped in
`try/except RuntimeError` because the loop may already be closed during a
shutdown race.

`_wrap` also catches every exception out of the dispatch loop and logs it
(`console.py:134-135`). A crash therefore does not propagate: `done` is still set
by the `finally`, the console silently disappears, and `_main` treats the
completed task as "console exited" (`internets.py:1373-1382`).

The three dispatched actions are safe to call from off-loop, each for its own
reason (`console.py:66-73`): `apply_debug`/`apply_loglevel` mutate logger state,
`_print_status` reads bot fields through their lock-guarded accessors
(`console.py:159`, `:162`), and `bot.request_shutdown` uses
`loop.call_soon_threadsafe` internally.

### 10.4 Command surface and parsing

| command | effect |
|---|---|
| `help` | prints `_CONSOLE_HELP` (`console.py:87-88`) |
| `debug [subsystem ...]` | `apply_debug` (`console.py:89-90`) |
| `loglevel ...` | `apply_loglevel`, prints the returned error if any (`console.py:91-93`) |
| `status` | `_print_status` (`console.py:94-95`) |
| `shutdown` / `quit` | requests shutdown and returns (`console.py:96-100`) |
| anything else | `Unknown command: ... - type 'help' for commands.` (`console.py:101-102`) |

Blank lines are skipped (`console.py:83-84`). The loop exits on `EOFError`
(Ctrl-D), `KeyboardInterrupt` (Ctrl-C) or `ValueError` (stdin closed mid-read)
(`console.py:81-82`), and on `shutdown`/`quit`. A bare `shutdown` with no
argument uses the reason `"Console shutdown"` (`console.py:97`).

**Only the command word is lowercased** (`console.py:86`); arguments keep their
case. `cmd_debug` on the IRC side lowercases the entire argument string
(`admin_cmds.py:888`), so `debug WEATHER` at the console and `.debug WEATHER`
over IRC do not register the same subsystem. The console form preserves
`WEATHER`, which matches no real logger name, so it prints a confirmation and
changes nothing. Use lowercase subsystem names at the console.

`help` is dispatched (`console.py:87`) but is not listed in `_CONSOLE_HELP`'s own
output.

### 10.5 Shutdown interaction

`_main` closes stdin and then cancels pending tasks (`internets.py:1397`,
`:1402-1403`). Nothing is awaited between those two statements, so the event loop
cannot deliver `done.set()` before the cancellation is applied. `run_console` -
parked at `await done.wait()` (`console.py:147`) - therefore normally receives
`CancelledError` and re-raises it (`console.py:148-151`). That is the expected
path, not an error path, and there is nothing to clean up precisely because the
dispatch thread is `daemon=True`.

A console `shutdown` command takes the other route: `request_shutdown` sets the
stop event on the loop, `_main` proceeds with graceful shutdown, and the console
task is cancelled as part of it.

### 10.6 Testing status

There is no `tests/test_console.py`, and `console.py` is listed in the coverage
`omit` set on the grounds that it needs a live loop and a TTY to exercise
(`pyproject.toml`). It is integration-tested by running the bot, not unit-tested.
Treat changes here as unguarded by the suite: the failure mode this module exists
to avoid - a hung shutdown - is exactly the kind that a green test run will not
catch.
