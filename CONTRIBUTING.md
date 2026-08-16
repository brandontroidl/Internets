# Contributing

Developer guide for the Internets IRC bot: Python 3.10+, asyncio, hot-reloadable
command modules. Read it alongside `README.md` for the system overview,
`SECURITY.md` for vulnerability reporting, and `CODE_OF_CONDUCT.md` (the
Contributor Covenant; participating in this project means agreeing to it).

Everything described here is enforced by a gate. If it is not green in
`.github/workflows/`, it does not merge. Where a topic has a full treatment
elsewhere in the documentation set, this page states the rule and links down
rather than repeating it.

## Local setup

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # full runtime deps
pip install -e ".[dev]"              # test and CI tooling
cp config.ini.example config.ini     # mandatory; see below
```

Three dependency surfaces, each with a different job:

| File | Contents | Who installs it |
|---|---|---|
| `requirements.txt` | runtime deps, security floors, no upper bounds | you, locally |
| `requirements.lock` | the same set, hash-pinned | CI, with `--require-hashes` |
| `pyproject.toml` `[project.optional-dependencies]` | extras + the `dev` tooling | you and CI |

The `dev` extra is `pytest>=8.3`, `pytest-asyncio>=0.23`, `pytest-cov>=5.0`,
`coverage>=7.6`, `bandit[sarif]>=1.7.10`, `pip-audit>=2.7.3`, `build>=1.2`.
The runtime extras (`async`, `bcrypt`, `argon2`, `weatherkit`, `xml`, and the
aggregate `all`) exist so a packaged install can pull only what it needs; their
floors must match `requirements.txt`, and `tests/run_tests.py` has a `DEPS`
check that fails if an extra ever sits below the `requirements.txt` floor.

Nothing in `pyproject.toml` installs the documentation toolchain. See
[Building the documentation](#building-the-documentation) for that list.

### config.ini must exist before any test run

`config.ini` is gitignored (it holds `[secrets]` in a real deployment) and
`config.py` reads it at **import time**. The test harness imports `internets`,
which imports `config`, so with no `config.ini` present the suite aborts at the
first `from config import ...` before a single test runs. Copy the
credential-free template:

```bash
cp config.ini.example config.ini
```

Every CI job that runs tests does this in an explicit "Stage config.ini for
tests" step. Never commit a populated `config.ini`; the `gitleaks` job scans
full history and will catch it.

## Running the tests

Two runners, neither a superset of the other. Both must be green before you
commit, and CI runs both.

```bash
python -m pytest tests/ -q      # 1738 passed, 3 skipped
python tests/run_tests.py       # 213 passed
```

Those counts are the current expected totals. If your run differs, find out why
before assuming your change is unrelated.

Prefer `python -m pytest` over the bare `pytest` console script. The script
carries a shebang pinning whichever interpreter installed it, which is not
necessarily the interpreter holding this project's dependencies; a mismatch
shows up as a wall of `ModuleNotFoundError` at collection rather than as an
obvious environment problem.

`tests/run_tests.py` is a self-contained harness with no pytest dependency. It
is named `run_tests.py`, not `test_*.py`, so pytest's default collection never
picks it up. Beyond ordinary unit coverage it holds the repo's completeness
gates: the sanitizer gate over the security-relevant modules, the dependency
floor check, and the version-literal check described under
[Cutting a release](#cutting-a-release).

Full detail on the harness, the fixtures, and what a green run does not prove:
`docs/testing.md`.

### Pin argv when a test imports config

`config.py` parses `sys.argv` at import time. Under pytest, `sys.argv` holds
pytest's own arguments, which that parser rejects. Any test file whose import
chain reaches `config.py` must pin argv around the import:

```python
_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import admin_cmds            # or config, or botlog
sys.argv = _SAVED_ARGV
```

`tests/test_config.py`, `tests/test_botlog.py`, and `tests/test_admin_cmds.py`
do this. `scripts/gen-command-reference.py` does it too, for the same reason.
Omit it and the whole file fails at collection, not at the test.

### Do not write `async def test_` functions

Drive the event loop explicitly instead:

```python
def test_dispatch_falls_through():
    out = asyncio.run(d.dispatch("current", 0.0, 0.0, "x"))
    assert out is None
```

`pytest-asyncio` is in the `dev` extra and `pyproject.toml` sets
`asyncio_mode = "auto"`, but the suite does not depend on the plugin and it is
frequently absent locally. Without it, an `async def test_` is collected,
reported as passed, and never executed: a green test that proves nothing.
`tests/test_sender.py` uses `loop.run_until_complete(...)` for the same reason.

The `PytestConfigWarning: Unknown config option: asyncio_mode` you see without
the plugin is expected and is not a failure.

### Coverage gate (core-only)

```bash
coverage run -m pytest tests/ --strict-markers
coverage report --fail-under=75
```

The 75% gate is core-only, not repo-wide. `pyproject.toml`
`[tool.coverage.run] omit` excludes `modules/*`, `weather_providers/*`, and the
two integration-level entry points `internets.py` and `console.py`, so the
number measures the unit-testable top-level core and nothing else. Do not read
the headline percentage as whole-repo coverage, and note that excluding both
entry points is why no coverage number can reveal the absence of an end-to-end
dispatch test (see `docs/known-issues.md`, "Test gaps worth closing first").

### Packaging gate

`scripts/verify_install.sh` builds an sdist and a wheel with `python -m build`,
installs the wheel into a throw-away venv, checks every installed file's
SHA-256 against the wheel's `RECORD`, then verifies from outside the repo that
each declared top-level module imports from the wheel and that the `internets`
console entry point resolves.

```bash
./scripts/verify_install.sh          # exit 0 == verified
```

Run it after any change to `pyproject.toml` `py-modules`, `packages.find`, the
entry point, or the module layout. It is not optional diligence: a wheel
missing `audit_log`, `process_lock`, and `metrics` shipped in v3.0.0 and v4.0.0
because this script existed and nothing ran it. CI's `package` job now runs it
on every push and pull request.

`scripts/sbom.sh` generates a software bill of materials if you need one.

## Coding conventions

These are the conventions actually in force in the tree, each with the
mechanism that enforces it.

**Async handlers, blocking work off the loop.** Every command handler is a
coroutine. `modules/base.py - BotModule.__init_subclass__()` validates at
class-definition time that each `COMMANDS` entry names an existing `async def`
method, so a typo raises `TypeError` at import rather than at first use. HTTP,
disk, and password hashing run through `await asyncio.to_thread(...)`. The
`_CMD_TIMEOUT` of 60 seconds in `internets.py - IRCBot` cancels an await; it
cannot interrupt a synchronous call already running on the loop, which is
exactly how `modules/mathx.py - MathxModule.cmd_isprime()` became a whole-bot
denial of service (known issue 3).

**Size-capped outbound HTTP.** Use `modules/base.py - fetch_json()`, which
streams the body and raises `ResponseTooLarge` past its cap before decoding or
parsing. Never a bare `r.json()` or an unbounded `r.text`. A provider that
needs a different transport writes its own explicit stream-and-cap loop;
`weather_providers/pirateweather/_codes.py - safe_get_json()` is the model,
and it also shows the correct way to raise without embedding a keyed URL in
the exception text.

**Lazy imports where a dependency is optional.** `modules/base.py -
fetch_json()` imports `requests` inside the function so `base.py` stays
importable in an environment without it, which is what lets the test suite load
the module layer. Follow the same shape when a top-level import would make an
otherwise-testable file unimportable, and keep the `# noqa: PLC0415` marker so
the intent is legible.

**`strip_ctrl` on anything third-party or user-supplied.** Text spliced into a
bot-attributed IRC line goes through `modules/base.py - strip_ctrl()`, which
strips C0 control characters, CR, LF, and NUL and truncates at 400 characters
by default. `tests/run_tests.py` holds a completeness gate over `search`,
`seen`, `tell`, `stocks`, `remind`, and `location` that fails if any of them
drops the sanitizer; `modules/weather.py` is allowed its equivalent
`_sanitize`.

**Credentials through `cred()`, never `cfg[...]`.** Every API key, NickServ
password, and similar value is read via `modules/base.py - cred()` so the
secret store wins over the config file. Add the canonical name to
`secret_store.py - KNOWN_SECRETS` and its config location to
`secret_store.py - CONFIG_LOCATIONS`; that makes it visible to `secret_store
list` and movable by `migrate`. It does **not** add it to log redaction:
`sender.py - redact_secrets()` matches on credential verbs
(`PASS`, `IDENTIFY`, `AUTHENTICATE`, `OPER`, and friends), not on a name
registry, and it applies to log output only, never to a composed `PRIVMSG`.

**Gate order: authorization, usage, cooldown, work.**

```python
async def cmd_mymod(self, nick, reply_to, arg):
    if not self.bot.is_admin(nick):             # 1. authorization, if any
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

Authorization first so an unauthorized user learns nothing and burns no quota.
Usage before the cooldown so a mistyped command is answered rather than
throttled. `rate_limited()` consumes the token when it returns `False`, so
calling it twice charges the user twice. Both orders exist in the tree today
and the difference is real; the comparison and the two exceptions are in
`docs/writing-modules.md`, section 8. Do not add a third pattern.

**Types and shared state.** Target 3.10: `from __future__ import annotations`
and PEP 604 unions (`X | Y`, never `Union[X, Y]`). Shared mutable state is
protected by a `threading.Lock`; follow `store.py` and `sender.py`. Note that
three sites currently mutate state outside their lock while a worker thread
serializes it (known issue 12) - copy the locked pattern, not those.

## Adding a module or a provider

Do not derive the contract from a neighbouring file. Both paths have a guide
written from the source:

- A new command module: `docs/writing-modules.md`. Start from
  `modules/example.py`, the copy-and-fill skeleton. In summary, the file
  subclasses `modules.base.BotModule`, declares
  `COMMANDS: dict[str, str]` mapping each command word to an `async def cmd_*`
  method name, exposes a top-level `setup(bot)` returning the instance
  (`internets.py - IRCBot.load_module()` refuses a module without it),
  overrides `is_configured()` if it needs a key, and overrides `forget(nick)`
  if it persists user data so `.forgetme` reaches it. New user-derived data
  also changes what `PRIVACY.md` describes; update it in the same PR.
- A new weather provider: `docs/writing-providers.md`. The dispatcher,
  ranking, and health/circuit-breaker behaviour are in `docs/providers.md`.
  Read known issue 2 before you rely on fallback: a provider that returns an
  empty result for most capabilities ends the chain instead of falling
  through.

A new module needs a test file. `docs/testing.md` has a worked template for
both cases.

## Documentation rules

The documentation set is verified, not merely built. Three gates apply.

**Cite by symbol, not by line number.** The symbol form is a backticked
`file.py - Class.method()`; the retired form is a backticked `file.py:123`. A
line number goes stale silently the moment anything above it moves: the cited
line still exists, it just describes something else, and no build step
notices. A single
session's source edits invalidated roughly 370 citations twice.

**`scripts/verify-doc-citations.py` must pass.** It parses each cited file's
AST and confirms the cited symbol exists, so it checks meaning rather than
arithmetic. It exits non-zero on a failed symbol, an out-of-range line, or a
missing file, and that count must stay at zero. Line-style citations are
reported as REVIEW rather than PASS by design: they are the style being
retired, and the remaining count should only ever go down.

```bash
scripts/verify-doc-citations.py              # report, exit 1 on failures
scripts/verify-doc-citations.py --summary    # counts only
```

`scripts/remap-doc-citations.py` is the mechanical helper for the line-number
citations that remain, not the primary tool. After an edit moves lines in a
cited file it rebuilds an old-to-new line map with `difflib` and rewrites the
numbers:

```bash
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py           # report
scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py --apply   # rewrite
```

Remapping keeps a citation pointing at the same text; it cannot tell you the
prose was already wrong, and `difflib` can mis-anchor when a block both moves
and changes in one edit. UNMAPPABLE citations point at lines that no longer
exist and need a human. Always run `verify-doc-citations.py` afterwards, and
prefer converting the citation to symbol style over renumbering it.

**Command lists are generated, not hand-maintained.**
`scripts/gen-command-reference.py` walks every module, instantiates each
`BotModule` subclass without running `__init__`, and emits the inventory from
actual registration. Any document that lists commands must pass the drift
check:

```bash
scripts/gen-command-reference.py                                   # emit Markdown
scripts/gen-command-reference.py --check docs/command-reference.md # exit 1 on drift
```

## Building the documentation

```bash
scripts/build-docs.sh           # HTML + PDF
scripts/build-docs.sh html      # HTML only
scripts/build-docs.sh pdf       # PDF only
```

Output lands in `docs/_build/`, which is gitignored. Both targets must build.

The toolchain is Sphinx with MyST for the Markdown sources, `sphinx-autoapi`,
`sphinx-rtd-theme`, `sphinx-copybutton`, `sphinx-design`, and `graphviz`. PDF
additionally needs a TeX Live with `xelatex` and `makeindex`; the script runs
explicit xelatex passes rather than `latexmk`. None of this is in the `dev`
extra, so install it separately.

Two things to know before you write:

- `sphinx-autoapi` publishes the API reference **from the source docstrings**.
  A module-level docstring is published documentation and is held to the same
  accuracy bar as these Markdown files.
- The build is deliberately not run with `-W`. A warning baseline is expected
  and benign, and every member of it names a path under `docs/autoapi/`:
  duplicate-object nags on re-exported attributes, and docutils formatting
  complaints about plain-text module docstrings. Do not chase those. Any
  warning naming a file outside `docs/autoapi/` is a real defect in a
  hand-written page, and it is yours if you edited that page. Compare the
  count before and after your change.

The PDF build has its own trap: `scripts/build-docs.sh` swallows xelatex output
and exits 0 even when content overflowed the printed page. Wide tables are the
usual cause, which is why the house style keeps tables at four columns or
fewer. If you add a wide table or a long unbroken URL, check
`docs/_build/latex/internets.log` for overfull box warnings rather than
trusting the script's exit code.

A new prose page goes under `docs/` and must be registered in the `toctree` in
`docs/index.md`, or Sphinx warns that the document is in no toctree.
`CONTRIBUTING.md`, `SECURITY.md`, and `CHANGELOG.md` are pulled into the
rendered set by `{include}` stubs (`docs/contributing.md`,
`docs/security-policy.md`, `docs/changelog.md`) with `heading-offset: 1`, so
their heading structure has to stay well-formed under a one-level shift. Those
three also cannot carry repo-root-relative Markdown links: a link that works
on GitHub does not resolve once the file is included from `docs/`, and Sphinx
reports it as a missing cross-reference. Reference sibling documents from them
as inline code paths instead.

## Commit conventions

Lowercase conventional commits, scope optional:

```
docs(security-model): reword the bcrypt drift note to state the risk
feat(scholar): keyless scholarly search - .papers / .thesis / .scholar
fix(deps): raise pyproject extras floors to requirements.txt policy
```

The body explains **why**, not what: the diff already says what changed. State
the problem the change addresses, the evidence, and what you deliberately did
not do. No emoji, no em-dashes, and no AI-attribution or `Co-Authored-By`
trailers.

Land work in small, independently-verifiable, bisectable commits. Keep a
security-sensitive change in its own commit and its own PR rather than coupling
it to unrelated work. Stage with explicit paths rather than a blanket
`git add -A`, and read the `N files changed` summary before pushing.

## CI gates

Three workflows, all triggered on push and pull request against `main`. All
declare least-privilege `permissions: contents: read` at the top and elevate
per job only where a SARIF upload needs `security-events: write`. Every action
is pinned to a commit SHA rather than a tag.

`tests.yml` has **four** jobs:

| Job | What it gates |
|---|---|
| `test` | both suites across 15 cells: `{ubuntu, macos, windows}` x Python `{3.10, 3.11, 3.12, 3.13, 3.14}`, `fail-fast: false` |
| `coverage` | needs `test`; the core-only 75% gate, uploads `coverage.xml` |
| `lint` | `python -m py_compile` over the 13 top-level modules, all of `weather_providers/`, and each `modules/*.py` |
| `package` | `scripts/verify_install.sh`, the wheel build-install-import gate |

The `test` job installs `requirements.lock --require-hashes` and then
`-e ".[dev]"`, stages `config.ini`, and runs `python tests/run_tests.py`
followed by `pytest tests/ -v --tb=short --strict-markers`. There is no
formatter or style linter anywhere in CI: `lint` checks syntax only, so
touched code must be `py_compile`-clean and nothing more is imposed.

`security.yml` has three jobs and also runs weekly on Monday 06:00 UTC:

| Job | What it gates |
|---|---|
| `bandit` | informational MEDIUM+ pass, then a gating pass at HIGH confidence and MEDIUM+ severity, then a SARIF upload |
| `pip-audit` | `requirements.lock` with `--strict`; any CVE fails the job |
| `gitleaks` | full-history secret scan (`fetch-depth: 0`) |

`pip-audit` scans the lockfile only, never the editable install (the local
`internets-irc` has no PyPI entry) and never the `pyproject.toml` extras, which
is why the extras floors have their own check in `tests/run_tests.py`. One
documented suppression: `--ignore-vuln PYSEC-2025-183`, a disputed pyjwt
finding about application-chosen key size rather than the library.
`[tool.bandit]` in `pyproject.toml` excludes `tests`, `.venv`, `build`, `dist`,
`.git`, and `__pycache__`.

`codeql.yml` runs GitHub's semantic SAST for Python with
`queries: security-extended`, on push, on pull request, and weekly on Tuesday
06:00 UTC. The suite is deliberately `security-extended` and not
`security-and-quality`: the latter's maintainability queries produced 123
alerts with no security value. The four open `py/overly-permissive-file`
alerts are triaged, benign, and deliberately left. Do not "fix" them without
discussion.

## Dependency policy

`requirements.txt` carries the runtime dependencies as **security floors**:
lower bounds set to a release known to be free of the CVEs annotated inline
beside each entry, with no upper bounds, so pip always takes the newest
compatible release and any future upstream break surfaces on the next push
across the full matrix. The floors in `pyproject.toml`'s optional extras must
match, and `tests/run_tests.py` fails if they drift below.

`requirements.lock` is the hash-pinned resolution of that file. CI installs it
with `--require-hashes`, so a wheel with the right name and the wrong hash
fails to install rather than executing. Regenerate it whenever
`requirements.txt` changes, from a Dependabot bump or a manual edit, and commit
both files in the same commit:

```bash
scripts/regen-lockfile.sh
```

The script resolves with `pip-compile --generate-hashes --strip-extras
--no-emit-options` inside an ephemeral venv, and it **requires Python 3.10
specifically on `PATH`**, failing loudly otherwise. The lock has to be resolved
on the lowest supported Python so that transitive dependencies gated on
`python_version < "3.11"` are captured at all.

**Known defect, live now:** the committed `requirements.lock` header records
that it was generated with Python 3.14, not 3.10. That resolution dropped
`typing_extensions>=4.4`, which `aiohttp` requires below Python 3.13, so every
Python <3.13 leg of the `test` job fails at
`pip install --require-hashes`. CI has been red on `main` since 2026-08-13. A
second defect hides the first on Windows: the install step runs three `pip`
commands in one `run:` block and `pwsh` does not fail fast, so the failing
install reports success and the job dies later in pytest with a confusing
`ModuleNotFoundError`. Full write-up in `docs/known-issues.md` (item 6). Do
not regenerate the lock on whatever interpreter happens to be default.

Dependabot watches pip and GitHub Actions daily, grouping security updates
separately from routine bumps so the security group can merge on its own.

## Cutting a release

The version lives in several hand-edited places. Two are cross-checked by
`tests/run_tests.py` and the rest by the version-literal guard beside it, so a
missed one fails the suite rather than shipping.

1. **Bump** `pyproject.toml` `version`, `config.py` `__version__`, and
   `docs/conf.py` (three literals: `release`, the truncated `version`
   MAJOR.MINOR, and `html_title`). The prose literals in `README.md`,
   `docs/*.md`, and the User-Agent example in `config.ini.example` move with
   them.
2. **Cut the CHANGELOG.** Rename `## [Unreleased]` to
   `## [X.Y.Z] - YYYY-MM-DD`, put the categories in Keep a Changelog order
   (Added, Changed, Deprecated, Removed, Fixed, Security), and merge duplicate
   category headings.
3. **Write a breaking-change preamble** directly under the version heading if
   anything requires operator action, following the shape 3.0.0 and 4.0.0 use:
   plain language, what breaks, the exact remedy. It is the only thing that
   reaches an operator who does not read the diff. Do not bury it in a bullet.
4. **Run every gate**: `python tests/run_tests.py`, `python -m pytest tests/`,
   the core-coverage gate, `scripts/verify_install.sh`,
   `scripts/verify-doc-citations.py`, and `scripts/build-docs.sh`.
5. **Tag** to the existing convention exactly: annotated, GPG-signed,
   `v`-prefixed, message `Internets vX.Y.Z`.

   ```bash
   git tag -s vX.Y.Z -m "Internets vX.Y.Z"
   git push origin vX.Y.Z
   ```

   The tag is `v`-prefixed while the CHANGELOG heading is not. Verify with
   `git tag -v vX.Y.Z`.

The version-literal guard scans `README.md`, `docs/conf.py`,
`docs/deployment.md`, `docs/configuration.md`, `docs/providers.md`, and
`docs/security-model.md` for any `X.Y.Z` on a line mentioning "version",
"release", or "Internets", and fails on anything that is not the current
version. It exempts lines carrying a comparison operator (`>=`, `==`, `~=`,
`<`). A consequence worth knowing before it bites you: writing a **third-party**
version number as a bare literal in one of those six files fails the suite. Use
a comparison operator, or name the package and its major without the full
triple.

SemVer here is about the operator, not an import surface. This is an
application: MAJOR means an existing working deployment needs the operator to
do something. v5.0.0 is major because a bcrypt password over 72 bytes stops
authenticating until `hashpw.py` is re-run, not because any Python API changed.

## Pull requests

- Open against `main`. `CODEOWNERS` assigns the maintainer as reviewer
  automatically.
- One-line summary plus a why-focused body; link the issue with `Fixes #N`.
- Both suites green locally, and all three workflows green on the PR.
- Fill in the checklist in `.github/PULL_REQUEST_TEMPLATE.md`. It is the
  short form of this page: sanitizer, `cred()`, `is_configured()`,
  `forget()`, no credentials in `config.ini`, `py_compile` clean.
- If you touched a cited source file, run `scripts/verify-doc-citations.py`
  before pushing.
- If you touched `requirements.txt`, run `scripts/regen-lockfile.sh` on
  Python 3.10 and commit both files together.
- If you touched packaging, run `scripts/verify_install.sh`.

A verified defect you find along the way belongs in `docs/known-issues.md`,
the permanent findings register, whether or not you fix it in the same PR.
That file is the single source for known-broken behaviour; do not restate its
entries elsewhere in the documentation, point at them.

## Reporting bugs and vulnerabilities

- Bugs and feature requests: open an issue with the templates in
  `.github/ISSUE_TEMPLATE/`. Blank issues are disabled; usage questions go to
  Discussions.
- Vulnerabilities: follow `SECURITY.md`. Use GitHub private vulnerability
  reporting from the repository Security tab. Do not open a public issue for
  a vulnerability.
