# Module System

Every user-facing command in the bot except the core admin set lives in a
module: one `.py` file under `modules/`, loaded by path at startup or at
runtime by an admin. This document is the integrator and operator view of that
system: how modules are found, loaded, bound to command words, gated, reloaded,
and what state a reload destroys.

Three companion documents carry detail this one deliberately does not repeat:

- [Command Reference](command-reference.md) - every command, its syntax, and
  its behavior. Generated from real registration.
- [Writing Modules](writing-modules.md) - the development guide: how to build
  a module that satisfies the contract described here.
- [internals/modules/index.md](internals/modules/index.md) - one implementation
  page per module, with line-level citations and per-module findings.

Source of truth for this page: `modules/base.py`, `internets.py -
IRCBot.load_module()` / `unload_module()` / `reload_module()` / `_dispatch()`,
and `admin_cmds.py - AdminCommandsMixin`.

---

## 1. What a module is

A module is a single file `modules/<name>.py` that exposes a top-level
`setup(bot)` factory returning a `BotModule` instance. There is no class
auto-discovery and no registration decorator: `setup()` is the only required
entry point, and a file without it is rejected at load
(`internets.py - load_module()`).

```python
from __future__ import annotations
from .base import BotModule

class PingModule(BotModule):
    COMMANDS: dict[str, str] = {"ping": "cmd_ping"}

    async def cmd_ping(self, nick, reply_to, arg):
        self.bot.privmsg(reply_to, f"{nick}: pong")

def setup(bot):
    return PingModule(bot)
```

That is a complete, loadable module. Everything else in the contract
(`help_lines`, `is_configured`, the lifecycle hooks, `forget`) is optional and
defaults to a no-op on `BotModule`.

The file count is not the module count. `modules/` holds 75 `.py` files:

| Category | Count | Files |
|---|---|---|
| Loadable modules | 70 | everything not listed below |
| Helpers (no `setup()`, no commands) | 5 | `__init__`, `base`, `geocode`, `units`, `_netsafe` |

Of the 70 loadable modules, 69 register at least one command word.
`linktitle` registers none: it is passive, driven entirely by the `on_raw`
fanout, and exists only to announce titles for URLs posted in a channel.
Across the 69 there are 165 primary commands (aliases folded into their
handler), plus 4 public and 23 admin core commands that live in
`admin_cmds.py` rather than in any module.

## 2. Discovery and autoload

There is no directory scan at startup. The bot loads exactly the modules named
in `config.ini [bot] autoload`, comma-separated, in list order
(`config.py` `AUTO_LOAD`, consumed by `internets.py -
IRCBot.autoload_modules()`). `[bot] modules_dir` selects the directory and
defaults to `modules`.

A module on disk but absent from `autoload` is inert until an admin runs
`.load <name>`. The shipped `config.ini.example` autoloads 67 of the 70
modules; `example`, `health`, and `privacy` are omitted.

:::{warning}
Omitting `privacy` from `autoload` removes `.forgetme`, `.privacy`, `.optout`,
and `.optin` from the running bot. `.forgetme` is the right-to-erasure entry
point that fans `forget()` across every loaded module, so a deployment that
copies `config.ini.example` verbatim ships with no user-facing erasure command.
Add `privacy` (and `health`, for the operator snapshot) to the autoload list.
:::

`.modules` reports what is loaded, with a per-module command count, then lists
every other `*.py` in the directory as "available". That availability list
excludes `__init__`, `base`, `geocode`, and `units` but not `_netsafe`, which
the loader will refuse anyway because the module-name pattern
`^[a-z][a-z0-9_]*$` rejects a leading underscore.

## 3. The load path

`IRCBot.load_module(name)` runs under `self._mod_lock` and executes these steps
in order. Order matters: two of the failure modes below depend on it.

```{graphviz}
digraph load {
  rankdir=TB;
  node [shape=box, fontsize=10, fontname="Helvetica"];
  n  [label="name matches\n^[a-z][a-z0-9_]*$"];
  d  [label="not already loaded"];
  e  [label="modules/<name>.py exists"];
  t  [label="resolved path stays\ninside modules/"];
  x  [label="exec_module\n(fresh execution)"];
  s  [label="module defines setup()"];
  f  [label="inst = setup(bot)"];
  c  [label="no command-word conflict\nwith another module"];
  l  [label="inst.on_load()"];
  r  [label="register COMMANDS\ninto dispatch table"];
  n -> d -> e -> t -> x -> s -> f -> c -> l -> r;
}
```

1. **Name validation.** `^[a-z][a-z0-9_]*$`. `MyMod`, `my-mod`, and `_netsafe`
   are all rejected here.
2. **Already-loaded check.** Loading a loaded module fails; use `.reload`.
3. **Existence check** for `modules/<name>.py`.
4. **Traversal guard.** `path.resolve().relative_to(MODULES_DIR.resolve())`.
   A symlink whose real path escapes the modules directory is blocked.
5. **Fresh execution.** `importlib.util.spec_from_file_location("modules.
   <name>", path)` then `exec_module`. The file is executed from source on
   every load, and the resulting module object is not inserted into
   `sys.modules`. That is what makes `.reload` pick up edits to the file.
   `BotModule.__init_subclass__` runs here, so a bad `COMMANDS` mapping fails
   the load at this point.
6. **`setup` presence check**, then `inst = mod.setup(self)`.
7. **Conflict check.** Any command word already owned by a *different* loaded
   module aborts the load. First loader wins; the second is rejected with the
   colliding words named.
8. **`inst.on_load()`**, then each `COMMANDS` entry is written into
   `self._commands[word] = (module_name, method_name)`.

Two consequences of that ordering:

- `setup()` runs **before** the conflict check, so a `setup()` with side
  effects (opening a file, starting a thread) performs them even when the load
  is then rejected for a conflict. Keep `setup()` to a constructor call and put
  real work in `on_load()`.
- `on_load()` runs **before** registration, so a raising `on_load()` aborts the
  load with nothing registered. `on_unload()` is not called on that path, so
  anything `on_load()` acquired before raising is leaked.

## 4. Command binding and dispatch

Registration is a flat, global word-to-owner map. There is no per-module
namespace and no prefix scoping: `.dns` belongs to whichever module claimed it
first.

`IRCBot._dispatch()` resolves a command word against `AdminCommandsMixin._CORE`
**first**, and only then against the module map. Core wins every collision.

:::{admonition} Known defect: `.uptime` is shadowed
:class: warning
`modules/health.py` registers `uptime`, which is also in
`AdminCommandsMixin._CORE`. Because `_dispatch()` resolves `_CORE` first,
health's public `.uptime` handler is permanently unreachable while its
`help_lines()` still advertises it. This is the only shadowed name in the whole
command set, checked programmatically against every module's `COMMANDS`. See
the reconstruction findings ledger.
:::

Before a handler is ever reached, the dispatcher applies a fixed gate chain
(shadow-ban, PM-only for `.auth`/`.deauth`, per-nick flood limit, per-channel
burst limit, 400-character argument cap, concurrent-task cap). The full chain
with its firing behavior is tabulated in
[Command Reference](command-reference.md#gate-chain). Two numbers matter to
module authors: `_MAX_ARG_LEN = 400`, so `arg` can never exceed 400 characters;
and `_CMD_TIMEOUT = 60`, the `asyncio.wait_for` bound `IRCBot._run_cmd()` wraps
around every handler.

The per-nick API cooldown (`bot.rate_limited(nick)`, default 10 s) is **not**
part of the dispatch chain. It is opt-in per command, and the module calls it.

## 5. Lifecycle hooks

All four hooks default to no-ops on `BotModule`. All are synchronous and run on
the event-loop thread.

| Hook | Called | Contract |
|---|---|---|
| `on_load()` | after `setup()`, before registration | read config and secrets once; start tasks; load state |
| `on_unload()` | before removal, and for every module during shutdown | cancel tasks, final flush; last chance to persist |
| `on_raw(line)` | every inbound IRC line | must be fast and must not block; exceptions are caught and logged at debug |
| `forget(nick)` | once per loaded module by `.forgetme` | erase that nick's records, persist, return the count removed; default returns 0 |

### on_raw placement

The fanout sits early in `IRCBot._handle_line()`, after IRCv3 tag stripping and
after the PING and PONG short-circuits, and before the CAP, numeric,
membership, and PRIVMSG handlers. Practical consequences:

- `on_raw` never sees PING or PONG lines.
- It runs on every other line, including lines that are empty after tag
  stripping, so handle the empty string.
- A shadow-banned sender's lines skip the fanout entirely, which is how
  `.seen` and `.tell` avoid recording a shadow-banned user while the bot still
  tracks them internally.
- A slow `on_raw` delays all inbound processing for every user. It is on the
  read path, not on a task.

### forget and the erasure surface

`.forgetme` calls `forget()` on every loaded module, so right-to-erasure
coverage equals exactly the set of modules that override it. Five do: `seen`,
`tell`, `notes`, `remind`, and `steam`. Saved weather locations are not covered
by a module `forget()`; `location.py` defines none, and `privacy.cmd_forgetme`
erases them by calling the core `bot.loc_del(nick)` directly.

## 6. `is_configured()`: visibility, not access control

`is_configured()` defaults to `True`. A module overrides it to report that a
required credential is absent. Seven modules do, all returning `False` when
their key is missing: `imdb`, `lastfm`, `satpass`, `steam`, `stocks`,
`twitch`, `youtube`.

What it gates, verified against every call site: four branches of
`admin_cmds.py - cmd_help()`, and `cmd_stats()`.

| Surface | Effect when `is_configured()` is `False` |
|---|---|
| `.help` (compact list) | the module name is dropped for non-admins; admins still see it, plus an `(N hidden, no key)` count |
| `.help all`, `.help <module>`, `.help <cmd>` | the module's commands are omitted for non-admins; admins see them and a trailing `(hidden, no key: ...)` list |
| `.stats` | the module is excluded from the configured-module count |

What it does **not** gate:

- **Dispatch.** `IRCBot._dispatch()` never consults `is_configured()`. A
  command from an unconfigured module still resolves, still spawns a task, and
  still runs its handler. Whatever the handler does with a missing key is the
  handler's own behavior, which is why the keyed modules each begin with their
  own explicit check and a "not configured" reply.
- **Loading.** An unconfigured module loads and stays loaded. This is
  deliberate: an admin can `.load` a module and add its key afterwards.

The rule is therefore: `is_configured()` hides, it never disables. Never use it
as a security boundary. A module that must refuse to act without a credential
has to refuse inside its handler.

One inconsistency to be aware of: `search.py` does not override
`is_configured()`, although `.si` / `.gi` (image search) require `brave_key`.
Those commands stay visible in `.help` on a keyless install and return an error
string when invoked.

## 7. Hot load, unload, and reload

| Command | Effect |
|---|---|
| `.load <mod>` | run the load path in section 3 |
| `.unload <mod>` | `on_unload()`, then delete every command word owned by that module |
| `.reload <mod>` | `unload_module()` then `load_module()`, nothing more |
| `.reloadall` | `.reload` over every currently loaded module, sequentially |
| `.restart` | full `execv` of the process |

All are admin-gated and audit-logged (`admin_cmds.py - cmd_load()` and
siblings).

### What a reload does not preserve

`reload_module()` is literally unload-then-load. Nothing is migrated between
the old instance and the new one.

| Lost on reload | Survives a reload |
|---|---|
| the module instance and every attribute on it | anything the module flushed to disk in `on_unload()` |
| module-level globals of the reloaded file (in-memory caches, counters, compiled state) | the cached helper modules and their module-level state |
| any `asyncio.Task` the module started, unless `on_unload()` cancels it | core bot state: locations, channel membership, admin sessions |

A task the module created and did not cancel in `on_unload()` is **not**
cancelled by the loader. It keeps running against the dead instance, and the
reloaded instance creates a second one. That is the most common reload leak;
the correct pattern is in [Writing Modules](writing-modules.md#7-persistent-state).

### Helper edits do not reload

The reloaded file is executed fresh, but its imports are not. `from .base
import ...`, `from .geocode import ...`, `from ._netsafe import ...`, and
`import weather_providers` all resolve through `sys.modules`, which the loader
never clears. Editing `modules/base.py`, `modules/geocode.py`,
`modules/units.py`, `modules/_netsafe.py`, or anything under
`weather_providers/` and then `.reload`-ing a command module picks up **none**
of the change. Use `.restart` for helper-level edits. `.reloadall` has the same
limitation: it re-runs load for every command module and still leaves the
helpers cached.

### A failed reload leaves the module unloaded

`reload_module()` unloads first. If the subsequent load fails, for example
because the file being edited has a syntax error or a `COMMANDS` typo that
trips `__init_subclass__`, the module is gone: its commands are deregistered
and nothing restores them. The reply names the failure, but the running bot has
silently lost that command set until the file is fixed and `.load` succeeds.
`.reloadall` reports the same condition as a `FAILED: <names>` list, and every
name in it is now unloaded.

Edit, then `.reload`, then confirm with `.modules` before assuming the module
is back.

## 8. Load-time error handling

Every exception raised anywhere in the load path is caught, logged to the
`modules` subsystem logger as `event=module_load_failed`, and reported to IRC
as `Error loading '<name>' - see log for details.` No traceback and no
exception text reaches the channel, so diagnosing a failed load always means
reading `internets.log`.

Two classes of failure are distinguishable from the reply itself because they
are checked before the generic handler:

- `'<name>' conflicts on: <words>` - the conflict check in step 7.
- `'<name>' has no setup().` - step 6.

The `COMMANDS` contract check is not one of them. `BotModule.__init_subclass__`
raises `TypeError` at class-definition time when a command maps to a missing
method or to a non-`async def` one, and that surfaces only as the generic
message. The same `TypeError` fails the test suite at collection, which is
where it is meant to be caught.

## 9. Module catalog

The inventory below is generated, not hand-maintained. Regenerate it with:

```console
$ python scripts/gen-command-reference.py
```

The same script has a `--check` mode that exits non-zero when a registered
command name is missing from a document, which is how
[Command Reference](command-reference.md) is gated against drift. For per-command
syntax and behavior, read that document; for implementation detail on a single
module, read its page under
[internals/modules/index.md](internals/modules/index.md).

| Module | Commands (aliases) |
| --- | --- |
| advice | `.advice` |
| apod | `.apod` |
| astro2 | `.launches`, `.moon`, `.neo`, `.sky`, `.solar` |
| bofh | `.bofh` (`.excuse`) |
| bored | `.bored` |
| calc | `.cc` |
| catfact | `.catfact` (`.cat`) |
| channels | `.join`, `.part`, `.users` |
| chuck | `.chuck` |
| cocktail | `.cocktail` (`.drink`) |
| cowsay | `.cowsay` |
| crypto | `.gecko` (`.coingecko`, `.cg`) |
| dadjoke | `.dadjoke` (`.joke`) |
| devtools | `.color`, `.cron`, `.jwt`, `.semver`, `.tz`, `.unix`, `.uuid5` |
| devutils | `.b64`, `.epoch`, `.hex`, `.morse`, `.unb64`, `.uuid` |
| dice | `.d` |
| dictionary | `.dict` (`.dictionary`) |
| dnd | `.dnd` |
| dnsutils | `.asn`, `.caa`, `.dns`, `.rdns`, `.whois` |
| encode | `.ascii`, `.b32`, `.crc`, `.defang`, `.ds`, `.entropy`, `.hash`, `.lorem`, `.pw`, `.slug`, `.ulid`, `.unicode` |
| example | `.example` |
| fact | `.fact` |
| fml | `.fml` |
| fx | `.fx` |
| games | `.8ball`, `.choose`, `.coin`, `.rps` |
| ghinfo | `.gh` |
| health | `.health`, `.uptime` |
| hn | `.hn` |
| httpcode | `.http` |
| idlerpg | `.irpg` (`.idlerpg`) |
| imdb | `.imdb` |
| ipinfo | `.ipinfo` |
| ipintel | `.ip` (`.rep`) |
| iss | `.iss` |
| lastfm | `.lastfm` |
| linktitle | (none - passive `on_raw` module) |
| location | `.delloc`, `.myloc`, `.regloc` (`.register_location`) |
| mathx | `.base`, `.bignum`, `.const`, `.factor`, `.gcd`, `.isprime`, `.pct`, `.roman`, `.stats` |
| mtg | `.mtg` |
| netcalc | `.cidr`, `.port`, `.subnet` |
| notes | `.notes` |
| numberfact | `.numberfact` (`.nf`) |
| physcalc | `.baud`, `.escape`, `.ly`, `.ohm`, `.rc`, `.sr` |
| pkginfo | `.crates`, `.npm`, `.pypi` |
| poke | `.poke` (`.pokemon`) |
| privacy | `.forgetme`, `.optin`, `.optout`, `.privacy` |
| probe | `.down`, `.headers`, `.ssl`, `.tcp` |
| qdb | `.qdb` |
| qr | `.qr` |
| recipe | `.recipe` (`.meal`) |
| reddit | `.reddit` (`.r`) |
| reflookup | `.arxiv`, `.doi`, `.element`, `.isbn`, `.rfc`, `.rtfm`, `.so`, `.wiki` |
| remind | `.remind`, `.remind-cancel`, `.remind-list` |
| satpass | `.passes` |
| scholar | `.papers`, `.scholar`, `.thesis` |
| scinews | `.sci` |
| search | `.si` (`.gi`), `.sw` (`.g`) |
| secinfo | `.cipher`, `.cve`, `.cvss`, `.hashid`, `.pwn` |
| seen | `.seen` |
| spacex | `.spacex` |
| steam | `.regsteam` (`.register_steam`), `.steam` |
| stocks | `.crypto`, `.stock` (`.s`) |
| tell | `.tell`, `.tell-cancel`, `.tell-list` |
| translate | `.t` (`.translate`) |
| twitch | `.tw` (`.twitch`) |
| urbandictionary | `.u` (`.urbandictionary`) |
| urls | `.expand` (`.unshorten`), `.shorten` |
| weather | `.alerts` (`.al`), `.aqi` (`.air`), `.astro` (`.sun`), `.forecast` (`.f`), `.history` (`.hist`), `.hourly` (`.h`), `.marine` (`.sea`), `.nowcast` (`.nc`), `.pollen` (`.allergy`), `.providers`, `.space` (`.aurora`), `.tides` (`.tide`), `.uv` (`.uvi`), `.weather` (`.w`), `.wildfire` (`.fire`) |
| xkcd | `.xkcd` |
| youtube | `.yt` (`.youtube`) |
| core (public) | `.auth`, `.help`, `.modules`, `.version` |
| core (admin) | `.act`, `.audit`, `.deauth`, `.debug`, `.fingerprint`, `.load`, `.loglevel`, `.mode`, `.nick`, `.raw`, `.rehash`, `.reload`, `.reloadall`, `.restart`, `.say`, `.shadow-ban`, `.shadow-list`, `.shadow-unban`, `.shutdown`, `.snomask`, `.stats`, `.unload`, `.uptime` |

Totals as generated on 2026-08-15: 165 primary module commands, 4 core public,
23 core admin. The `linktitle` cell is annotated here; the generator emits it
empty.

### Credential gating at a glance

Only these seven modules disappear from `.help` without a key. Everything else
either needs no credential or degrades at call time.

| Module | Requires |
|---|---|
| imdb | `omdb_key` |
| lastfm | `lastfm_key` |
| satpass | `n2yo_api_key` |
| steam | `steam_key` |
| stocks | at least one of `finnhub_key`, `alphavantage_key`, `twelvedata_key` |
| twitch | `twitch_client_id` and `twitch_client_secret` |
| youtube | `youtube_key` |

The weather subsystem gates differently. `modules/weather.py` is always
configured; the gating happens one layer down, per provider. Exactly **20 of
the 32 registered providers are key-gated**: their factory in
`weather_providers/__init__.py` returns `None` when the credential is absent,
so the provider never enters the dispatch chain. The remaining 12 are keyless
(`currentuvindex`, `eccc`, `gdacs`, `metno`, `nasapower`, `nifc`,
`noaa_coops`, `nws`, `openmeteo`, `pollendotcom`, `sunrisesunset`, `swpc`) and
cover the common capabilities on a fresh install. `weatherkit` additionally
requires PyJWT to be importable, not just its four credential fields. See
[Weather Providers](providers.md).

Every module's outbound endpoint, credential, and failure mode is catalogued in
[Integrations](integrations.md).

## 10. Helper modules

These five files live in `modules/` but are not modules. They define no
`COMMANDS` and no `setup()`, cannot be autoloaded, and are imported by the
command modules.

| File | Role |
|---|---|
| `base.py` | `BotModule` plus the shared helpers: `fetch_json`, `resolve_public`, `cred`, `strip_ctrl`, `help_row`, `ResponseTooLarge` |
| `_netsafe.py` | SSRF guard for user-supplied URLs: `safe_open`, `url_is_safe`, `resolve_safe_ip`, `SSRFBlocked`, with thread-local DNS pinning across redirects |
| `geocode.py` | location classification and resolution via Nominatim and Zippopotam, with a TTL and LRU cache |
| `units.py` | dual-unit temperature, wind, pressure, and distance formatting for weather output |
| `__init__.py` | package marker; documents the `setup(bot) -> BotModule` convention and nothing else |

`base.py` and `_netsafe.py` are security chokepoints, not conveniences.
`fetch_json` is the enforcement point for the repository-wide rule that no
outbound HTTP call may buffer an unbounded body, and `strip_ctrl` is the single
sanitizer standing between third-party text and a bot-attributed IRC line. Both
are covered in [Writing Modules](writing-modules.md) and, at line level, in
[internals/modules/base.md](internals/modules/base.md) and
[internals/modules/_netsafe.md](internals/modules/_netsafe.md).

## 11. Known defects in the module surface

Recorded here so nobody documents around them. Each is verified against source;
the reconstruction findings ledger carries the full record.

- **`.uptime` shadowing** - `modules/health.py` registers a command word owned
  by `_CORE`; its handler is unreachable while `help_lines()` advertises it.
  Section 4.
- **`health` reads the wrong Store fields** - `modules/health.py` reads
  `_dirty_locations` / `_dirty_channels`; `store.py` defines `_dirty_locs` /
  `_dirty_chans`, so `.health` prints `?` for those two rows permanently.
- **`mathx.cmd_isprime` blocks the event loop** - `_isprime` runs
  synchronously on the loop rather than through `asyncio.to_thread` (the
  sibling `cmd_bignum` does offload), and a composite surviving trial division
  falls into an unbounded `_pollard_rho`. A pasted 100-digit semiprime stalls
  the whole bot. Any-user denial of service.
- **`stocks` leaks API keys to the channel** - the all-providers-failed reply
  appends `str(exception)`, and urllib3 transport errors embed the full request
  query including `token=` / `apikey=`. `sender.redact_secrets` is log-only and
  does not scrub PRIVMSG.
- **`modules/example.py` comments have drifted** - the skeleton implies an
  admin bypass on the API cooldown that does not exist, and overstates its own
  input bounding. Read [Writing Modules](writing-modules.md) as the authority.
- **Module JSON stores have no integrity envelope** - `seen.json`,
  `tells.json`, `notes.json`, `reminders.json`, `steamids.json`, and
  `shadow_bans.json` are bare JSON with an atomic write but no checksum and no
  quarantine. A corrupt file loads as empty and is overwritten by the next
  flush. Only `store.py`'s three datasets are recoverable. See
  [State and Persistence](state-and-persistence.md#two-persistence-patterns).

## See also

- [Writing Modules](writing-modules.md) - build one.
- [Command Reference](command-reference.md) - use them.
- [Runtime Architecture](architecture.md#71-module-system) - where the module
  system sits in the process.
- [Administration](administration.md#module-management) - the operator view of
  `.load` / `.unload` / `.reload`.
- [Configuration](configuration.md) - `autoload`, `modules_dir`, cooldowns,
  and credential placement.
- [internals/modules/index.md](internals/modules/index.md) - per-module
  implementation reference.
