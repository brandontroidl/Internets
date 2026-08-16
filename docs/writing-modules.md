# Writing Modules

How to build a command module that satisfies the contract, passes the suite,
and does not introduce a security regression. A competent Python developer
should be able to work from this page alone.

[Module System](modules.md) describes how modules are discovered, loaded, and
reloaded; this page describes what you put in the file. Read section 3 of that
document (the load path) before your first module, because two of the rules
below exist only because of the order the loader uses.

:::{warning}
`modules/example.py` is a loadable skeleton and is useful as a starting file,
but its comments have drifted from the code. It implies that admins bypass the
per-nick API cooldown (they do not: `IRCBot.rate_limited()` calls
`RateLimiter.api_check()`, which takes no admin argument and has no bypass) and
its `_MAX_INPUT` comment claims to bound input before it reaches a URL, while
the constant is only ever passed to `strip_ctrl` on the way out. Copy the file
if you like; treat this document as the contract.
:::

---

## 1. Before you start

You need three things: a running bot or the test suite, a name nobody has
claimed, and a decision about whether your module talks to the network.

Check the name first. Command words are a single flat global namespace; a
collision aborts the load with a conflict message naming the words, and the
first loader wins.

```console
$ python scripts/gen-command-reference.py | grep -w '`\.mycmd`'
```

An empty result means the word is free. That script is also the drift gate for
[Command Reference](command-reference.md), so you will run it again at the end.

## 2. File placement and naming

One module is one file: `modules/<name>.py`. No packages, no subdirectories.
The loader constructs the path itself and refuses anything whose resolved path
escapes `modules/`, so a symlink out of the tree does not load.

The name must match `^[a-z][a-z0-9_]*$`. `MyMod.py`, `my-mod.py`, and
`_private.py` are all rejected at `.load` time by
`internets.py - IRCBot.load_module()`.

Pick a name that is not already a helper: `base`, `geocode`, `units`, and
`_netsafe` are imported by other modules and are not loadable.

## 3. The `BotModule` contract

```python
from __future__ import annotations

import logging

from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.mymod")


class MyModule(BotModule):
    COMMANDS: dict[str, str] = {
        "mymod": "cmd_mymod",
        "mm":    "cmd_mymod",     # alias: same method, second word
    }

    async def cmd_mymod(self, nick: str, reply_to: str,
                        arg: str | None) -> None:
        ...

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "mymod/.mm <text>", "Echo text back")]


def setup(bot: object) -> MyModule:
    return MyModule(bot)   # type: ignore[arg-type]
```

### `COMMANDS`

Maps a command word, without the prefix, to the **name of a method** as a
string. Several words may point at one method; that is how aliases are made.
The dispatcher registers each word separately, so `.mymod` and `.mm` are two
entries in the global table owned by one module.

`BotModule.__init_subclass__` validates the mapping at class-definition time:
every value must name a method that exists on the class and is a coroutine
function (`inspect.iscoroutinefunction`). A typo or a `def` where you meant
`async def` raises `TypeError` when the file is executed, which means at
`.load` time and at test collection, not the first time a user runs the
command. Lean on it.

### `setup(bot)`

The only required top-level name. The loader calls it and expects a
`BotModule` instance back. Keep it to the constructor call.

`setup()` runs **before** the command-conflict check, so side effects in
`setup()` happen even when the load is subsequently rejected for a collision.
Do configuration work in `on_load()` instead, which runs after the conflict
check has passed.

### Lifecycle hooks

| Hook | Default | Use it for |
|---|---|---|
| `on_load()` | no-op | read config and credentials once; load state; start tasks |
| `on_unload()` | no-op | cancel every task you started; final flush |
| `on_raw(line)` | no-op | passive line watching, synchronously, on the read path |
| `is_configured()` | `True` | report a missing credential (hides from `.help`) |
| `forget(nick)` | returns `0` | erase that nick's records; return the count |
| `help_lines(prefix)` | `[]` | one line per primary command |

`on_load()` raising aborts the load with nothing registered, and `on_unload()`
is not called on that path, so anything acquired before the raise is leaked.
Acquire late and acquire in an order you can unwind.

`on_raw()` runs on the event-loop thread for every inbound line except PING and
PONG, before the CAP, numeric, membership, and PRIVMSG handlers. It must be
fast and must not block. Exceptions are caught and logged at debug level, which
means a raising `on_raw` degrades invisibly, so wrap the body yourself and log
what you swallow. A line that was nothing but IRCv3 tags arrives as an empty
string, so handle that. If real work is needed, schedule a task rather than
doing it inline.

## 4. Handlers and the `self.bot` surface

Every handler has one signature:

```python
async def cmd_x(self, nick: str, reply_to: str, arg: str | None) -> None
```

| Parameter | Value |
|---|---|
| `nick` | the sender's current nick |
| `reply_to` | the channel name, or the sender's nick when the command arrived by PM |
| `arg` | everything after the command word, or `None` when nothing followed it |

`arg` is `None`, never the empty string, when the user types the bare command.
`arg.split()` on `None` raises `AttributeError`, so test `if not arg:` first.
The dispatcher has already rejected arguments longer than
`IRCBot._MAX_ARG_LEN = 400`, so `arg` is at most 400 characters and a module
advertising a larger input cap is advertising something unreachable.

Handlers return nothing. They answer through `self.bot`, and they run under
`asyncio.wait_for(..., timeout=60)` in `IRCBot._run_cmd()`; exceeding that
cancels the coroutine and notices the user that the command timed out.

### The bot surface

`self.bot` is the `IRCBot` instance. This is everything modules use, verified
against `internets.py`:

| Member | Behavior |
|---|---|
| `cfg` | the live `ConfigParser`. Read at use time so `.rehash` takes effect |
| `privmsg(target, msg)` | validate target, split into 400-byte UTF-8-safe chunks, enqueue PRIVMSG |
| `notice(target, msg)` | same, as NOTICE. Use for errors and rate-limit replies |
| `reply(nick, reply_to, msg, privileged=False)` | PM goes to the nick; a channel gets PRIVMSG, or a private NOTICE when `privileged` |
| `preply(nick, reply_to, msg)` | `reply(..., privileged=True)`. Output that should not be broadcast |
| `send(raw, priority=1)` | raw line onto the token-bucket sender. Priority 0 bypasses the rate limit and is reserved for PONG and QUIT |
| `rate_limited(nick)` | per-nick API cooldown, default 10 s. Consumes the token when it returns `False`. No admin bypass |
| `flood_limited(nick)` | per-nick flood gate, default 3 s, admins bypass. The dispatcher already applies it to every command |
| `channel_limited(channel)` | per-channel burst gate, also already applied by the dispatcher |
| `is_admin(nick)` | authed and current hostmask matches the one bound at auth. Fail-closed |
| `is_chanop(channel, nick)` | channel-operator check, lock-guarded for `to_thread` callers |
| `is_shadow_banned(nick)` | shadow-ban membership |
| `loc_get/loc_set/loc_del(nick)` | the per-nick saved weather location. This is PII |
| `channel_users(channel)` | membership snapshot from the store |
| `active_channels` | `ChannelSet`; supports `len()`, `in`, iteration, and `.snapshot()` |

Anything with a leading underscore is private. Three modules reach into private
attributes anyway (`privacy.py` uses `bot._store`, `health.py` uses
`bot._modules`, and `seen.py` / `remind.py` use
`getattr(self.bot, "_loop", None)` to schedule their flush task). Only the last
of those is a pattern worth copying, and only because there is no public
accessor for the loop; guard it with `getattr` exactly as they do, because the
loop is `None` before the bot starts and in most tests.

### Async rules

The bot is one event loop. Every blocking call stalls every user, not just the
caller.

- `requests.get()`, any disk read or write, `hashlib.scrypt`, `json.loads` on a
  large body, and any CPU-heavy computation must run under
  `await asyncio.to_thread(...)`.
- Nothing enforces this. There is no automatic offload and no watchdog; the
  60 s command timeout cancels the coroutine but not the thread or the blocking
  call underneath it.
- The failure is not theoretical. `mathx.cmd_isprime` runs its primality test
  synchronously on the loop while its sibling `cmd_bignum` offloads correctly,
  and a pasted 100-digit semiprime falls into an unbounded Pollard rho and
  hangs the entire bot for every user. See the findings ledger.

## 5. Outbound HTTP: the mandatory patterns

### Rule: no unbounded body, ever

A bare `requests.get(url).json()` or a read of `r.text` buffers the whole
response before you can look at it. A compromised or misconfigured upstream
returning a multi-gigabyte body takes the process down, and the bot has one
process. Every outbound HTTP call in a module therefore goes through
`base.fetch_json()`, or implements the same stream-and-cap inline.

A bare `r.json()` or an unbounded `r.text` in a module is a defect by project
policy, not a style preference.

### `fetch_json`

```python
from .base import fetch_json, ResponseTooLarge

fetch_json(url, *, ua, params=None, headers=None,
           timeout=10, max_bytes=262144, allow_404=False) -> Any
```

It issues `requests.get(..., stream=True)` inside a `with` block (so the socket
is released on every exit path), reads `max_bytes + 1` raw bytes with
`decode_content=True`, and raises `ResponseTooLarge` **before** decoding or
parsing if the cap was exceeded. Because `decode_content=True` counts
decompressed bytes, a small gzip bomb cannot expand past the cap in memory.

`ua` is keyword-only and mandatory, so no module can silently ship without a
contact User-Agent.

`allow_404=True` returns `None` on a 404 instead of raising, for
lookup-or-miss semantics (an unknown dictionary word, an unknown Pokemon).
Every other error status still raises.

The error contract is exactly three exceptions; catch these:

| Exception | Cause |
|---|---|
| `requests.RequestException` | transport error, or any non-404 HTTP status (`HTTPError` is a subclass) |
| `ResponseTooLarge` | body exceeded `max_bytes` |
| `json.JSONDecodeError` | body was not valid JSON, including an empty body |

The 256 KiB default fits nearly every API the bot talks to. Raise it
deliberately and comment the measurement, the way `poke.py` does for PokeAPI
(about 425 KB for the largest entry, capped at 1 MiB).

### The canonical fetch shape

A module-level **sync** function does the whole fetch, parse, and format, and
always returns a finished string. It never raises. The handler awaits it
through `asyncio.to_thread` and emits the result.

```python
import asyncio
import requests
from .base import fetch_json, ResponseTooLarge, strip_ctrl

_API = "https://api.example.com/v1/thing"


def _fetch_sync(query: str, ua: str) -> str:
    """Fetch, parse, format. Always returns a string, never raises."""
    try:
        data = fetch_json(_API, params={"q": query}, ua=ua, allow_404=True)
        if not isinstance(data, dict):
            return "mymod: not found"
        return strip_ctrl(data.get("name", ""), 300)
    except ResponseTooLarge:
        log.warning("mymod: upstream response too large")
        return "mymod: upstream response too large"
    except requests.RequestException as e:
        log.warning("mymod: request failed: %s", type(e).__name__)
        return "mymod: lookup failed"
    except Exception as e:                     # parse / unexpected shape
        log.warning("mymod: parse failed: %r", e)
        return "mymod: lookup failed"
```

Why the sync function must catch everything: if it raises, `to_thread`
propagates into the coroutine, `IRCBot._run_cmd()` catches it, increments
`unexpected_errors`, and sends the user a generic internal-error notice. The
user learns nothing and you lose the chance to say which of "not found",
"rate limited", and "upstream down" happened.

:::{warning}
Never interpolate an exception's `str()` into an IRC reply. urllib3 embeds the
full request URL in its transport errors, query string included. `stocks.py`
appends `str(exception)` to its all-providers-failed reply, so a network outage
while finance keys are configured publishes every one of those keys to the
channel. `sender.redact_secrets` is log-only and does not scrub PRIVMSG. Log
and emit the exception **class** name, never its text.
:::

### The inline stream-and-cap variant

Use it only when you need something `fetch_json` does not offer, such as a
non-JSON body or a status code you must branch on before parsing. Reproduce the
whole shape, including the `with` block and the `+ 1` read:

```python
_MAX_BODY_BYTES = 1024 * 1024   # measured: largest live response ~425 KB

with requests.get(url, headers={"User-Agent": ua},
                  timeout=10, stream=True) as r:
    if r.status_code == 404:
        return "not found"
    r.raise_for_status()
    body = r.raw.read(_MAX_BODY_BYTES + 1, decode_content=True)
if len(body) > _MAX_BODY_BYTES:
    return "upstream response too large"
data = json.loads(body.decode("utf-8", errors="replace"))
```

### SSRF: when the host comes from user input

`fetch_json` size-caps; it does not validate the destination. It will happily
fetch `http://169.254.169.254/`. It is safe only for a fixed, developer-chosen
host.

The moment any part of the URL or host originates with a user or a fetched
document, use the guard:

| Situation | Use |
|---|---|
| you hand a URL to an HTTP library and let it follow redirects | `_netsafe.safe_open()` |
| you resolve a host yourself and connect to the returned IP | `base.resolve_public()` |

```python
from ._netsafe import SSRFBlocked, safe_open

try:
    with safe_open("HEAD", url, ua, follow_redirects=True, timeout=10) as resp:
        final = strip_ctrl(resp.url)
except SSRFBlocked:
    return "blocked"
except requests.RequestException:
    return "lookup failed"
```

`safe_open` resolves the host, rejects the request if **any** answer is
private, loopback, link-local, site-local, unique-local, IPv4-mapped, or a
known metadata hostname, then pins DNS for the calling thread so urllib3 cannot
re-resolve to a different address between the check and the connect. It
re-validates and re-pins on every redirect hop. Read the body inside the
`with`; the session closes on exit.

`resolve_public()` is the lighter check: it returns the `getaddrinfo` list and
raises `ValueError` unless every answer is public. It is resolve-time only, so
the caller must connect to an address from the returned list rather than
re-resolving the name. `probe.py` does exactly that for `.ssl` and `.tcp`.

Working references: `urls.py` (`.expand`), `probe.py` (`.headers`, `.down`),
`scinews.py` (article reader), `ipintel.py`. Line-level detail is in
[internals/modules/_netsafe.md](internals/modules/_netsafe.md).

## 6. Credentials and configuration

### Reading a credential

```python
def on_load(self) -> None:
    from .base import cred
    self._ua = cred(self.bot.cfg, "weather_user_agent",
                    "weather", "user_agent", "Internets/1.0")
    self._key = cred(self.bot.cfg, "mymod_key", "mymod", "api_key")

def is_configured(self) -> bool:
    return bool(getattr(self, "_key", ""))
```

`cred(cfg, secret_name, section, key, default="")` resolves in order:
`secret_store.get(secret_name)` (which itself checks
`INTERNETS_<NAME_UPPER>` in the environment, then the 0600
`config.ini [secrets]` section), then `cfg.get(section, key)` as a legacy
fallback for pre-2.4.0 installs, then `default`.

Two behaviors matter. Template placeholders in the config fallback
(`changeme`, `your-key`, `placeholder`, `set-in-secret-store`, `<your-`,
`you@example`, `example.com`) are treated as unset, so a half-filled template
never leaks into an outbound request. And `cred` does not raise on a fresh
install, so a module degrades to unconfigured rather than failing its load.
The one gap: only `ImportError` is caught around `secret_store.get()`, so a
corrupt store propagates out of `cred` and fails your `on_load`.

Read credentials once, in `on_load()`, not per command.

**Do not invent a per-module User-Agent section.** Network modules share one
outbound contact identifier, read as
`cred(cfg, "weather_user_agent", "weather", "user_agent", ...)`, which 49 of
the 75 files under `modules/` do. The name is historical drift, not a weather
scope.

### `is_configured()` hides, it never disables

Returning `False` removes the module's commands from `.help` for non-admins.
It does **not** stop dispatch: `IRCBot._dispatch()` never consults it, so an
unconfigured command still resolves, still spawns a task, and still runs your
handler. If your module must refuse to act without a key, refuse inside the
handler:

```python
async def cmd_mymod(self, nick, reply_to, arg):
    if not self._key:
        self.bot.privmsg(reply_to, "mymod: not configured - see config.ini")
        return
```

`imdb`, `lastfm`, `satpass`, `steam`, `stocks`, `twitch`, and `youtube` are the
seven modules that override `is_configured()`; each also carries its own
in-handler check. `search.py` is the counterexample to avoid: `.si` needs
`brave_key` but the module never overrides `is_configured()`, so the command
stays advertised on a keyless install and fails at invocation.

### Registering a new secret

1. Add the canonical name to `KNOWN_SECRETS` in `secret_store.py`. Without
   this, the key still works through `get()` and the environment override, but
   it is invisible to `secret_store list` and `status()` and `migrate` will not
   relocate it. `nasa_api_key` is the live example of that omission.
2. Add a `CONFIG_LOCATIONS` entry mapping the name to its
   `(section, key)` only if you are migrating an existing plaintext ini key.
3. Add the section and key to `config.ini.example` with a blank value and a
   comment naming the signup URL. Several modules' ini fallbacks are currently
   unreachable on a fresh install because their section is missing from the
   template; do not add another.
4. Store it, letting the tool prompt:

```console
$ python -m secret_store set mymod_key
Value for mymod_key:
```

Pass `--value` only in a script. On an interactive shell it puts the secret in
your shell history and in the process table.

### Reading non-secret config

```python
url = self.bot.cfg.get("mymod", "api_url",
                       fallback="https://default.example.com")
```

Read `cfg` at use time, or re-read it in `on_load()` and accept that a
`.rehash` does not reach you until the next reload. Add every key you read to
`config.ini.example`. See [Configuration](configuration.md).

## 7. Persistent state

Only add state if the module genuinely needs it across restarts. Five modules
do: `seen`, `tell`, `notes`, `remind`, and `steam`.

### The write pattern

Every module store uses the same sequence: `tempfile.mkstemp` in the target
directory, write, `chmod 0o600`, `os.replace`. `mkstemp` creates at 0600 with
no collision risk, the explicit `chmod` covers the case where you write through
a path that did not inherit it, and `os.replace` is atomic on POSIX so a reader
never sees a torn file.

```python
import json, os, tempfile, threading
from pathlib import Path

class MyModule(BotModule):
    def on_load(self) -> None:
        self._file = Path(self.bot.cfg.get("mymod", "file",
                                           fallback="mymod.json"))
        self._lock = threading.Lock()
        self._data: dict[str, object] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if self._file.exists():
                with open(self._file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._data = data
        except Exception as e:
            log.warning("mymod: load failed: %r", e)
            self._data = {}

    def _save_sync(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._data)
            self._dirty = False
        fd, tmp = tempfile.mkstemp(dir=str(self._file.parent),
                                   prefix=self._file.name + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._file)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log.warning("mymod: save failed: %r", e)
            with self._lock:
                self._dirty = True
```

Take the snapshot under the lock and write outside it. The write runs in a
`to_thread` worker while handlers keep mutating `self._data` on the loop;
iterating the live dict from the worker is a "dictionary changed size during
iteration" waiting to happen. `notes.py` and `steam.py` both have that defect
today, as does the core shadow-ban save.

### Flushing

```python
def on_load(self) -> None:
    ...
    self._flush_task = None
    loop = getattr(self.bot, "_loop", None)
    if loop is not None:
        self._flush_task = loop.create_task(self._periodic_flush())

async def _periodic_flush(self) -> None:
    try:
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(self._save_sync)
    except asyncio.CancelledError:
        return

def on_unload(self) -> None:
    t = getattr(self, "_flush_task", None)
    if t is not None and not t.done():
        t.cancel()
    self._save_sync()          # final flush
```

Cancelling in `on_unload()` is not optional. The loader does not cancel your
tasks; an uncancelled flush task survives a `.reload`, keeps running against
the dead instance, and the reloaded instance starts a second one.
`graceful_shutdown()` unloads every module before flushing the core store, so
`on_unload()` is your last chance to persist.

:::{warning}
**Module stores have no integrity envelope.** `store.py` wraps its three core
datasets as `{"schema": 2, "checksum": "<sha256>", "data": ...}`, validates the
checksum on read, and quarantines a bad file to `<name>.corrupt.<ts>` instead
of using it. Module stores do none of that: a corrupt `seen.json` or
`notes.json` is a parse failure at load, the module logs a warning and starts
empty, and the next flush overwrites the damaged file. There is no recovery
path. If your data is worth more than that, say so in review rather than
assuming the envelope applies.

No writer in this repository calls `fsync`, so the atomic replace guarantees a
reader never sees a torn file but does not guarantee the newest version
survives a power loss.
:::

Full inventory and the pattern comparison:
[State and Persistence](state-and-persistence.md#two-persistence-patterns).

## 8. Rate limiting and gate order

Two limiters exist. Know which one you are calling.

| Limiter | Default | Applied by | Admin bypass |
|---|---|---|---|
| flood (`flood_limited`) | 3 s per nick | the dispatcher, on every command | yes |
| API cooldown (`rate_limited`) | 10 s per nick | your handler, explicitly | **no** |

The dispatcher's flood gate already bounds every command word at one per 3 s
per nick, plus a per-channel burst gate. `rate_limited()` is the additional,
opt-in gate for commands that do real work: a network call, a heavy
computation, anything that costs an upstream quota. Call it, and note that it
consumes the token when it returns `False`, so calling it twice in one handler
charges the user twice.

### Gate order

The house convention, and the order to write:

```python
async def cmd_mymod(self, nick, reply_to, arg):
    if not self.bot.is_admin(nick):            # 1. authorization, if any
        self.bot.notice(nick, f"{nick}: admins only")
        return
    if not arg:                                 # 2. usage / empty argument
        p = self.bot.cfg["bot"]["command_prefix"]
        self.bot.privmsg(reply_to, f"{nick}: usage: {p}mymod <text>")
        return
    if self.bot.rate_limited(nick):             # 3. API cooldown
        self.bot.notice(nick, f"{nick}: slow down - try again shortly")
        return
    result = await asyncio.to_thread(_fetch_sync, arg, self._ua)  # 4. work
    self.bot.privmsg(reply_to, f"{nick}: {result}")
```

Authorization first, so an unauthorized user learns nothing about argument
syntax and burns no quota. Usage before the cooldown, so a user who mistypes
the command gets told how to use it instead of being throttled for asking. The
cooldown immediately before the work, never after it.

### The inconsistency in the tree

Both orders ship today, and the difference is real.

| Order | Modules | Effect of a bare, argument-less invocation |
|---|---|---|
| usage before cooldown | `dictionary`, `urbandictionary`, `translate`, `search`, `reddit`, and `example.py` | emits a usage line without consuming an API token; repeat invocations are bounded only by the 3 s dispatcher flood gate |
| cooldown before usage | `crypto`, `fx` | consumes an API token to be told the syntax; a user who mistypes waits out the full cooldown |
| no cooldown at all | `bofh`, `dice`, and every command in `privacy` | only the dispatcher's flood gate applies |

The usage-first order is the convention and is what `example.py` teaches, but
be aware of what it costs: a stream of bare `.dict` invocations produces one
usage reply per flood window, at three times the rate the API cooldown would
allow, and none of them are charged against it. If your usage reply is
expensive, or your command has no argument at all (`.hn`, `.xkcd`, `.iss` all
gate first, correctly, because there is no usage branch to reach), gate first.

The right fix for a command with an optional argument is to gate on the
cooldown before doing work and to keep the usage branch free, which is what the
order above does. Do not add a third pattern.

## 9. Output sanitization and IRC line length

### `strip_ctrl` on everything third-party

```python
from .base import strip_ctrl

title = strip_ctrl(data.get("title", ""), 300)
self.bot.privmsg(reply_to, f"{nick}: {title}")
```

`strip_ctrl(s, max_len=400)` removes the full C0 range `\x00-\x1f` plus `\x7f`
and truncates. It is the only defense against a hostile upstream injecting
bot-attributed bold (`\x02`), color (`\x03`), CTCP (`\x01`), terminal escapes
(`\x1b`), or BEL spam into a line the channel will attribute to your bot. The
sender strips only `\r\n\x00` as a transport backstop; it is not the sanitizer.

Route every API field, every header value, every user echo, and every feed
title through it. It coerces non-string input, so `strip_ctrl(None)` is `""`
and `strip_ctrl(42)` is `"42"`.

`tests/run_tests.py` carries a completeness gate that greps a fixed list of
security-relevant modules for a `strip_ctrl` reference. If your module emits
upstream or user-derived text, add its name to that tuple. Note what the gate
proves: a call site exists somewhere in the file, not that the string you
emitted passed through it.

### Deliberate formatting

If your module intentionally emits bold, sanitize each untrusted field first
and assemble afterwards, so the control bytes you add are yours and the ones
upstream sent are gone:

```python
name = strip_ctrl(data["name"], 200)
desc = strip_ctrl(data["desc"], 300)
line = f"\x02{name}\x02: {desc}"
```

Do not call `strip_ctrl` on the assembled line: it removes your `\x02` too.
`bored.py` does exactly that and silently loses its intended bold.

### The 400-character reality

Three separate 400s apply, and they are not the same 400:

| Limit | Where | Effect |
|---|---|---|
| `strip_ctrl(..., max_len=400)` default | `modules/base.py` | your string is truncated at 400 characters |
| `IRCBot._MAX_BODY = 400` | `internets.py - _split_msg()` | the message body is split into 400-byte, UTF-8-boundary-safe chunks, one IRC line each |
| `IRCBot._MAX_ARG_LEN = 400` | `internets.py - _dispatch()` | inbound `arg` longer than 400 characters is refused before your handler runs |

Chunking means a long reply becomes several lines rather than being truncated,
and each line goes through the token-bucket sender. Multi-line output is fine
when you mean it (a header then one line per item), but every line costs a
token, so cap the item count yourself.

The trap is passing a value longer than 400 through the default `strip_ctrl`:

:::{admonition} Known defect: `.qr`
:class: warning
`qr.py` advertises and accepts up to 1000 characters, builds an image URL from
the encoded input, and then emits it through a `strip_ctrl` call left at the
400-character default. Long inputs produce a silently truncated, broken link.
The advertised cap is unreachable anyway, since the dispatcher already refuses
an `arg` over 400 characters. When the string you are emitting is a URL or any
other atom that must survive intact, pass an explicit `max_len`.
:::

## 10. Privacy erasure

If your module keys anything by nick, override `forget()`:

```python
def forget(self, nick: str) -> int:
    with self._lock:
        removed = self._data.pop(nick.lower(), None)
    if removed is None:
        return 0
    self._save_sync()
    return 1
```

`.forgetme` calls `forget()` on every loaded module, so the erasure surface is
exactly the set of modules that override it: `seen`, `tell`, `notes`,
`remind`, `steam`. Return the number of records removed; the count is reported
back to the user. If your module stores nothing personal, do not override it,
because the default returning `0` is what makes the report accurate.

Erasure means the in-memory copy and the on-disk file, immediately, not at the
next periodic flush. And do not log per-nick PII in the first place: the bot
log is outside `.forgetme`'s reach, which is why `location.cmd_regloc` writing
nick-to-location pairs and `linktitle` writing announced URLs at INFO are both
recorded as privacy findings.

## 11. Testing

Three suite-wide gates pick up your file whether or not you write a test.

1. **`.help` gate** (`tests/test_help.py`) parametrizes over every file in
   `modules/`. Every primary command must appear in `help_lines()`, every line
   must stay under the byte bound and start with the two-space indent that
   `help_row()` produces, and aliases must be written `mymod/.mm`, never
   spaced. It instantiates your class without running `__init__`, so
   `help_lines()` must not depend on `on_load()` having run.
2. **Sanitizer completeness gate** (`tests/run_tests.py`) greps a fixed module
   list for `strip_ctrl`. Add your name if you emit third-party text.
3. **`COMMANDS` contract** fails collection outright on a typo or a
   non-`async def` handler.

Then write `tests/test_mymod.py`:

```python
from unittest.mock import patch
import asyncio
from modules.mymod import MyModule, _fetch_sync


class FakeBot:
    def __init__(self):
        self.replies = []
        self.cfg = {"bot": {"command_prefix": "."}}
    def privmsg(self, target, msg):
        self.replies.append(("privmsg", target, msg))
    def notice(self, target, msg):
        self.replies.append(("notice", target, msg))
    def rate_limited(self, nick):
        return False


def test_usage_on_empty_arg():
    bot = FakeBot()
    mod = MyModule(bot)
    asyncio.run(mod.cmd_mymod("user", "#test", None))
    assert any("usage" in r[2] for r in bot.replies)


def test_fetch_reports_miss():
    with patch("modules.mymod.fetch_json", return_value=None):
        assert "not found" in _fetch_sync("q", "ua")
```

Conventions the suite enforces or expects:

- Never write `async def test_`. Use `asyncio.run(...)`. `pyproject.toml` sets
  `asyncio_mode = "auto"`, which contradicts the suite's manual-loop
  convention; an `async def` test may no-op locally and run in CI.
- Assert behavior, not the current implementation.
- Exercise the error branches: non-200, oversize, malformed payload, missing
  key. That is where most module defects live.
- Reconcile the counts after adding a file. The totals must move by exactly
  what you added; a file at the wrong path leaves the suite green and your
  invariant unguarded.

```console
$ pytest tests/                   # main suite, 40 files
$ python tests/run_tests.py       # standalone regression suite
$ pytest tests/test_mymod.py -v
```

Both runners must pass; they are disjoint, and a green pytest says nothing
about `run_tests.py`. Depth on fixtures, doubles, and the coverage gate is in
[Testing](testing.md#writing-a-test-for-a-new-module).

## 12. Registering and deploying

During development, from an authed admin session or the console:

```
.load mymod
```

Edit, then `.reload mymod`. Two things to know before you rely on that loop:

- A failed reload leaves the module **unloaded**. `reload_module()` unloads
  first, and if the load then fails (a syntax error in the file you just
  edited, a `COMMANDS` typo) nothing restores it. Confirm with `.modules`.
- Editing a helper (`base.py`, `geocode.py`, `units.py`, `_netsafe.py`, or
  anything under `weather_providers/`) and reloading a command module picks up
  nothing. Helpers stay cached in `sys.modules`. Use `.restart`.

For production, add the name to `config.ini`:

```ini
[bot]
autoload = weather, seen, tell, ..., mymod
```

Modules load in list order at startup. Finally, regenerate the command
inventory and check the reference:

```console
$ python scripts/gen-command-reference.py
$ python scripts/gen-command-reference.py --check docs/command-reference.md
```

## 13. Common pitfalls

**Blocking the loop.** Covered in section 4, and it is the single most damaging
mistake available to a module author.

**Catching broadly in the handler.** The handler should not carry a bare
`except Exception`. `IRCBot._run_cmd()` already catches, logs with a traceback,
counts the error, and notices the user. Catch specifically in the sync fetch
function and return a descriptive string. A handler that swallows its own
exception produces no traceback and no metric.

**Treating `arg` as a string.** It is `None` for a bare command.

**Assuming `is_configured()` protects anything.** It hides from `.help` and
nothing else.

**Forgetting `on_unload()`.** Every task you create, you cancel.

**A per-module User-Agent section.** Use the shared credential.

**Emitting exception text.** Class names only.

**Importing heavy optional dependencies at module top level.** They load on
`.load` even if the command is never used. `requests` at the top is the
convention because it is a core dependency and always present; anything
optional gets a function-local import. `fetch_json` imports `requests` lazily
inside itself for the same reason.

**Claiming a command word that exists.** Check with the generator script
before you write the module, not after.

## 14. A complete worked module

Every element below is checked against the live contract: the class validates
under `__init_subclass__`, the handler signature matches what `_run_cmd()`
calls, the fetch is capped, output is sanitized, the gates are in the
recommended order, and `help_lines()` satisfies the `.help` regression gate.

```python
"""ISBN lookup - .book <isbn>, via the Open Library API."""

from __future__ import annotations

import asyncio
import logging

import requests

from .base import (BotModule, ResponseTooLarge, cred, fetch_json,
                   help_row, strip_ctrl)

log = logging.getLogger("internets.book")

_API = "https://openlibrary.org/api/books"
_MAX_FIELD = 160


def _fetch_sync(isbn: str, ua: str) -> str:
    """Fetch, parse, format. Always returns a string, never raises."""
    key = f"ISBN:{isbn}"
    try:
        data = fetch_json(_API, ua=ua,
                          params={"bibkeys": key, "format": "json",
                                  "jscmd": "data"})
    except ResponseTooLarge:
        log.warning("book: upstream response too large")
        return "book: upstream response too large"
    except requests.RequestException as e:
        log.warning("book: request failed: %s", type(e).__name__)
        return "book: lookup failed"
    except Exception as e:
        log.warning("book: parse failed: %r", e)
        return "book: lookup failed"

    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return f"book: no record for {strip_ctrl(isbn, 20)}"

    title = strip_ctrl(entry.get("title", "?"), _MAX_FIELD)
    authors = ", ".join(
        strip_ctrl(a.get("name", ""), 40)
        for a in entry.get("authors", [])[:3]
        if isinstance(a, dict)
    ) or "unknown"
    year = strip_ctrl(entry.get("publish_date", "?"), 20)
    pages = entry.get("number_of_pages")
    tail = f", {pages}pp" if isinstance(pages, int) else ""
    return f"{title} - {authors} ({year}{tail})"


class BookModule(BotModule):
    """`.book <isbn>` - look up a book by ISBN-10 or ISBN-13."""

    COMMANDS: dict[str, str] = {
        "book": "cmd_book",
        "isbn13": "cmd_book",
    }

    def on_load(self) -> None:
        self._ua = cred(self.bot.cfg, "weather_user_agent",
                        "weather", "user_agent", "Internets/1.0")

    async def cmd_book(self, nick: str, reply_to: str,
                       arg: str | None) -> None:
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}book <isbn>")
            return
        isbn = "".join(c for c in arg if c.isalnum())
        if len(isbn) not in (10, 13):
            self.bot.privmsg(reply_to, f"{nick}: book: need an ISBN-10 or -13")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again shortly")
            return
        result = await asyncio.to_thread(_fetch_sync, isbn, self._ua)
        self.bot.privmsg(reply_to, f"{nick}: {result}")

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "book/.isbn13 <isbn>",
                         "Look up a book by ISBN")]


def setup(bot: object) -> BookModule:
    return BookModule(bot)   # type: ignore[arg-type]
```

Points worth noticing in it:

- The alias is a second `COMMANDS` key pointing at the same method, and
  `help_lines()` writes it `book/.isbn13` with no spaces, which the `.help`
  alias-separator gate requires.
- Input is normalized and validated (`isalnum`, length 10 or 13) **before** the
  cooldown, so a malformed ISBN does not cost a token, and before the value
  reaches a URL. The dispatcher already bounded `arg` at 400 characters.
- The fetch is `fetch_json` at the default 256 KiB cap with the mandatory `ua`,
  and it catches exactly the three-exception contract plus a fallback.
- Every field taken from the response passes through `strip_ctrl` with an
  explicit length, and the author list is capped at three entries so the reply
  stays on one IRC line.
- No `forget()` override, because nothing is keyed by nick.
- `is_configured()` is not overridden, because Open Library needs no key.

## See also

- [Module System](modules.md) - loading, reloading, and the catalog.
- [Command Reference](command-reference.md) - the user-facing surface.
- [Testing](testing.md) - suites, fixtures, and gates.
- [Configuration](configuration.md) - config keys and the secret model.
- [Security Model](security-model.md) - SSRF layer, size caps, and
  `strip_ctrl` in context.
- [internals/modules/base.md](internals/modules/base.md) - line-level reference
  for `BotModule` and the shared helpers.
- [internals/modules/_netsafe.md](internals/modules/_netsafe.md) - the SSRF
  guard in detail.
