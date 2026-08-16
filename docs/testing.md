# Testing and verification

How to run the suites, what each one covers, the conventions a new test must
follow, and the automated gates that stand between a commit and `main`.

Line-level detail on individual test files lives in
[internals/tests.md](internals/tests.md); the workflow and packaging detail
lives in [internals/ci-and-packaging.md](internals/ci-and-packaging.md). This
page is the operational view: what to run, what a green result does and does
not prove, and where the honest gaps are.

## Prerequisite: config.ini must exist

`config.py` parses `config.ini` from the current working directory **at import
time**, and both runners import it transitively. A fresh clone has no
`config.ini` (the real file is gitignored because it carries the `[secrets]`
section), so the suite aborts during collection with an import error rather
than a helpful message.

```bash
cd ~/Internets
cp config.ini.example config.ini   # credential-free template
```

CI does the same in an explicit "Stage config.ini for tests" step in every job
that runs tests (`.github/workflows/tests.yml`). The template carries no
credentials, so a checked-out tree plus this copy is enough to load the bot for
testing. Never commit a populated `config.ini`.

## Running the suites

Both runners must be run. Neither is a superset of the other. The pytest suite
is 40 files (`tests/test_*.py`, discovered via
`pyproject.toml [tool.pytest.ini_options] testpaths = ["tests"]`); the
standalone runner is one 2705-line file.

```bash
cd ~/Internets                       # config.ini must exist in the cwd
python tests/run_tests.py            # standalone stdlib harness
python -m pytest tests/ -q           # pytest suite
```

Measured on this checkout (2026-08-15, Python 3.14, Fedora 43):

| Runner | Result | Wall time |
| --- | --- | --- |
| `python -m pytest tests/ -q` | 1738 passed, 3 skipped, 1 warning | ~15 s |
| `python tests/run_tests.py` | 213 passed, 0 failed | ~2 s |

The 3 skips come from `tests/test_help.py`, which walks every file under
`modules/` and skips those defining no `BotModule` subclass. The 1 local
warning is `PytestConfigWarning: Unknown config option: asyncio_mode`, emitted
when `pytest-asyncio` is not installed; the suite passes without it because no
test is written as `async def test_`, per the async convention below.

Run the suite **serially**. There is no `pytest-xdist` in the dev extras and
the suite is not written for parallel workers: `tests/test_config.py`
re-imports modules after `chdir`, several files mutate `sys.argv` /
`os.environ` / module singletons, and `tests/test_metrics.py` binds live
sockets.

A local pass on one interpreter does not prove the Windows or Python 3.10 legs.
CI runs the matrix; see [CI gates](#ci-gates).

## Why there are two runners

`tests/run_tests.py` is not a pytest-free convenience subset. It is the sole
home of a set of checks that exist nowhere else, and deleting it would drop
real coverage:

- `internets.py` helper tests: `ChannelSet`, `_backoff`, nick-change tracking,
  `is_admin` fail-closed branches, inbound redaction. `internets.py` has no
  `tests/test_*.py` file.
- The source-grep sanitizer completeness gate: `security: modules that emit
  upstream/user text route it through a sanitizer` (`tests/run_tests.py:234`)
  fails when a security-relevant module drops `modules.base.strip_ctrl`.
- The dependency-floor gate: `DEPS: pyproject extras never sit below the
  requirements.txt security floors` (`tests/run_tests.py:2598`).
- The version-consistency gates: `internets.__version__` against
  `pyproject.toml` and against every hand-written version literal in the docs
  (`tests/run_tests.py:2639`, `:2684`).
- Async-architecture contract checks (provider methods are coroutines,
  `weather_providers.get_weather` / `get_forecast` are async).

Mechanically, `run_tests.py` is a `@test("name")` decorator that registers and
immediately executes each function at definition time, tallies pass/fail,
prints ASCII-safe `[PASS]` / `[FAIL]` markers (Windows cp1252 consoles), and
exits 1 on any failure. It needs nothing beyond the standard library plus the
bot's own imports.

The trap: the file is named `run_tests.py`, not `test_*.py`, so pytest's
default collection never picks it up. Running only `pytest` silently skips
every check above.

## Harness conventions

### conftest.py

`tests/conftest.py` is three lines: it inserts the repo root into `sys.path` so
`import protocol`, `import store`, and `from modules import ...` resolve
without installing the package. It holds no fixtures.
`tests/run_tests.py` performs the same insertion itself.

### The argv-pinning convention for config imports

`config.py` parses `sys.argv` at import time. Under pytest, `sys.argv` holds
pytest's own arguments (test paths, `-q`), which `config.py`'s CLI parser
rejects or misinterprets. Every test module whose import chain reaches
`config.py` therefore pins argv around the import:

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

If a new test imports anything that transitively reaches `config.py`, pin argv
the same way or the whole file fails at collection.

### Async convention: no `async def test_`

`pytest-asyncio` is declared in the `dev` extra and `pyproject.toml` sets
`asyncio_mode = "auto"`, but the suite does not depend on the plugin. Without
it installed, an `async def test_` function is collected, reported as passed,
and never executed: a green test that proves nothing.

Every async path in this repo is therefore driven from an ordinary sync test:

```python
def test_dispatch_falls_through():
    result = asyncio.run(dispatch.get_weather(40.7, -74.0, "NYC"))
    assert result is not None
```

`tests/test_sender.py` uses `loop.run_until_complete(...)` for the same reason.

```{note}
The `asyncio_mode = "auto"` setting plus the `pytest-asyncio>=0.23` dev extra
contradict this convention. CI installs `.[dev]`, so an `async def test_`
added under that assumption would no-op locally and actually run in CI: two
environments with different test populations. Recorded as a finding in
[internals/tests.md](internals/tests.md).
```

## Fixtures and doubles

There is no shared fixture library. Each file builds its own doubles. Four
patterns recur.

| Pattern | Where | What it does |
| --- | --- | --- |
| `FakeBot` | 7 independent definitions | Hand-built bot stand-in exposing only what the code under test touches |
| Transport stub | Most module tests | `monkeypatch.setattr(mod, "fetch_json", ...)` with canned responses |
| Determinism stub | netsafe, probe, process_lock, hashpw | `getaddrinfo`, `os.kill`, `getpass` replaced |
| Real filesystem | store, audit_log, process_lock, secret_store | `tmp_path` against the real read/write code |

### FakeBot

A `FakeBot` exposes only the attributes the code under test reaches: `cfg`,
`preply` / `privmsg` / `notice` sinks that append to a list, `loc_get`,
`rate_limited`. It is declared independently in `tests/test_admin_cmds.py`,
`test_satpass.py`, `test_scinews.py`, `test_dnsutils.py`, `test_ipintel.py`,
`test_weather_flags.py`, and twice in `tests/run_tests.py`.

The `test_admin_cmds.py` variant is the deepest and the one to copy for
handler-level work: it subclasses the real `AdminCommandsMixin` so the actual
`cmd_*` coroutines run, and supplies `FakeModule` / `FakeStore` / `FakeSender`
around them. Only true externals are stubbed (the audit-log singleton is
redirected to a temp file, password verification is monkeypatched so no hash
backend or real config is needed).

The duplication is a deliberate-looking trade-off: each fake is minimal and
local, but a change to the bot interface must be chased through all seven
copies, and nothing checks that a fake stays faithful to `IRCBot`.

### Transport stubs

No test hits the real network. The dominant pattern is to patch the
`fetch_json` name **as imported into the module under test**, not
`modules.base.fetch_json`:

```python
def test_pkginfo_happy_path(monkeypatch):
    monkeypatch.setattr(pkginfo, "fetch_json", lambda *a, **k: CANNED)
    assert pkginfo._pypi_sync("requests", ua="x").startswith("requests")
```

Patching the source module instead of the importing module is the common
mistake: the module under test holds its own reference from import time, so
the patch has no effect and the test quietly hits the network.

### Real-filesystem tests

`store.py`, `audit_log.py`, `process_lock.py`, and `secret_store.py` are tested
against real files in `tmp_path` / `TemporaryDirectory`, so the actual
read/write/permission code runs. This follows the project's stated policy that
mocks hide integration bugs on resolution chains, config propagation, security
boundaries, and I/O. `tests/test_secret_store.py` monkeypatches `SECRETS_FILE`
to a temp path so it never touches the real `config.ini`.

## Writing a test for a new module

A new file under `modules/` is picked up by three suite-wide gates whether or
not you write a dedicated test file. Satisfy them first.

1. **`.help` regression gate** (`tests/test_help.py`). Every primary command
   must appear in `help_lines()`, every line must stay inside the 512-byte IRC
   limit, and alias separators must be normalized (`bofh/.excuse`, written via
   `modules.base.help_row`). The gate instantiates every `BotModule` subclass
   without running `__init__`, so no network, keys, or config are needed.
2. **Sanitizer completeness gate** (`tests/run_tests.py:234`). A module that
   emits upstream or user-derived text must reference
   `modules.base.strip_ctrl`. Note this is a source-text grep: it proves a call
   site exists, not that the emitted string passes through it.
3. **`COMMANDS` contract**. `BotModule.__init_subclass__` raises `TypeError` at
   import if a command maps to a missing method or to a non-`async def` one, so
   a typo fails the whole suite at collection rather than at first use.

Then write the behavioral test:

```python
# tests/test_mymod.py
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # conftest does this
from modules import mymod

def test_format_happy_path(monkeypatch):
    monkeypatch.setattr(mymod, "fetch_json", lambda *a, **k: {"value": 42})
    out = mymod._fetch_sync("thing", ua="test-agent")
    assert "42" in out

def test_missing_key_is_refused():
    m = mymod.MyModule.__new__(mymod.MyModule)   # skip __init__
    m._key = ""
    assert m.is_configured() is False
```

Conventions to follow, all of them load-bearing:

- Assert **behavior**, not the current implementation. A test that restates the
  code blocks refactors and catches nothing.
- Prove the fix, not the code: a regression test should fail without the fix
  and pass with it. Existing regression tests carry the incident in their
  docstring (`SEC-*` / `BUG-*` labels in `run_tests.py`).
- Exercise the error branches too: timeout, non-200, malformed payload, missing
  key. Several modules' only real defects live there.
- Never write `async def test_`. Use `asyncio.run(...)`.
- Reconcile the counts after adding a file. The suite totals must move by
  exactly what you added; a new file at the wrong path or filtered by a name
  pattern leaves the suite green and the invariant unguarded.

## Writing a test for a new weather provider

Provider tests operate at the parser level with `fetch()` mocked. The models to
copy are `tests/test_new_weather_capabilities.py` (capability auto-discovery
plus mocked `fetch()` for the non-trivial parsers) and
`tests/test_airnow_purpleair.py` (AQI math, sensor selection, and the
no-coverage raise that the dispatcher's fallback contract depends on).

Pin at least:

- **Registration and capability discovery.** The provider appears in
  `provider_capabilities()` with the capabilities its module files implement.
- **Parse of a canned upstream payload** into the normalized dataclass
  (`WeatherResult`, `AirQualityResult`, ...), including units. Two of the
  regression pins in `tests/test_provider_fixes.py` exist because a parser
  stored raw m/s in a km/h field and because another returned a hollow
  all-`None` result instead of failing over.
- **The no-coverage / no-data path raises** rather than returning an empty
  result. The dispatcher fails over on the exception; a hollow result
  terminates the chain with nothing.
- **Key gating.** A provider whose factory needs a credential must return
  `None` from the factory when the key is absent (see
  `weather_providers/__init__.py` `_f_*` factories).

Coverage of providers is uneven today: airnow, purpleair, metno and the 14
newer-capability providers have parser-level tests; accuweather,
openweathermap, pirateweather and meteomatics appear only in
registration/priority/reliability assertions.

## Coverage gate

```bash
coverage run -m pytest tests/ --strict-markers
coverage report --fail-under=75
```

The gate is **core-only**, not repo-wide. `pyproject.toml
[tool.coverage.run]` omits:

| Omitted | Reason recorded in pyproject.toml |
| --- | --- |
| `modules/*` | Keeps coverage core-only |
| `weather_providers/*` | Keeps coverage core-only |
| `internets.py` | Async IRC event loop; needs a live/mock connection |
| `console.py` | Stdin console loop; same |
| `tests/*`, `build/*`, `dist/*`, `.venv/*` | Not product code |

Branch coverage is on (`branch = true`), `fail_under = 75`, and the standard
`exclude_lines` pragmas apply (`pragma: no cover`,
`raise NotImplementedError`, `if __name__ == .__main__.:`, `if TYPE_CHECKING:`).

Measured on this checkout (2026-08-15): **86%** core coverage over 2568
statements and 822 branches, against the 75% floor. The two lowest files are
`store.py` (63%) and `secret_store.py` (66%), both of which have large CLI /
maintenance-command surfaces the unit tests do not drive.

Do not read the headline percentage as repo-wide coverage. The SSRF guard,
dispatch, and parsing bulk of the codebase sit inside the omitted paths, and so
do both entry points, which is why the missing end-to-end dispatch test cannot
show up as a coverage drop.

## Linting and static analysis

There is **no style linter in CI**. The `lint` job is a syntax check only:
`python -m py_compile` over a hand-enumerated list of the 13 top-level modules,
then `find weather_providers -name '*.py' -exec python -m py_compile {} +`,
then a loop over `modules/*.py`.

```{warning}
The top-level file list in `.github/workflows/tests.yml` is maintained by hand.
A new top-level module added without editing that list is silently unlinted.
This is the same maintenance shape whose failure (an omission from
`pyproject.toml [tool.setuptools] py-modules`) shipped uninstallable wheels in
v3.0.0 and v4.0.0.
```

### bandit

`.github/workflows/security.yml` runs three passes:

1. Informational, MEDIUM and above (`-ll --exit-zero`) - never gates.
2. Gating, HIGH severity plus HIGH confidence (`-iii -ll`) - fails CI.
3. SARIF upload with `if: always()`, so findings reach the Security tab even
   when the gate fails.

`tests/`, venvs, and build directories are excluded, mirrored in
`pyproject.toml [tool.bandit]`.

### CodeQL

`.github/workflows/codeql.yml` runs the Python pack with
`queries: security-extended` (wider than the default suite) on push, PR, and a
weekly Tuesday 06:00 UTC cron. The division of labor is recorded in the
workflow header: bandit is cheap pattern-matching SAST, CodeQL catches
taint/dataflow issues across function boundaries. Both upload SARIF to the same
Security tab.

```{note}
Triage state: the `security-extended` rollout took the alert count from 123 to
4. The 4 remaining open `py/overly-permissive-file` alerts have been triaged
benign and are **deliberately left open**. They are not outstanding work; do
not re-fix them.
```

## Dependency policy and pip-audit

Two files, two jobs:

- `requirements.txt` - human-maintained **floors**, each annotated with the CVE
  or advisory that set it. No upper bounds; the cross-platform CI matrix is the
  stated tripwire for a breaking upstream release.
- `requirements.lock` - machine-generated exact pins with `--hash=sha256:`
  entries for every package and transitive dependency. CI installs from it with
  `--require-hashes`, which defends against PyPI account takeover and
  dependency confusion: a poisoned wheel with the right name but the wrong hash
  fails to install.

Regenerate with `scripts/regen-lockfile.sh`, and commit `requirements.txt` and
`requirements.lock` together. The script's contract is that the lock must be
resolved on **Python 3.10 specifically**, the lowest supported version, because
conditional transitive dependencies gated `python_version < "3.11"` are
otherwise silently omitted. The script fails loudly when no 3.10 interpreter is
found.

`pip-audit -r requirements.lock --strict` runs in `security.yml` on push, PR,
and a weekly Monday 06:00 UTC cron, so a new CVE against a pinned dependency
surfaces without a code change. One documented suppression:
`--ignore-vuln PYSEC-2025-183` (PyJWT), disputed upstream and inapplicable here
because the key size is Apple-issued for WeatherKit ES256 signing.

The audit covers `requirements.lock` only. `pyproject.toml` extras floors are
covered by no automated CI check; the `DEPS:` gate in `tests/run_tests.py`
(which asserts the extras never sit below the `requirements.txt` floors) is the
only mechanical guard, and it compares against the floors file rather than
against a vulnerability database.

## CI gates

`.github/workflows/tests.yml` has four jobs, all triggered on push and pull
request against `main`, with least-privilege `permissions: contents: read`.

| Job | Runner | What it gates |
| --- | --- | --- |
| `test` | 15-cell matrix | Both suites, per platform and interpreter |
| `coverage` | ubuntu / 3.12, `needs: test` | Core-only 75% floor, uploads `coverage.xml` (14-day retention) |
| `lint` | ubuntu / 3.12 | `py_compile` syntax over all Python |
| `package` | ubuntu / 3.12 | `scripts/verify_install.sh` wheel install gate |

The `test` matrix is `{ubuntu, macos, windows}-latest` x Python
`3.10, 3.11, 3.12, 3.13, 3.14`, `fail-fast: false`. Each cell installs
`requirements.lock --require-hashes` then `pip install -e ".[dev]"` (dev extras
stay unhashed because they only run on CI runners and never reach production),
stages `config.ini`, then runs `python tests/run_tests.py` followed by
`pytest tests/ -v --tb=short --strict-markers`. `PYTHONIOENCODING: utf-8` is
exported workflow-wide for Windows cp1252 consoles.

The `package` job builds an sdist and wheel, installs the wheel into a
throwaway venv, verifies every installed file against the SHA-256 hashes in the
wheel's `RECORD`, then smoke-tests `import internets`, the version string, and
console entry-point resolution. It exists because wheels missing `audit_log`,
`process_lock`, and `metrics` shipped uninstallable in v3.0.0 and v4.0.0:
`CONTRIBUTING.md` named the gate but nothing ran it.

All third-party actions are pinned to full commit SHAs with a version comment;
Dependabot's github-actions ecosystem keeps them current.

```{warning}
**Known defect: CI Tests is RED on `main`.**

`requirements.lock` was regenerated on Python 3.14, violating the
resolve-on-3.10 contract in `scripts/regen-lockfile.sh` (the lock header reads
`autogenerated by pip-compile with Python 3.14`). The marker-gated
`typing_extensions>=4.4` transitive, pulled by `aiohttp==3.14.3` under
`python_version < "3.13"`, is therefore absent from the lock, so every Python
< 3.13 leg fails `pip install -r requirements.lock --require-hashes` with
"all requirements must have their versions pinned".

Verified live on 2026-08-15: the last three pushes to `main` (workflow runs
31669351519, 31848012132, 31848249036) all failed. The fix is to regenerate the
lock on a 3.10 interpreter per the script. This is a dependency-surface change
and an owner decision, not documentation work; see [known issues](known-issues.md).

Secondary, same root cause: on the Windows legs the three pip commands share
one `run:` block and pwsh does not stop on a failing command, so the lockfile
install failure is not reported as the failure. The job proceeds to
`pip install -e ".[dev]"` and fails much later inside pytest with
`ModuleNotFoundError: No module named 'bcrypt'`. The Linux and macOS legs fail
fast because bash runs with `-e`.

Consequence for a contributor: a red `Tests` check on a PR against `main` today
is expected and pre-existing. Confirm your change is not the cause by running
both suites locally before assuming CI is telling you something about your
work.
```

## What a green run does not prove

The gaps below are structural and known. They are recorded here so a green
suite is not read as more evidence than it is; per-file detail lives in the
Findings sections of the docs under `docs/internals/`.

- **No end-to-end dispatch test.** Nothing drives a raw inbound IRC line
  through `internets.py` parsing, command lookup, a module handler, and
  `sender.py` output as a single path. `internets.py` is tested only at the
  helper level (in `run_tests.py`), `sender.py` only at the queue level, and
  modules only at the handler/helper level. The wiring between them is
  exercised by no test, and because both entry points are omitted from the
  coverage gate, the gap is invisible to the coverage number by construction.
- **`console.py` is untested.** The stdin admin console has no test file at
  all. See [internals/console.md](internals/console.md).
- **44 modules have no behavioral tests** in either runner, out of the 75 files
  under `modules/`:
  advice, apod, bofh, bored, catfact, chuck, cocktail, cowsay, dadjoke,
  devutils, dictionary, dnd, example, fact, fml, fx, games, health, hn,
  httpcode, idlerpg, imdb, ipinfo, iss, lastfm, linktitle, mtg, netcalc, notes,
  poke, privacy, qr, recipe, reddit, remind, search, seen, spacex, steam, tell,
  twitch, urls, xkcd, youtube. Most are thin fetch-and-format wrappers, but the
  list includes stateful modules (notes, remind, tell, seen, idlerpg) and
  input-handling ones with a security surface (ipinfo, linktitle, urls).
  `search`, `seen`, `tell`, and `remind` do appear in `run_tests.py`, but only
  in the source-grep sanitizer gate, which checks that a call site exists, not
  that behavior is correct.
- **The sanitizer gate is text-matching.** A module that imports `strip_ctrl`
  without calling it on the string it emits passes.
- **Weather providers are unevenly covered**; see
  [Writing a test for a new weather provider](#writing-a-test-for-a-new-weather-provider).
- **Misleading name:** `tests/test_dispatcher.py` covers
  `weather_providers/_dispatch.py`, not bot command dispatch.
  `tests/test_stocks.py` (31 lines) covers two number-formatting helpers only,
  not the stocks fetch path, which is where the verified key-leak defect lives
  (see [integrations.md](integrations.md)).

## Related documentation

- [internals/tests.md](internals/tests.md) - per-file map of what each test
  pins, plus the full findings list.
- [internals/ci-and-packaging.md](internals/ci-and-packaging.md) - workflow,
  packaging, and `scripts/` reference.
- [writing-modules.md](writing-modules.md) - the module authoring contract the
  suite-wide gates enforce.
- [integrations.md](integrations.md) - the external services module tests stub
  out.
