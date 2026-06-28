# Contributing

Developer guide for the Internets IRC bot (Python 3.14, asyncio, hot-reloadable
modules). Read this alongside `README.md` (architecture), `SECURITY.md`
(vulnerability reporting), and `CODE_OF_CONDUCT.md` (the Contributor Covenant;
participation in this project means agreeing to it). Everything below is
enforced by CI in
`.github/workflows/` - if it is not green there, it does not merge.

## Local setup

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # full runtime deps (requests, aiohttp, argon2/bcrypt, PyJWT+cryptography, defusedxml)
pip install -e ".[dev]"              # pytest, pytest-asyncio, pytest-cov, coverage, bandit[sarif], pip-audit, build
```

`requirements.txt` carries the runtime deps with security-floor lower bounds and
no upper bounds (see the header in that file for the version policy). The
hash-pinned `requirements.lock` is what CI installs (`--require-hashes`); see
"Regenerating the lockfile" below. Dev/test/CI tools live in
`pyproject.toml` under `[project.optional-dependencies] dev`, not in
`requirements.txt`.

### config.ini is mandatory before any test run

`config.ini` is gitignored (it holds the `[secrets]` section in real
deployments) and `config.py` reads it at **import time**. The test harness
imports `internets`, which imports `config`, so with no `config.ini` present the
suite aborts at the first `from config import ...` before a single test runs.
Stage the credential-free template first:

```bash
cp config.ini.example config.ini
```

CI does this in an explicit "Stage config.ini for tests" step in every job that
runs tests. The example template carries no credentials, so a checked-out tree
plus this copy is enough to load the bot for testing. Never commit a populated
`config.ini` (gitleaks will catch it; see below).

## Running tests

Two suites. **Both must be green before you commit**, and CI runs both.

```bash
python tests/run_tests.py            # standalone @test harness, stdlib only
pytest tests/ -v --strict-markers    # full pytest suite
```

### `tests/run_tests.py` - standalone harness

A self-contained runner with no pytest dependency. It defines a `test(name)`
decorator that registers and immediately runs each test function, tallies
pass/fail, prints `[PASS]`/`[FAIL]` markers (ASCII so Windows cp1252 consoles do
not crash), and `sys.exit(1)` if anything failed. It inserts the project root on
`sys.path` itself, so it runs from a bare checkout once `config.ini` exists. Use
it as the fast smoke gate: it covers `protocol`, `store`/`RateLimiter`,
`sender`, `hashpw`, `secret_store`, the `internets` core helpers (admin-auth
fail-closed, regexes, backoff), the weather provider/dispatch layer, and a block
of security-regression assertions (line-length cap, target-injection rejection,
TLS-floor inspection, log sanitization).

### `pytest tests/` - full suite

Per-module test files (`tests/test_*.py`). `pyproject.toml [tool.pytest.ini_options]`
sets `testpaths = ["tests"]`, `asyncio_mode = "auto"` (async tests need no
`@pytest.mark.asyncio`), and `addopts = "--strict-markers"` (an unknown
`@pytest.mark.<name>` is an error, not a silent skip - keep it on locally so
marker typos surface before CI). `tests/conftest.py` puts the project root on
`sys.path`.

The two suites overlap deliberately; the standalone harness is the
dependency-free smoke check, pytest is the full matrix. Run both.

### Coverage gate (core-only)

```bash
coverage run -m pytest tests/ --strict-markers
coverage report --fail-under=75
```

The 75% gate is **core-only, not repo-wide**. `pyproject.toml
[tool.coverage.run] omit` excludes `modules/*` and `weather_providers/*` (the
SSRF/dispatch/parsing bulk), so the reported percentage measures only the
top-level orchestration modules. Do not read the headline number as whole-repo
coverage. The CI `coverage` job depends on `test` passing first.

## Isolated-copy install gate

`scripts/verify_install.sh` is the supply-chain smoke test. It builds an sdist +
wheel via `python -m build`, installs the wheel into a throw-away venv, verifies
every installed file's SHA-256 against the wheel's `RECORD` metadata (catches
tampering or a broken extractor), then smoke-tests `import internets`, the
`__version__` string, and that the `internets` console entry point resolves. Run
it before any change that touches packaging (`pyproject.toml` `py-modules` /
`packages.find`, the entry point, or module layout):

```bash
./scripts/verify_install.sh          # exit 0 == verified
```

`scripts/sbom.sh` generates a software bill of materials if you need one.

## Code style

- Target Python 3.10+ (CI matrix is 3.10 through 3.14). Use `from __future__
  import annotations` and PEP 604 unions (`X | Y`, never `Union[X, Y]`).
- Async-first: every command handler is a coroutine. `BotModule.__init_subclass__`
  enforces this at class-definition time - a `COMMANDS` entry pointing at a
  missing or non-`async def` method raises `TypeError` at import, not at first
  use. Blocking I/O (HTTP, disk, password hashing) must run via
  `await asyncio.to_thread(...)` inside the handler so the event loop stays free.
- Shared mutable state is protected by a `threading.Lock`; follow the pattern in
  `store.py` / `sender.py`.
- Never read credentials directly from `cfg[...]`. Route every API key, NickServ
  password, etc. through `modules.base.cred(cfg, name, section, key)` so the
  secret store wins over the config file.
- Never log credential values. `sender.py` already redacts outbound `PASS`,
  `NS IDENTIFY`, `OPER`, and `AUTHENTICATE`; module code must hold the same
  discipline. Modules that splice third-party or user text into bot-attributed
  IRC lines must run it through `modules.base.strip_ctrl` (strips C0/CRLF/NUL,
  truncates) - the standalone suite has a completeness test that fails if a
  security-relevant module drops the sanitizer.

## Module authoring checklist

Read `modules/base.py` (the `BotModule` interface and its docstrings) and the
"Architecture" section of `README.md` before writing a module. Every new file
under `modules/`:

- Starts from `modules/example.py`, the copy-and-fill skeleton (copy it, rename
  the class + logger, fill `COMMANDS` + the `cmd_*` coroutine(s)) - the best
  starting point.
- Subclasses `modules.base.BotModule`.
- Defines `COMMANDS: dict[str, str]` mapping each command word to an `async def`
  method name (validated at import by `__init_subclass__`).
- Exposes a top-level `setup(bot) -> ModuleClass` function returning the
  instance (see `modules/calc.py:151`, `modules/weather.py:858`).
- Overrides `is_configured()` to return `False` until its API key is present, if
  it needs one - `.help` hides modules where this returns `False` so users only
  see commands they can run. Dispatch still works, so an admin can `.load` it and
  add a key later.
- Overrides `forget(nick)` if it persists user PII (mutate the store, persist,
  return the count removed) so `.forgetme` covers it.
- Adds any required credential name to `secret_store.KNOWN_SECRETS`, and its
  `config.ini` location to `CONFIG_LOCATIONS` if the migrate command needs to
  find it.
- Uses `modules.base.fetch_json` (or an equivalent capped stream) for outbound
  HTTP. Never call bare `r.json()` / unbounded `r.text` - all outbound HTTP is
  response-size-capped.

Optional sync hooks: `on_load()`, `on_unload()`, `on_raw(line)` (must be fast
and sync - it runs for every incoming IRC line), `help_lines(prefix)`.

## CI workflows (`.github/workflows/`)

All three trigger on push and PR to `main`; `security.yml` and `codeql.yml` also
run on a weekly cron. All declare least-privilege `permissions: contents: read`
at the top, elevating per-job only where a SARIF upload needs
`security-events: write`. Action refs are pinned to commit SHAs.

- **`tests.yml`** - three jobs.
  - `test`: full matrix, `os = {ubuntu, macos, windows}` x `python =
    {3.10, 3.11, 3.12, 3.13, 3.14}` (15 cells, `fail-fast: false`). Installs
    `requirements.lock --require-hashes` then `-e ".[dev]"`, stages
    `config.ini`, runs `python tests/run_tests.py` then `pytest`.
  - `coverage`: needs `test`; runs the core-only 75% gate (see above) and
    uploads `coverage.xml`.
  - `lint`: `python -m py_compile` over every top-level module (plus
    `audit_log`, `process_lock`, `metrics`), all of `weather_providers/`, and
    each `modules/*.py`. There is no formatter/linter gate beyond
    syntax-compile - touched code must be `py_compile`-clean.
- **`security.yml`** - three jobs.
  - `bandit`: informational MEDIUM+ pass (`-ll --exit-zero`), then a **gating**
    pass that fails CI on any MEDIUM-or-higher severity + HIGH-confidence finding
    (`-iii` = HIGH confidence floor, `-ll` = MEDIUM+ severity floor), then
    uploads SARIF to the Security tab. `[tool.bandit]` in
    `pyproject.toml` excludes
    `tests`/`.venv`/`build`/`dist`/`.git`/`__pycache__`.
  - `pip-audit`: scans `requirements.lock --strict` (any CVE fails the job).
    Scanned against the lockfile, not the editable install (the local
    `internets-irc` has no PyPI entry). One documented exception:
    `--ignore-vuln PYSEC-2025-183` (disputed pyjwt finding about
    application-chosen key size, not the library; re-evaluate if a fix ships).
  - `gitleaks`: full-history (`fetch-depth: 0`) secret scan.
- **`codeql.yml`** - GitHub semantic SAST (`security-and-quality` queries,
  Python). Catches taint/dataflow bugs bandit's pattern matching misses; both
  feed the same Security tab and dedupe.

## Regenerating the lockfile

`requirements.lock` is hash-pinned and CI installs it with `--require-hashes`, so
it must stay in sync with `requirements.txt`. Regenerate it whenever
`requirements.txt` changes (Dependabot bump, manual edit):

```bash
scripts/regen-lockfile.sh
```

The script resolves with `pip-compile --generate-hashes --strip-extras
--no-emit-options` (from `pip-tools`) inside an ephemeral venv. It **requires
Python 3.10 specifically** on `PATH` and fails loudly otherwise: the lock must be
resolved on the lowest supported Python so conditional transitive deps gated
`python_version < "3.11"` (e.g. `async-timeout`) are captured. A lock generated
on 3.14 silently omits them and breaks CI's 3.10 jobs. The lockfile header records
the exact `pip-compile` invocation. Commit `requirements.txt` and
`requirements.lock` together in the same commit.

## Pull requests

- Open against `main`.
- One-line summary; link any related issue with `Fixes #N`.
- Both test suites and all three CI workflows (`tests`, `security`, `codeql`)
  must be green.
- Touched code must be `py_compile`-clean (the `lint` job enforces it).
- Land work in small, independently-verifiable, bisectable commits. Keep a
  security-sensitive change in its own PR rather than coupling it to unrelated
  work.
- Bump `version` in `pyproject.toml` and `internets.__version__` together when
  releasing - the standalone suite asserts they match.

## Reporting bugs or security issues

- General bugs / feature requests: open an issue using the templates in
  `.github/ISSUE_TEMPLATE/`.
- Security vulnerabilities: follow `SECURITY.md`. Use GitHub Private
  Vulnerability Reporting (the repo Security tab). Do **not** open a public
  issue for a vulnerability.
