# Writing a Module

Step-by-step guide to building, testing, and shipping a new command module.
This is the practical companion to the contract reference in `docs/modules.md`
Part 1. Read that first if you haven't; this guide assumes you know what
`BotModule`, `COMMANDS`, and `setup()` are.

Start from `modules/example.py` - copy it, rename, and fill. This guide
explains the decisions behind each piece.

---

## 1. Create the file

```bash
cp modules/example.py modules/mymod.py
```

Module names must match `^[a-z][a-z0-9_]*$`. A file named `MyMod.py` or
`my-mod.py` is rejected by the loader (`internets.py:450`).

---

## 2. Define the class and commands

```python
from __future__ import annotations
import asyncio
import logging
from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.mymod")

class MyModule(BotModule):
    COMMANDS: dict[str, str] = {
        "mymod": "cmd_mymod",
        "mm": "cmd_mymod",          # alias
    }

    async def cmd_mymod(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}mymod <text>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down")
            return
        self.bot.privmsg(reply_to, f"{nick}: you said {strip_ctrl(arg, 200)}")

    def help_lines(self, prefix: str) -> list[str]:
        return [help_row(prefix, "mymod/.mm <text>", "Echo text back")]

def setup(bot):
    return MyModule(bot)
```

### What the handler receives

| Parameter | Value |
|---|---|
| `nick` | The sender's current IRC nick |
| `reply_to` | The channel name (if invoked in a channel) or the sender's nick (if PM) |
| `arg` | Everything after the command word, or **`None`** if nothing followed the command |

`arg` is `None`, not empty string, when the user types just `.mymod` with
nothing after it. Check `if not arg:` (catches both) or `if arg is None:`
when you need to distinguish empty from absent.

### Reply methods

| Method | When to use |
|---|---|
| `self.bot.privmsg(reply_to, msg)` | Normal output (to channel or PM) |
| `self.bot.notice(nick, msg)` | Error/rate-limit messages (NOTICE to user, not the channel) |
| `self.bot.reply(nick, reply_to, msg)` | Routing split: channels get PRIVMSG, PMs get NOTICE |
| `self.bot.preply(nick, reply_to, msg)` | Same as reply but with `privileged=True` (always NOTICE) |

### Contract enforcement

`BotModule.__init_subclass__` (`base.py:220`) validates at class-definition
time that every value in `COMMANDS` names a real `async def` method. A typo
(`"cmd_mymodd"`) or a sync handler (`def cmd_mymod` without `async`) raises
`TypeError` when the file is loaded, not when a user first invokes the
command. This is the safety net; lean on it.

---

## 3. Add network calls (the canonical fetch pattern)

Every module that hits an HTTP API follows the same shape: a **module-level
sync function** does the entire fetch + parse + format and always returns a
finished string, catching every error. The handler awaits it via
`asyncio.to_thread`. This keeps blocking I/O off the event loop.

```python
from .base import fetch_json, ResponseTooLarge

_API = "https://api.example.com/v1/thing"

def _fetch_sync(query: str, ua: str) -> str:
    """Fetch, parse, format. Always returns a string, never raises."""
    try:
        data = fetch_json(_API, params={"q": query}, ua=ua)
        if data is None:
            return "mymod: not found"
        return strip_ctrl(str(data.get("name", "")), 300)
    except ResponseTooLarge:
        log.warning("mymod: response too large")
        return "mymod: response too large"
    except Exception as e:
        log.warning(f"mymod: {e!r}")
        return "mymod: lookup failed"
```

Then in the handler:

```python
async def cmd_mymod(self, nick, reply_to, arg):
    if not arg:
        # ... usage
        return
    if self.bot.rate_limited(nick):
        # ... slow down
        return
    result = await asyncio.to_thread(_fetch_sync, arg, self._ua)
    self.bot.privmsg(reply_to, f"{nick}: {result}")
```

### Rules

- **Use `fetch_json`, not bare `requests.get(...).json()`.** `fetch_json`
  streams the body and caps it at 256 KB before decode. A compromised upstream
  returning a multi-GB JSON body cannot OOM the process. Pass `max_bytes=`
  for APIs with legitimately larger payloads.
- **`allow_404=True`** returns `None` on 404 for lookup-or-miss semantics
  (dictionary word, Pokemon name). Without it, 404 raises `HTTPError`.
- **The sync function must catch everything.** If it raises, `to_thread`
  propagates the exception to the event loop, the command's `_run_cmd`
  wrapper catches it and sends a generic error notice to IRC, and the user
  gets no useful message. Catch inside the function and return a descriptive
  string.

### SSRF: when the URL comes from user input

`fetch_json` only size-caps; it does not validate the destination. If the
host or URL is derived from user or feed input (a `.fetch <url>` command),
do not pass it to `fetch_json`. Route it through `_netsafe.safe_open()`
(DNS-pinned, redirect-safe) or `base.resolve_public()` (resolve-only,
for modules that connect to the validated IP themselves). See `probe.py`,
`urls.py`, `scinews.py` for the patterns.

---

## 4. Add credentials and configuration

### Reading a secret (API key)

```python
def on_load(self) -> None:
    from .base import cred
    self._ua = cred(self.bot.cfg, "weather_user_agent",
                    "weather", "user_agent", "Internets/1.0")
    self._key = cred(self.bot.cfg, "mymod_key", "mymod", "api_key")

def is_configured(self) -> bool:
    return bool(getattr(self, "_key", ""))
```

`cred()` checks `secret_store.get()` first, then `config.ini` fallback.
Template placeholders (`changeme`, `your-key-here`, etc.) are treated as
unset. When `is_configured()` returns `False`, `.help` hides the module's
commands from normal users (admins still see them). The module still loads
and dispatches - it is hidden, not disabled.

### Registering a new secret

1. Add the name to `KNOWN_SECRETS` in `secret_store.py`.
2. Add a `CONFIG_LOCATIONS` entry if migrating from a legacy config section.
3. Add the key to `config.ini.example` under the appropriate section with a
   blank value and a comment naming the signup URL.
4. Set it: `python -m secret_store set mymod_key --value <key>`

### Reading non-secret config

```python
url = self.bot.cfg.get("mymod", "api_url", fallback="https://default.example.com")
```

Add the section and key to `config.ini.example` with a comment.

---

## 5. Add persistent state

If your module stores per-nick data that must survive restarts (like `.seen`,
`.tell`, `.notes`, `.remind`), follow the atomic-write pattern used by all
four of those modules:

```python
import json
import os
import tempfile
import threading
from pathlib import Path

class MyModule(BotModule):
    def on_load(self) -> None:
        self._file = Path(self.bot.cfg.get("mymod", "file", fallback="mymod.json"))
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
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
            log.warning(f"mymod: load failed: {e!r}")
            self._data = {}

    def _save_sync(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._data)
            self._dirty = False

        fd, tmp = tempfile.mkstemp(
            dir=str(self._file.parent),
            prefix=self._file.name + ".",
            suffix=".tmp",
        )
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
            log.warning(f"mymod: save failed: {e!r}")
            with self._lock:
                self._dirty = True
```

### Why this pattern

- `tempfile.mkstemp` creates a unique temp file with 0600 perms and no
  collision risk.
- `os.chmod` before `os.replace` ensures the final file is never momentarily
  world-readable.
- `os.replace` is atomic on POSIX - either the full new file is there or the
  old one is.
- On failure, the temp file is cleaned up and `_dirty` is re-set so the next
  flush retries.

### Periodic flush

Schedule a flush task on the bot's event loop (same pattern as `seen.py`):

```python
def on_load(self) -> None:
    # ... after _load() ...
    loop = getattr(self.bot, "_loop", None)
    if loop:
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
    if t and not t.done():
        t.cancel()
    self._save_sync()   # final flush
```

---

## 6. Implement privacy erasure

If your module stores per-nick data, override `forget()`:

```python
def forget(self, nick: str) -> int:
    with self._lock:
        removed = self._data.pop(nick.lower(), None)
    if removed is None:
        return 0
    self._save_sync()
    return 1
```

`.forgetme` calls `forget()` on every loaded module. Return the count of
records removed. If your module stores no per-nick data, the default
`BotModule.forget()` (returns 0) is correct - do not override it.

Privacy contract: if a user runs `.forgetme`, every trace of their nick
must be gone from your module's state and its on-disk file.

---

## 7. Sanitize output

Every string that comes from upstream (an API response, user input being
echoed, a feed title) must go through `strip_ctrl` before it reaches an
IRC line:

```python
from .base import strip_ctrl

title = strip_ctrl(data.get("title", ""), 300)
self.bot.privmsg(reply_to, f"{nick}: {title}")
```

`strip_ctrl` removes the full C0 control range (`\x00-\x1f`) plus DEL
(`\x7f`), preventing IRC formatting injection (bold, color, reverse, ESC,
BEL) and protocol injection (CR/LF). The sender's own `\r\n\x00` stripping
is a transport backstop, not the primary defense.

If your module intentionally uses bold (`\x02`) in assembled output, strip
each untrusted field individually, then strip only transport bytes from the
final line:

```python
name = strip_ctrl(data["name"], 200)
line = f"\x02{name}\x02: {strip_ctrl(data['desc'], 300)}"
# transport backstop only on the assembled line
import re
line = re.sub(r"[\r\n\x00]", "", line)
```

---

## 8. Write tests

### Where to put them

`tests/test_mymod.py`. The test suite is split:
- `pytest tests/` - the main suite (41 files, `asyncio_mode = "auto"`)
- `python tests/run_tests.py` - standalone regression tests

New modules go in the pytest suite.

### What to test

1. **Handler behavior** with a fake bot. Build a minimal stub:

```python
import asyncio
from modules.mymod import MyModule

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
```

2. **The sync fetch function** with a mocked HTTP response. Patch
   `requests.get` or `modules.base.fetch_json`:

```python
from unittest.mock import patch

def test_fetch_returns_name():
    with patch("modules.mymod.fetch_json", return_value={"name": "Thing"}):
        result = _fetch_sync("query", "ua")
    assert "Thing" in result

def test_fetch_handles_404():
    with patch("modules.mymod.fetch_json", return_value=None):
        result = _fetch_sync("query", "ua")
    assert "not found" in result
```

3. **Persistence** (if applicable) against real temp files:

```python
def test_save_and_load(tmp_path):
    # Set module's _file to a temp path, add data, save, reload, verify
```

4. **`strip_ctrl` usage** - verify upstream text passes through it. The
   standalone suite (`run_tests.py`) has a completeness gate that checks
   security-relevant modules for `strip_ctrl` references; add yours if it
   handles upstream text.

### Running tests

```bash
pytest tests/                    # main suite
python tests/run_tests.py        # standalone regression tests
pytest tests/test_mymod.py -v    # just your module
```

Both suites must pass before merge. They are disjoint - a green pytest does
not mean `run_tests.py` passed.

---

## 9. Register and deploy

### Development (hot-load)

```
.load mymod
```

from an authed admin session or the interactive console. The module is
loaded from disk immediately. Edit the file, then `.reload mymod` to pick
up changes. This works for the command module itself but NOT for changes to
helpers it imports (`base.py`, `geocode.py`, `_netsafe.py`,
`weather_providers/`). Those require `.restart`.

### Production (autoload)

Add the module name to `config.ini`:

```ini
[bot]
autoload = weather, seen, tell, ..., mymod
```

It loads on every startup, in list order.

---

## 10. Common pitfalls

### Blocking the event loop

`requests.get()`, `hashlib.scrypt()`, `json.loads()` on a large body, and
any disk I/O are blocking calls. They must run under
`await asyncio.to_thread(...)`. The single event loop serves every user;
a 2s HTTP call blocks every other command for 2s.

### Forgetting rate limiting

Every command that does real work (network, CPU) should gate on
`self.bot.rate_limited(nick)`. Without it, a user can flood the bot with
requests. Place the rate-limit check after the usage/empty-arg check
(so `.mymod` with no argument doesn't consume a rate-limit token) and
before the actual work.

### Catching too broadly in the handler

The handler coroutine should not have a bare `except Exception`. The
`_run_cmd` wrapper in the core already catches unhandled exceptions and
sends a generic error notice. Catch specifically in the sync fetch function
and return a descriptive string. An unhandled exception in the handler is
logged with a traceback; a silently-caught one is invisible.

### Assuming `arg` is a string

`arg` is `None` when the user types just the command word. `arg.split()`
on `None` raises `AttributeError`. Always check first.

### Importing at module top level

Importing `requests` or other heavy deps at the top of the module file
means they load on `.load mymod` even if the command is never invoked.
The convention is to import `requests` at the top (it's a core dep and
always available) but import optional deps lazily inside the function
that uses them. `fetch_json` imports `requests` lazily internally.

### Naming collisions

If your `COMMANDS` dict contains a word already owned by another loaded
module, the load is rejected with a conflict message. Check existing
modules first: `grep -r "COMMANDS" modules/*.py | grep '"myword"'`.

### The `on_raw` trap

`on_raw(line)` runs synchronously on the event-loop thread for every
incoming IRC line. It must be fast and must never raise. Wrap the entire
body in `try/except` and log at `debug` level on error. If you need to do
real work in response to a raw line, schedule an `asyncio.Task` from
`on_raw` rather than doing it inline.

`on_raw` never receives PING or PONG lines (they are handled and returned
before the module fanout). A line that is only IRCv3 tags (empty after
`strip_tags`) does reach `on_raw` - handle empty strings.

---

## Reference: the complete module lifecycle

1. Admin runs `.load mymod` (or it's in `autoload`).
2. Loader validates the name, checks path traversal, `exec_module`s the file.
3. Loader calls `mod.setup(bot)`, gets a `BotModule` instance back.
4. Loader checks for command-word collisions with other loaded modules.
5. `inst.on_load()` runs (read config, start tasks, load state).
6. Commands are registered in `self._commands`.
7. User invokes `.mymod foo` in a channel.
8. Dispatch: shadow-ban check -> flood gate -> channel gate -> arg-length
   gate -> task-count gate -> spawn `asyncio.Task`.
9. `_run_cmd` wraps the handler in `asyncio.wait_for(timeout=60)`.
10. Handler runs, replies via `self.bot.privmsg()`.
11. Admin runs `.unload mymod` or `.reload mymod`.
12. `inst.on_unload()` runs (cancel tasks, final flush).
13. Commands are removed from `self._commands`.
14. On `.reload`: the file is re-read from disk and steps 2-6 repeat.
