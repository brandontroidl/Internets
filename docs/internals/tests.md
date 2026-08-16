# tests/ - the test suite as behavioral evidence

## Purpose

The suite pins behavior, not implementation: security guards (SSRF, CRLF/NUL
injection, secret redaction, path traversal), fail-closed branches (admin auth,
audit-log key handling, config floors), formatting contracts for IRC output, and
regression tests written against specific observed incidents (each usually
carries the incident in its docstring). Two harnesses coexist deliberately:

- `tests/test_*.py` - the pytest suite. 40 files, discovered by
  `pyproject.toml [tool.pytest.ini_options] testpaths = ["tests"]`.
- `tests/run_tests.py` - a standalone, zero-dependency runner
  (`python tests/run_tests.py`). It needs nothing but the standard library
  plus the bot's own imports: a `@test("name")` decorator runs each function
  at definition time, counts pass/fail, prints `[PASS]`/`[FAIL]` markers
  (ASCII-safe for Windows cp1252 consoles), and exits 1 on any failure.

CI (`.github/workflows/tests.yml`) runs both, in that order. `run_tests.py` is
not a subset kept for machines without pytest only; it holds tests that exist
nowhere else (the `internets.py` helper tests - `ChannelSet`, `_backoff`,
nick-change tracking, `is_admin` fail-closed branches, inbound redaction - plus
the source-grep sanitizer completeness gate and the version-consistency
checks). Removing it would drop real coverage, not redundancy.

## Harness conventions

### conftest.py

Three lines: inserts the repo root into `sys.path` so `import protocol`,
`import store`, `from modules import ...` resolve without installing the
package. `run_tests.py` does the same insertion itself.

### The argv-pinning convention for config imports

`config.py` parses `sys.argv` at import time (and reads `config.ini` from the
current directory). Under pytest, `sys.argv` contains pytest's own arguments
(test paths, `-q`), which `config.py`'s CLI parser would reject or
misinterpret. Every test module whose import chain reaches `config.py`
therefore pins argv around the import:

```python
_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import admin_cmds            # or config, or botlog
sys.argv = _SAVED_ARGV
```

Used by `tests/test_config.py`, `tests/test_botlog.py`, and
`tests/test_admin_cmds.py`. `test_config.py` goes further: its `reimport`
fixture drives full module re-execution (`importlib.reload` after `chdir` into
a crafted temp config directory) and restores real state on teardown.

A second consequence of import-time config: the suite requires a staged
`config.ini` in the repo root. Locally the gitignored working copy serves; CI
stages `cp config.ini.example config.ini` before running anything.

### Async convention: no pytest-asyncio at runtime

Test docstrings state the convention explicitly (`tests/test_sender.py`,
`tests/test_admin_cmds.py`): async behavior is exercised by driving a real
event loop with `asyncio.run(...)` or `loop.run_until_complete(...)`, never
with `async def test_` functions. `pyproject.toml` sets
`asyncio_mode = "auto"` and lists `pytest-asyncio>=0.23` in the `dev` extra,
but the plugin is not required for the suite to pass - see Findings.

## Test-file map

One entry per file; the behavior each pins, per its docstring and content.

- `test_admin_cmds.py` (1186 lines, largest) - `admin_cmds.py`
  `AdminCommandsMixin` handlers driven through a `FakeBot` that subclasses the
  real mixin; auth, shadow-ban, module load/reload, rehash, audit trail.
- `test_airnow_purpleair.py` - AirNow/PurpleAir air-quality providers: EPA
  2024 PM2.5 AQI math, humidity correction, dominant-pollutant and
  nearest-sensor selection, no-coverage raise (dispatcher fallback contract).
- `test_astro2.py` - `modules/astro2.py`: solar/NEO/launch fetch formatting
  via monkeypatched `fetch_json`; pure moon-phase and sky-lookup helpers.
- `test_audit_log.py` - `audit_log.py` HMAC-chained tamper-evident log:
  record/verify/count, key generation and 0600 perms, fail-closed on
  unreadable key, tamper detection, rotation, legacy SHA-256 fallback.
- `test_botlog.py` - `botlog.py` safe formatter, debug filter, and the two
  import-time `sys.exit(1)` config guards (one in-process, one via a fresh
  subprocess with a tampered config value).
- `test_calc.py` - `modules/calc.py` safe expression evaluator: arithmetic,
  implicit multiplication, factorial/exponent bombs, unknown-name rejection.
- `test_config.py` - `config.py` config.ini loading, CLI parsing, three-tier
  secret resolution; the `reimport` fixture re-executes the module against
  crafted temp configs.
- `test_crypto_cache.py` - `modules/crypto.py` coin-id cache: FIFO bound under
  attacker-influenceable distinct queries (unbounded-growth regression).
- `test_devtools.py` - `modules/devtools.py` pure dev tools: JWT decode,
  semver, UUID, timezones, unix time, color parsing, cron description.
- `test_dispatcher.py` - **name trap: this is `weather_providers/_dispatch.py`,
  not bot command dispatch.** `force_provider`, accuracy-first sort key,
  `DEFAULT_RELIABILITY` shape, `provider_status()`/`provider_capabilities()`.
- `test_dnsutils.py` - `modules/dnsutils.py` DNS/RDAP/ASN lookups: formatting
  of each `_*_sync` helper with canned `fetch_json` responses.
- `test_encode.py` - `modules/encode.py` pure offline codecs and generators
  (hashes, base32, slug, ULID, defang, entropy, password, lorem).
- `test_fetch_json.py` - `modules.base.fetch_json`, the shared HTTP size-cap
  guard: pins the cap boundary and 404/malformed-JSON paths so an off-by-one
  cannot silently disable the OOM guard.
- `test_ghinfo.py` - `modules/ghinfo.py` GitHub repo formatting with canned
  responses, including the `ResponseTooLarge` path.
- `test_hashpw.py` - `hashpw.py`: real scrypt/bcrypt/argon2 code paths (cost
  params pinned to floors via env for speed), verify dispatch fail-closed
  branches, and the `main()` CLI via monkeypatched `getpass`.
- `test_help.py` - suite-wide `.help` regression gate: every primary command
  documented in `help_lines()`, lines IRC-safe under the 512-byte limit,
  alias separators normalized. Instantiates every `BotModule` subclass
  without running `__init__` (3 skips fire for module files with no
  `BotModule` subclass).
- `test_ipintel.py` - `modules/ipintel.py`: DNSBL/DShield/GreyNoise/AbuseIPDB
  stubs, Tor cache reset, verdict/format helpers, async `cmd_ip` via
  `asyncio.run` against a fake bot.
- `test_mathx.py` - `modules/mathx.py` pure math toolbox (primes, factoring,
  bases, stats, roman numerals, fibonacci).
- `test_metrics.py` - `metrics.py` Prometheus registry + loopback-only HTTP
  exporter; server tests bind 127.0.0.1 with an ephemeral port (port 0).
- `test_modules_base.py` - `modules/base.py`: `strip_ctrl` (full C0 + DEL
  sanitizer), `cred()` placeholder filter, `is_configured()`.
- `test_netsafe.py` - `modules/_netsafe.py` SSRF-safe fetch: all non-public
  ranges blocked (including IPv4-mapped IPv6), any-private-answer DNS
  rebinding defense, per-redirect-hop re-validation and re-pin.
- `test_new_weather_capabilities.py` - the uv/pollen/wildfire/space_weather/
  tides capabilities and the 14 providers added with them: capability
  auto-discovery per provider, mocked `fetch()` for the non-trivial parsers.
- `test_numberfact.py` - `modules/numberfact.py` `math_fact(n)`; retries
  randomized fact selection so the suite stays deterministic.
- `test_physcalc.py` - `modules/physcalc.py` physics/engineering calculators
  (light time, escape velocity, Ohm's law, RC, baud framing).
- `test_pkginfo.py` - `modules/pkginfo.py` PyPI/npm/crates formatting: happy
  path, 404 -> None, malformed payload.
- `test_pkginfo_validate.py` - the pkginfo package-name validator: rejects
  `..` traversal and control bytes before any fetch (the name is interpolated
  into the registry URL path).
- `test_probe.py` - `modules/probe.py` SSRF-guarded network probers:
  `base.resolve_public` refusal of non-public addresses; localhost must be
  refused.
- `test_process_lock.py` - `process_lock.py` PID lock: real on-disk lockfiles
  via `tmp_path`; only the liveness probe (and the Windows psutil import
  branch) is mocked.
- `test_protocol.py` - `protocol.py` pure IRC parsing: tags, ISUPPORT
  CHANMODES/PREFIX, mode-change parsing, NAMES entries, SASL PLAIN payload.
- `test_provider_fixes.py` - regression pins for two review-confirmed provider
  bugs: weatherbit historical wind stored raw m/s in a km/h field; tomorrowio
  hollow all-None air-quality result instead of failing over.
- `test_reflookup.py` - `modules/reflookup.py` reference lookups (arXiv, DOI,
  elements) with canned responses.
- `test_satpass.py` - `modules/satpass.py` N2YO satellite passes: key-gated
  module, HTTP mocked, local `FakeBot` with a real `ConfigParser`.
- `test_scholar.py` - `modules/scholar.py` keyless scholarly search: query
  construction asserted by capturing `fetch_json` kwargs; `parse_orcid` /
  `split_flags` pure helpers.
- `test_scinews.py` - `modules/scinews.py` STEM news aggregator: RSS parsing
  from byte fixtures, dedupe/rotation, reader.
- `test_secinfo.py` - `modules/secinfo.py`: CVE lookup (canned NVD JSON), HIBP
  k-anonymity range check (canned `requests.get`), hash-id/CVSS/cipher pure
  helpers.
- `test_secret_store.py` - `secret_store.py` two-tier env -> config.ini
  backend; `SECRETS_FILE` monkeypatched to a temp path, never the real
  config.ini; permission checks.
- `test_sender.py` - `sender.py` async priority queue + token-bucket rate
  limiting; real Sender/queue/drain loop against a `FakeWriter`, driven with
  `loop.run_until_complete`.
- `test_stocks.py` - `modules/stocks.py` number/change formatting helpers
  only (31 lines, smallest).
- `test_store.py` - `store.py` `Store` (persistence, pruning, corrupt-file
  quarantine) and `RateLimiter`.
- `test_weather_flags.py` - `modules/weather.py` `_parse_weather_flags`:
  per-provider alias map (`-aw`, `-vc`, `-nws`, ...), `-p`/`-l`/`-n` escape
  hatches, unknown-flag passthrough.

`run_tests.py` additionally covers, in its own sections: `protocol.py`,
`store.py` (including the corrupt-quarantine and privacy-floor prune tests),
`modules/calc.py`, `modules/dice.py`, the weather-provider protocol/configure
surface, `modules/units.py`, `sender.py` redaction (both directions plus the
inbound `_redact_inbound` path), `hashpw.py`, the `internets.py` helpers
(`ChannelSet`, `_backoff`, nick tracking, `is_admin` fail-closed), async
architecture checks, security-hardening and audit-pass regression pins
(`SEC-*`/`BUG-*` labels), module edge cases (`translate`, `mathx`,
`urbandictionary`, `geocode`, `qdb`, `channels`, `location`), and
version-consistency between `internets.__version__`, `pyproject.toml`, and
`docs/conf.py`.

## Fixtures and doubles

There is no shared fixture library: `conftest.py` holds no fixtures, and each
file builds its own doubles. The recurring patterns:

- **FakeBot** - a hand-built object exposing only what the code under test
  touches (`cfg`, `preply`/`privmsg`/`notice` sinks that append to a list,
  `loc_get`, `rate_limited`). Defined independently in `test_admin_cmds.py`,
  `test_satpass.py`, `test_scinews.py`, `test_dnsutils.py`,
  `test_ipintel.py`, `test_weather_flags.py`, and twice in `run_tests.py`.
  The `test_admin_cmds.py` variant is the deepest: it subclasses the real
  `AdminCommandsMixin` so the actual `cmd_*` coroutines run, and supplies
  `FakeModule`/`FakeStore`/`FakeSender` around them; only true externals are
  stubbed (audit-log singleton redirected to a temp file, password verify
  monkeypatched so no hash backend or real config is needed).
- **Transport stubs** - the dominant pattern for modules: monkeypatch the
  `fetch_json` name as imported into the module under test
  (`monkeypatch.setattr(pkginfo, "fetch_json", ...)`) with canned dict
  responses or raised exceptions. No test hits the real network.
- **Determinism stubs** - `getaddrinfo` monkeypatched in `test_netsafe.py` /
  `test_probe.py`; `os.kill`/liveness in `test_process_lock.py`; `getpass` in
  `test_hashpw.py`.
- **Real-filesystem tests** - `tmp_path`/`TemporaryDirectory` for `store.py`,
  `audit_log.py`, `process_lock.py`, `secret_store.py`: the real read/write
  code runs against real files, per the project's mocks-hide-integration-bugs
  policy.

The duplication of FakeBot across seven sites is a deliberate-looking
trade-off (each fake is minimal and local) but it means a bot-interface change
must be chased through each copy; see Findings.

## How to run

```text
cd ~/Internets                 # config.ini must exist in the cwd
python -m pytest tests/ -q     # pytest suite
python tests/run_tests.py      # standalone runner
```

Measured on this checkout (2026-08-15, Python 3.14, Fedora):

```text
python -m pytest tests/ -q  ->  1738 passed, 3 skipped in ~15s
python tests/run_tests.py   ->  Results: 213 passed, 0 failed
```

The 3 skips are `test_help.py` skipping module files that define no
`BotModule` subclass. The 1 warning locally (`Unknown config option:
asyncio_mode`) means pytest-asyncio is not installed in the environment; the
suite passes regardless because no test uses `async def test_`.

Caveats:

- **Serial only.** There is no pytest-xdist in the dev extras and the suite is
  not written for parallel workers: `test_config.py` re-imports modules after
  `chdir`, several files mutate `sys.argv`/`os.environ`/module singletons, and
  `test_metrics.py` binds live sockets. Run it serially as shown.
- CI additionally passes `--strict-markers` (also set in `addopts`) and runs
  the suite per-platform across Python 3.10-3.14; a local single-version pass
  does not prove the Windows or 3.10 legs.
- Both runners import `config.py` transitively, so a missing `config.ini`
  fails the run at collection/import, not with a helpful message. Copy
  `config.ini.example` to `config.ini` first on a fresh clone.

## Important behavior without tests

Detailed per-file test-gap findings live in the Findings sections of the
per-file docs under `docs/internals/` (nearly every top-level doc and many
module docs carry at least one). What follows is only the structural,
suite-level view.

- **No end-to-end dispatch test.** Nothing drives a raw inbound IRC line
  through `internets.py` parsing, command lookup, module handler, and
  `sender.py` output as one path. `internets.py` is tested only at the helper
  level (in `run_tests.py`), `sender.py` at the queue level, and modules at
  the handler/helper level; the wiring between them is exercised by no test.
  `internets.py` and `console.py` are likewise excluded from the coverage
  gate (`pyproject.toml [tool.coverage.run] omit`), so the gap is invisible
  to the coverage number by construction.
- **No console tests.** `console.py` (the stdin admin console) has no test
  file at all; `docs/internals/console.md` records the detailed gaps.
- **44 of the 75 files under `modules/` have no behavioral tests anywhere** (neither a
  `tests/test_*.py` file nor a `run_tests.py` section): advice, apod, bofh,
  bored, catfact, chuck, cocktail, cowsay, dadjoke, devutils, dictionary,
  dnd, example, fact, fml, fx, games, health, hn, httpcode, idlerpg, imdb,
  ipinfo, iss, lastfm, linktitle, mtg, netcalc, notes, poke, privacy, qr,
  recipe, reddit, remind, search, seen, spacex, steam, tell, twitch, urls,
  xkcd, youtube. Most are thin fetch-and-format wrappers, but the list
  includes stateful modules (notes, remind, tell, seen, idlerpg) and
  security-relevant ones (ipinfo, linktitle, urls - all URL/input handling).
  Note: search, seen, tell, and remind do appear in `run_tests.py`, but only
  in the source-grep sanitizer gate (asserting the module text references
  `strip_ctrl`), which checks presence of a call site, not behavior.
- **Weather providers are unevenly covered.** airnow, purpleair, metno, and
  the 14 new-capability providers have parser-level tests with mocked HTTP;
  several older providers (accuweather, openweathermap, pirateweather,
  meteomatics) appear in tests only via registration/priority/reliability
  assertions, not response parsing.
- **Misleading names.** `test_dispatcher.py` tests
  `weather_providers/_dispatch.py`, not bot command dispatch (there is no bot
  dispatch test to confuse it with, which is itself the previous finding).
  `test_astro2.py` vs `modules/astro2.py` is fine, but note `test_stocks.py`
  covers two formatting helpers only, not the stocks fetch path.
- **Sanitizer gate is text-matching.** The `run_tests.py` "modules that emit
  upstream/user text route it through a sanitizer" test greps module source
  for `strip_ctrl`; it is explicitly a completeness enumeration, but a module
  that imports the name without calling it on the emitted string would pass.

## Findings

- test-gap | internets.py / sender.py / modules | No end-to-end dispatch test:
  the raw-line -> handler -> reply path is wired by no test, and both entry
  points are omitted from the coverage gate, so the number cannot reveal it.
- test-gap | console.py | No test file references the console at all.
- test-gap | 44 modules listed above | No behavioral tests; four of them are
  covered only by a source-text grep in `run_tests.py`.
- questionable | pyproject.toml - `[tool.pytest.ini_options]` vs test
  docstrings | `asyncio_mode = "auto"` and the `pytest-asyncio>=0.23` dev
  extra contradict the suite's own stated convention ("the project has no
  pytest-asyncio plugin", `tests/test_sender.py` docstring); without the
  plugin installed the option only emits a warning, but an `async def test_`
  added under that assumption would silently no-op locally and run in CI,
  giving the two environments different test populations.
- questionable | FakeBot (7 independent definitions) | The bot-facing test
  double is re-declared per file with slightly different surfaces; an
  interface change to preply/privmsg or `cfg` shape must be chased through
  every copy, and nothing checks the fakes stay faithful to `IRCBot`.
- questionable | run_tests.py | 2705 lines in one file with ad-hoc sections;
  it duplicates part of the pytest suite while being the sole home of the
  `internets.py` helper tests, so the split of unique-vs-duplicated coverage
  is discoverable only by reading both.
