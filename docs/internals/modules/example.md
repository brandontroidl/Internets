# example.py - the copy-and-fill template module

`modules/example.py` (160 lines) is a tutorial artifact: a complete, loadable
`BotModule` whose working command is deliberately trivial (`.example <text>` echoes
the argument uppercased) so a new module author can `.load example`, watch it run,
and then replace the body. Roughly half the file is commentary teaching the
conventions the other 70+ modules follow; only ~30 lines execute. It should be read
as the practical companion to the [base contract](base.md) - and this doc's main job
is to certify that what it teaches matches what the code actually does.

## Commands

| Command | Usage | Behavior |
|---|---|---|
| `.example` | `.example <text>` | replies `<nick>: <TEXT>` (control-stripped, capped at 200 chars, uppercased); usage line when no argument |

`is_configured()` is not overridden (no key needed); `setup(bot)` returns
`ExampleModule(bot)` per the loader contract.

## What the live code demonstrates

`ExampleModule.cmd_example()` walks the canonical handler shape in order, each step
matching the real dispatch path in `internets.py`:

1. **Usage before spending tokens** - empty `arg` gets a usage reply (prefix read
   live from `self.bot.cfg["bot"]["command_prefix"]`, matching the use-time-read
   convention of `IRCBot._cmd_prefix()`) and returns before the rate limiter is
   consulted, so a fumbled invocation does not burn the user's API cooldown.
2. **Rate limiting** - `self.bot.rate_limited(nick)` before doing work, with the
   refusal sent as a private `notice` to the nick (not channel spam). Matches
   `store.py - RateLimiter.api_check()` semantics (per-nick cooldown, consumed on
   success). See Findings for a misleading comment here.
3. **Output sanitization** - `strip_ctrl(arg, _MAX_INPUT)` before the text enters a
   bot-emitted line; `_MAX_INPUT = 200` doubles as the interpolation bound the
   header comment prescribes for URLs/identifiers.

`help_lines()` returns one `help_row()` line, demonstrating the alias notation
(`"example/.ex <text>"`) in its docstring.

## What the commentary teaches (verified against the real contract)

Each teaching block was checked against the implementation it describes:

- **COMMANDS validation** (header, lines 21-24): matches
  `base.py - BotModule.__init_subclass__()` - typo or sync handler raises
  `TypeError` at class definition, i.e. at `.load` time, and the loader converts it
  into a load failure message.
- **The `_fetch_sync` network shape** (lines 38-73, commented out): module-level
  sync function doing fetch + parse + format, returning a finished string on EVERY
  path, called via `asyncio.to_thread`. The three except clauses mirror
  `fetch_json`'s exact error contract (`ResponseTooLarge`,
  `requests.RequestException`, broad `Exception` for parse surprises). This is
  genuinely the dominant pattern (about two dozen modules call `fetch_json`;
  spot-checked `fml.py` and `spacex.py - _fetch_sync()` follow the
  catch-all-return-string shape exactly).
- **SSRF warning** (lines 68-72): correctly states that `fetch_json` size-caps but
  does not validate destinations, and routes user-derived URLs to
  `base.resolve_public()` / `_netsafe.safe_open()` with accurate module citations
  (`probe.py`, `urls.py`, `scinews.py`).
- **`on_load` / `cred` guidance** (lines 82-101): matches `base.cred()` precedence
  (secret_store, then config, then default) and the shared-UA convention
  (`weather_user_agent` reused rather than per-module UA sections). The
  "never raises on a fresh install" claim is accurate for the fresh-install case;
  the corrupt-store caveat is recorded in [base.md](base.md#findings).
- **`is_configured` gating** (lines 96-101): matches `base.py` - hides the module
  from `.help` while keeping dispatch live so an admin can add a key later.
- **`on_raw` caveat** (header, lines 10-13): "must be fast" matches the sync
  read-path fanout; "never raise - wrap the body" is stricter than the actual
  contract (the fanout in `internets.py:888` catches and logs at debug), which is
  the right direction for advice - a raising `on_raw` is silently swallowed, so
  wrapping with real logging is how a module keeps its own errors observable.
- **`forget()` guidance** (lines 140-145): matches `base.py - BotModule.forget()`
  and the atomic-persist pattern (`mkstemp` + `os.replace` + 0600) that `seen.py`
  and the store use.
- **Pattern index** (lines 148-154): the five "read this module for pattern X"
  citations were spot-checked; `weather.py _parse_weather_flags` /
  `_weather_cmd`, `geocode.py` TTL-LRU cache, and `reflookup.py` defusedxml all
  exist as described.

## Lifecycle, state, concurrency, failure, security

As per the base contract; nothing module-specific. No external integration, no
secrets, no persistent state, `forget()` correctly left at the default 0. The only
security-relevant behavior is the `strip_ctrl` demonstration, which is the point.

## Findings

- doc-drift | `example.py - cmd_example()` | The comment above
  `self.bot.rate_limited(nick)` says "admins bypass the flood gate", which is true
  of `flood_limited()` (applied earlier by `_dispatch`) but reads as a claim about
  the adjacent `rate_limited()` call - and `store.py - RateLimiter.api_check()` has
  no admin bypass. A module author copying this will wrongly expect admin-exempt
  API cooldowns.
- questionable | `example.py - _MAX_INPUT` | The header comment says to bound input
  "before interpolating it into a URL", but the commented `_fetch_sync` reference
  passes the raw `arg` to `fetch_json`'s `params=` (correct - requests URL-encodes
  params) and applies the bound only to the OUTPUT field; the teaching text and the
  taught code bound different things. Harmless as written, but the prose overstates
  what the template enforces.
