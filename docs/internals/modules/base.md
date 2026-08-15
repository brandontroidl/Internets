# base.py - the module developer contract (BotModule + shared helpers)

`modules/base.py` (286 lines) defines everything a command module is built from: the
`BotModule` base class every module subclasses, and five module-shared helpers -
`fetch_json` (size-capped HTTP), `resolve_public` (anti-SSRF resolver), `cred`
(secret/config credential lookup), `help_row` (`.help` formatting), and `strip_ctrl`
(IRC output sanitizer). It is the single file a module author must understand; the 70+
modules under `modules/` are variations on the contract stated here.

This doc also covers `modules/__init__.py` (7 lines), which has no behavior of its own
(see [Package init](#package-init)).

## Purpose

Three distinct jobs share the file:

1. **The module lifecycle contract** - `BotModule` names the hooks the loader and
   dispatcher in `internets.py` call (`COMMANDS`, `help_lines`, `is_configured`,
   `on_load`, `on_unload`, `on_raw`, `forget`) and validates the command-to-handler
   mapping at class-definition time.
2. **Shared security helpers** - `fetch_json` bounds every outbound JSON fetch so a
   hostile upstream cannot OOM the process; `resolve_public` refuses non-public
   addresses for the network probers; `strip_ctrl` removes IRC control bytes from
   third-party text before it reaches a bot-emitted line; `cred` keeps placeholder
   template values out of outbound requests.
3. **Shared presentation** - `help_row` keeps `.help` output column-aligned across all
   modules.

## Responsibilities and boundaries

Belongs here: the base class, and helpers that more than a handful of modules need and
that carry a security or consistency invariant.

Deliberately NOT here:

- **Dispatch, rate limiting, admin auth, message sending** - all live on the bot object
  (`internets.py - IRCBot`); modules reach them through `self.bot` (see
  [The bot surface](#the-bot-surface)).
- **SSRF defense for user-supplied URLs** - `fetch_json` does NOT validate its
  destination; it only caps size. URL-following with DNS pinning lives in
  [`_netsafe.py`](_netsafe.md). `resolve_public` here is the lighter resolve-time
  check used by the probe commands (comparison in
  [resolve_public vs _netsafe](#resolve_public-vs-_netsafe)).
- **Async HTTP for the weather subsystem** - `weather_providers/_http.py - get_json()`
  is a separate, parallel implementation (aiohttp-first, different error contract);
  see [Relationship to weather_providers/_http.py](#relationship-to-weather_providers_httppy).
- **Secret storage** - `secret_store.py` owns storage; `cred` only reads it.

## Dependencies and dependents

Imports: stdlib only at module import time (`json`, `configparser`, `typing`).
`requests`, `socket`, `ipaddress`, `inspect`, and `secret_store` are imported lazily
inside the functions that need them, so `base.py` imports cleanly in test environments
without the full dependency set. The `IRCBot` type is imported under `TYPE_CHECKING`
only - no runtime import cycle with `internets.py`.

Dependents (verified by grep at time of writing): all 70+ command modules subclass
`BotModule`; 72 of 75 files under `modules/` use `help_row` and/or `strip_ctrl`; 22
call `fetch_json` (24 files reference it, minus this file and `example.py`'s
commentary); `probe.py`, `scinews.py` use `resolve_public`; most keyed modules use
`cred`. `internets.py - IRCBot.load_module()` consumes the contract from the other
side.

## Lifecycle

`base.py` itself is imported once (as `modules.base`) when the first module loads. Per
module, the lifecycle is driven by `internets.py`:

1. `.load <name>` or autoload calls `IRCBot.load_module()` - validates the module name
   (`^[a-z][a-z0-9_]*$`), confines the path to `modules/`, and executes the file as
   `modules.<name>` via `importlib.util.spec_from_file_location`.
2. Executing the class body triggers `BotModule.__init_subclass__` - a bad `COMMANDS`
   entry raises `TypeError` here, so the load fails before registration.
3. The loader calls the module's `setup(bot)` entry point, which returns a `BotModule`
   instance; the loader rejects command-word collisions with other loaded modules, then
   calls `inst.on_load()` and registers `inst.COMMANDS` into the dispatch table.
4. Steady state: `IRCBot._dispatch()` resolves a command word to `(module, method)` and
   runs the handler; every incoming IRC line is fanned out to each module's `on_raw()`.
5. `.unload` / shutdown calls `on_unload()` and removes the module's commands.
   `graceful_shutdown()` unloads every module before flushing the store, so
   `on_unload()` is a module's last chance to persist state.

## State

`base.py` holds no mutable state. Module-level names are constants:
`_DEFAULT_MAX_JSON_BYTES` (256 KiB), `_PLACEHOLDER_MARKERS`, `_HELP_USAGE_W` (24),
`_IRC_CTRL_RE` (compiled once). A `BotModule` instance owns only `self.bot`; anything
else is subclass state. Persistent per-user state (locations, seen, tells) lives in
`store.py`, reached via the bot surface.

## Concurrency

- Command handlers are coroutines run as event-loop tasks
  (`internets.py - IRCBot._dispatch()` / `_run_cmd()`), bounded by
  `_CMD_TIMEOUT = 60` s and a `_MAX_TASKS = 50` concurrent-task cap. A handler doing
  blocking I/O MUST offload it (`await asyncio.to_thread(...)`) or it stalls the whole
  bot - the `BotModule` docstring and `example.py` both state this; nothing enforces it
  mechanically.
- `on_load` / `on_unload` / `on_raw` are synchronous and run on the event-loop thread.
  `on_raw` runs in the IRC read path for every line; a slow `on_raw` delays all
  processing. Exceptions from `on_raw` are caught and logged at debug by the fanout
  loop (`internets.py:888`), so a raising `on_raw` degrades silently rather than
  crashing the bot.
- `fetch_json`, `resolve_public`, and `cred` are thread-safe by construction (no shared
  mutable state); they are routinely called from `asyncio.to_thread` workers.

## Failure behavior

Documented per function below. The overall pattern the codebase follows (stated as the
canonical shape in `example.py`): the module-level sync fetch helper catches
`ResponseTooLarge`, `requests.RequestException`, and parse errors and returns a
finished user-facing string, so command coroutines do not raise. When one does raise
anyway, `IRCBot._run_cmd()` catches it, increments the `unexpected_errors` metric, and
notices the user - so a buggy module cannot take the bot down.

## Security

- `fetch_json` is the enforcement point for the repo-wide rule that no outbound HTTP
  call may buffer an unbounded body (see below).
- `resolve_public` and `strip_ctrl` are shared security chokepoints; their absence in a
  module that needs them is a defect in that module, not here.
- `cred` prevents template placeholders (which can include example email addresses)
  from leaking into outbound requests as PII-shaped values.
- `BotModule.forget()` is the privacy contract: `.forgetme` calls it on every loaded
  module, so right-to-erasure coverage is exactly the set of modules that override it.

## Functions

### fetch_json()

```python
fetch_json(url, *, ua, params=None, headers=None, timeout=10,
           max_bytes=262144, allow_404=False) -> Any
```

The mandatory helper for module HTTP. It exists because `requests.get(url).json()`
buffers the entire body before parsing: a compromised or misconfigured upstream can
return gigabytes (a JSON bomb) and OOM the process. Every module fetch goes through
this helper (or an equivalent inline stream+cap); a bare `r.json()` or unbounded
`r.text` in a module is a defect by project policy (memory: HTTP size caps).

Algorithm:

1. Builds headers with the caller's mandatory `ua` as `User-Agent` (keyword-only, so no
   module can forget it silently), merging optional extras. The dict is rebuilt per
   call - no cross-call mutation (pinned by
   `test_fetch_json.py - TestRequestWiring.test_caller_headers_do_not_mutate_across_calls`).
2. `requests.get(..., stream=True)` inside a `with` block, so the socket is released on
   every exit path (404 short-circuit, HTTP error, oversize, success) - a `stream=True`
   response left unclosed leaks the connection/FD.
3. If `allow_404` and status is 404: returns `None` ("lookup miss" semantics -
   dictionary word not found, unknown pokémon). Any other error status raises via
   `raise_for_status()`; a 500 raises even with `allow_404=True`
   (`test_fetch_json.py - TestNotFound.test_500_always_raises_even_with_allow_404`).
4. Reads `max_bytes + 1` bytes via `r.raw.read(max_bytes + 1, decode_content=True)`.
   If more than `max_bytes` came back, raises `ResponseTooLarge` BEFORE any decode or
   parse. Under the urllib3 2.x this repo requires (requests>=2.32.3; urllib3 2.7
   installed), the cap counts decompressed bytes, so a small gzip bomb cannot expand
   past the cap in memory. The boundary is exact: a body of exactly `max_bytes` passes,
   one byte more raises (`test_fetch_json.py - TestSizeCap`).
5. Decodes as UTF-8 with `errors="replace"` (invalid bytes can never raise
   `UnicodeDecodeError`) and parses with `json.loads`.

Error contract (callers catch exactly these):

| Exception | Cause |
|---|---|
| `requests.RequestException` | transport error, or non-404 HTTP status (`HTTPError` is a subclass) |
| `ResponseTooLarge` | body exceeded `max_bytes` |
| `json.JSONDecodeError` | body was not valid JSON (including empty body) |

Defaults: `timeout=10` (passed to requests as a scalar, so it bounds the connect and
each socket read, not total wall time - see Findings), `max_bytes=256 KiB`
(`_DEFAULT_MAX_JSON_BYTES`; modules with legitimately larger payloads pass an explicit
`max_bytes=`, e.g. `ipintel.py` 16 KiB-2 MiB per endpoint, `pkginfo.py` 2 MiB for
crates metadata).

What it does NOT do: destination validation. `fetch_json` will happily fetch
`http://169.254.169.254/`. It is only safe for fixed, developer-chosen hosts. A
user-influenced URL must go through `resolve_public()` or `_netsafe.safe_open()`
instead - `example.py` states this rule to module authors.

`requests` is imported inside the function so `base.py` stays importable without it.

### resolve_public()

```python
resolve_public(host: str, port: int = 0) -> list  # getaddrinfo result
```

Resolve-time SSRF check used by the network probers (`probe.py` `.headers` / `.ssl` /
`.tcp` / `.down`, and `scinews.py`). Normalizes the host (strip, drop trailing dot),
rejects empty or >253-char names, resolves via `socket.getaddrinfo`
(`SOCK_STREAM`), and raises `ValueError` unless EVERY resolved address is public - any
answer that `ipaddress` classifies as private, loopback, link-local, multicast,
reserved, or unspecified fails the whole resolution (the all-answers rule defeats
DNS responses that mix one public and one internal address). Behavioral evidence:
`test_probe.py` pins private-answer refusal, empty/oversized host, and NXDOMAIN.

The docstring itself flags the TOCTOU limit: this is resolve-time validation only; a
hostile DNS server can rebind between check and connect. Callers that connect by IP
should connect to an address from the returned list (which `probe.py` does), not
re-resolve the name. Callers that hand the hostname to an HTTP library need
`_netsafe.safe_open()`, which closes the TOCTOU with DNS pinning.

#### resolve_public vs _netsafe

Two SSRF guards coexist deliberately:

| | `base.resolve_public` | `_netsafe` (`resolve_safe_ip` + `safe_open`) |
|---|---|---|
| Used for | raw-socket probes that connect to the returned IP themselves | HTTP fetches of user-influenced URLs |
| TOCTOU | open (caller must connect to returned IP) | closed via thread-local DNS pin |
| Redirects | n/a | re-validated and re-pinned per hop |
| IPv4-mapped IPv6 | via `ipaddress.is_private` (correct on current interpreters, verified locally on 3.14.6) | explicit `ipv4_mapped` unwrap, interpreter-independent |
| IPv6 site-local `fec0::/10` | NOT blocked (see Findings) | blocked (`is_site_local`) |
| Metadata hostnames | not special-cased (IP check catches the resolved address) | `METADATA_HOSTS` name blocklist as defense-in-depth |
| Failure signal | `ValueError` | `None` / `SSRFBlocked` |

### cred()

```python
cred(cfg, secret_name, section, key, default="") -> str
```

Credential/PII lookup with precedence: `secret_store.get(secret_name)` first (which
itself checks the `INTERNETS_*` env override, then the 0600 `config.ini[secrets]`
tier - see the secret-storage model), then `cfg.get(section, key)` as a legacy
fallback for pre-2.4.0 installs that kept keys directly in the ini, then `default`.
Two hardening details:

- **Placeholder filter**: if the config value contains any `_PLACEHOLDER_MARKERS`
  substring (case-insensitive: `changeme`, `your-key`, `placeholder`,
  `set-in-secret-store`, `<your-`, `you@example`, `example.com`), the value is treated
  as unset and `default` is returned - template values never leak into outbound
  requests (e.g. a `you@example.com` contact address in a User-Agent). Pinned
  exhaustively by `test_modules_base.py - TestCredPlaceholderFilter`, including the
  mixed-case and embedded-substring cases.
- **Never raises on a broken environment**: `ImportError` on `secret_store` is
  swallowed, and `ConfigParserError` / `AttributeError` from a missing section/key or a
  `None` cfg return `default` (`TestCredDefensive`). A module using `cred` in
  `on_load` degrades to unconfigured instead of failing the load.

Note the filter applies only to the config fallback path, not to secret_store values -
the implementation implies secret_store contents are operator-entered real values, not
template text.

### help_row()

```python
help_row(prefix, usage, desc, *, width=24) -> str
```

Formats one `.help` line: two-space indent, `prefix + usage` padded to a 24-column
usage field, then the description. Usages at or past the width fall back to a single
separating space instead of wrapping. Aliases are written into `usage` as
`cmd/.alias`. Keeping every module on this one helper is what keeps `.help` uniform
and lets `.help <cmd>` match on the leading token. Tested in `test_help.py`
(per-line compactness bound).

### strip_ctrl()

```python
strip_ctrl(s: object, max_len: int = 400) -> str
```

The single sanitizer for third-party text spliced into an IRC line (API fields,
Location headers, user echoes). Strips the full C0 range `\x00-\x1f` plus `\x7f`,
then truncates to `max_len`. Rationale (docstring, confirmed against `sender.py`
behavior): the IRC sender only strips `\r\n\x00` as a transport backstop, so without
this, upstream text could inject bot-attributed formatting (`\x02` bold, `\x03`
color), CTCP (`\x01`), terminal escapes (`\x1b`), or BEL spam. Coerces non-`str`
input (`None` becomes `""`, `42` becomes `"42"`). Pinned by
`test_modules_base.py - TestStripCtrl`. Scope note: C1 controls (U+0080-U+009F) are
not stripped; IRC clients do not interpret them as formatting, and they cannot be
produced by C0-based injection.

## Classes

### BotModule

The abstract base every module subclasses. Constructor stores the bot reference
(`self.bot = bot`) and nothing else.

**`COMMANDS: dict[str, str]`** - maps command words (what the user types after the
prefix) to the NAME of an `async def` method on the class. Several words may map to
one method (aliases). The loader (`internets.py - load_module()`) registers each entry
in the global dispatch table and refuses to load a module whose words collide with
another loaded module.

**`__init_subclass__`** - validates the `COMMANDS` contract at class-definition time:
each named method must exist and be a coroutine function
(`inspect.iscoroutinefunction`; deliberately not the deprecated `asyncio` alias). A
typo or a sync handler is a `TypeError` at import/load, not an `AttributeError` the
first time a user runs the command in production. Pinned by
`test_modules_base.py - TestCommandsContractValidation` (missing handler, sync
handler, empty COMMANDS all covered).

**Handler signature** - every handler is
`async def cmd_x(self, nick: str, reply_to: str, arg: str | None)`; the dispatcher
calls `handler(nick, reply_to, arg)` under a 60 s timeout
(`internets.py - _run_cmd()`). `arg` is everything after the command word, or `None`;
the dispatcher has already rejected args over 400 chars (`_MAX_ARG_LEN`).

**Overridable hooks** (all default to no-ops; pinned by
`test_modules_base.py - TestBotModuleHooks`):

| Hook | When | Contract |
|---|---|---|
| `help_lines(prefix)` | `.help` rendering | return a list of `help_row()` lines; default `[]` |
| `is_configured()` | `.help` filtering | return False when a required key is missing; the module stays loaded and dispatchable (admins can add a key later) but invisible in `.help`; default True |
| `on_load()` | after registration, event-loop thread | read config/secrets once here, not per command |
| `on_unload()` | before removal (also at shutdown) | cancel tasks, flush state |
| `on_raw(line)` | every incoming IRC line, sync, read path | must be fast; exceptions are swallowed at debug level by the fanout |
| `forget(nick)` | `.forgetme` for each loaded module | modules persisting per-nick PII MUST override: erase, persist, return count removed; default returns 0 |

### The bot surface

`self.bot` is the `IRCBot` instance (`internets.py`). The methods and attributes
modules rely on (each cited symbol is in `internets.py` unless noted):

| Member | Behavior |
|---|---|
| `cfg` | live `ConfigParser`; read at use-time so `.rehash` changes take effect (see `_cmd_prefix()` for the pattern) |
| `privmsg(target, msg)` / `notice(target, msg)` | validate the target (non-empty, no spaces), split the body into 400-byte UTF-8-safe chunks (`_split_msg()`), and enqueue `PRIVMSG`/`NOTICE` lines |
| `reply(nick, reply_to, msg, privileged=False)` / `preply(...)` | PM goes to the nick; channel goes to the channel, or as a private notice to the nick when `privileged=True` (for output that should not be broadcast) |
| `send(msg, priority=1)` | raw enqueue to the token-bucket Sender (`sender.py`); `priority=0` bypasses the rate limit (used for PONG/QUIT), modules should not need it |
| `rate_limited(nick)` | per-nick API cooldown (`store.py - RateLimiter.api_check()`); call before doing real work (network/CPU); consumes the token when it returns False; NO admin bypass |
| `flood_limited(nick)` | per-nick command-flood gate with admin bypass; the dispatcher already applies this to every command, so modules rarely call it |
| `is_admin(nick)` | True only for an authed nick whose current hostmask matches the one bound at auth (fail-closed; see `is_admin()` comment block) |
| `is_chanop(channel, nick)` | channel-operator check, lock-guarded for to_thread callers |
| `loc_get/loc_set/loc_del(nick)` | per-nick saved weather location, persisted via `store.py` (PII - modules using it must participate in `forget`) |
| `channel_users(ch)` | membership snapshot from the store |
| `active_channels` | `ChannelSet` of currently joined channels |

## Package init

`modules/__init__.py` marks the directory as a package and documents the plugin
convention (`setup(bot) -> BotModule`). Its `__all__ = ["base"]` is effectively
decorative: the loader imports module files directly by path as `modules.<name>`
(`load_module()`), never via `from modules import *`. No behavior; nothing else to
document.

## Relationship to weather_providers/_http.py

`fetch_json` is not duplicated there, but the concern is: `weather_providers/_http.py -
get_json()` is a second, independent size-capped JSON fetcher for the weather
subsystem - async (aiohttp with a cached per-loop session, falling back to
requests-in-thread), 1 MiB default cap streamed incrementally in 64 KiB chunks, and a
different error contract (everything wrapped in `HTTPError` with `status` /
`is_rate_limit`, vs. `fetch_json`'s three-exception contract). The split is justified:
the weather dispatcher fans out across provider chains and needs true async I/O plus
typed rate-limit detection, while module fetches are one-shot calls already running in
`to_thread`. Both enforce the same invariant (no unbounded body buffering); neither
validates destinations (weather URLs are developer-fixed). Maintainers should treat
them as two enforcement points of one policy - a cap change in one is not inherited by
the other.

## Implementation walk

- Lines 1-15: future import, lazy-import-friendly stdlib imports, `TYPE_CHECKING`
  import of `IRCBot` (avoids a runtime cycle), and `_DEFAULT_MAX_JSON_BYTES` with the
  rationale comment naming the outlier modules (poke ~1 MB, numberfact ~4 MB).
- Lines 18-24: `ResponseTooLarge` - dedicated exception so callers can distinguish
  oversize from transport errors.
- Lines 27-70: `fetch_json` (above).
- Lines 73-108: `resolve_public` (above).
- Lines 111-146: `_PLACEHOLDER_MARKERS` + `cred` (above).
- Lines 149-171: `_HELP_USAGE_W` + `help_row` (above).
- Line 174: `_IRC_CTRL_RE = __import__("re").compile(...)` - compiles the C0+DEL
  regex once at import. The `__import__("re")` inline form avoids a top-level `import
  re` for one constant; functionally identical, stylistically an outlier (every other
  lazy import in the file uses a function-local `import`).
- Lines 177-193: `strip_ctrl` (above).
- Lines 196-286: `BotModule` (above). No other code; nothing unreachable.

## Findings

- questionable | `base.py - resolve_public()` | Does not block IPv6 site-local
  `fec0::/10`: `is_site_local` is unchecked and on the current interpreter
  (verified: Python 3.14.6, `fec0::1` has `is_private=False`, `is_reserved=False`)
  none of the checked predicates catch it, while `_netsafe.ip_is_blocked()` blocks it
  explicitly - the two SSRF guards disagree. Exploitability is low (deprecated range,
  requires a AAAA answer plus a route), but the check sets should match.
- questionable | `base.py - fetch_json()` | The scalar `timeout=10` bounds connect
  and each socket read, not total duration; a slow-drip upstream can hold a
  `to_thread` worker (and its `_MAX_TASKS` slot) far past 10 s, backstopped only by
  the dispatcher's 60 s command timeout - which cancels the coroutine but not the
  underlying thread. Same pattern in `_netsafe.safe_open()`.
- questionable | `base.py - cred()` | Only `ImportError` is caught around
  `secret_store.get()`; any other exception from the secret store propagates out of
  `cred` and would fail a module's `on_load`, contradicting the "never raises on a
  fresh install" claim in `example.py`'s commentary (accurate for fresh installs,
  not for a corrupt store).
- test-gap | `base.py - fetch_json()` | No test drives a real compressed
  (gzip) body through the cap, so the decompressed-bytes-counted property rests on
  urllib3 2.x semantics rather than a pinned test (`test_fetch_json.py` fakes
  `r.raw`).
