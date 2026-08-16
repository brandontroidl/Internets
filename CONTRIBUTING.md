# Contributing

Developer guide for the Internets IRC bot: Python 3.10+, asyncio, hot-reloadable
command modules. Read it alongside `README.md` for the system overview,
`SECURITY.md` for vulnerability reporting, and `CODE_OF_CONDUCT.md` (the
Contributor Covenant; participating in this project means agreeing to it).

This page is the contributor gateway. It states each rule, the mechanism that
enforces it, and where the full treatment lives. It does not repeat the
reference documentation under `docs/`; follow the pointers.

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
| `pyproject.toml` `[project.optional-dependencies]` | extras plus the `dev` tooling | you and CI |

The `dev` extra is test and CI tooling only and never reaches a deployment. The
runtime extras (`async`, `bcrypt`, `argon2`, `weatherkit`, `xml`, and the
aggregate `all`) exist so a packaged install can pull only what it needs. Their
floors must match `requirements.txt`, and `tests/run_tests.py` has a `DEPS`
check that fails if an extra ever sits below the `requirements.txt` floor.
Which package is genuinely optional and what breaks without it is in
`docs/dependencies.md`.

Nothing in `pyproject.toml` installs the documentation toolchain. See
[Building the documentation](#building-the-documentation) for that list.

### config.ini must exist before any test run

`config.ini` is gitignored (it holds `[secrets]` in a real deployment) and
`config.py` reads it at import time. The test harness imports `internets`,
which imports `config`, so with no `config.ini` present the suite aborts at the
first `from config import ...` before a single test runs. Copy the
credential-free template:

```bash
cp config.ini.example config.ini
```

Every CI job that runs tests does this in an explicit "Stage config.ini for
tests" step. Never commit a populated `config.ini`; the `gitleaks` job scans
full history and will catch it.

One trap worth knowing before you chmod anything: `secret_store.py - perms_ok()`
requires mode exactly 0600 on the file holding `[secrets]`, while the startup
warning in `botlog.py` suggests 640. Follow the code, not the warning
(`docs/known-issues.md`, item 7).

## Running the tests

Two runners, neither a superset of the other. Both must pass before you commit,
and CI runs both.

```bash
python -m pytest tests/ -q
python tests/run_tests.py
```

If either goes red, find out why before assuming your change is unrelated. Do
not record expected pass counts here or in a commit message; they move with
every added test and a stale count reads as a failure.

Prefer `python -m pytest` over the bare `pytest` console script. The script
carries a shebang pinning whichever interpreter installed it, which is not
necessarily the interpreter holding this project's dependencies; a mismatch
shows up as a wall of `ModuleNotFoundError` at collection rather than as an
obvious environment problem.

`tests/run_tests.py` is a self-contained harness with no pytest dependency. It
is named `run_tests.py`, not `test_*.py`, so pytest's default collection never
picks it up. Beyond ordinary unit coverage it holds the repo's completeness
gates: the sanitizer gate over the security-relevant modules, the dependency
floor check, and a version-literal check over a fixed set of documents.

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
dispatch test (`docs/known-issues.md`, "Test gaps worth closing first").

### Packaging gate

`scripts/verify_install.sh` builds an sdist and a wheel, installs the wheel into
a throw-away venv, checks every installed file's SHA-256 against the wheel's
`RECORD`, then verifies from outside the repo that each declared top-level
module imports and that the `internets` console entry point resolves.

```bash
./scripts/verify_install.sh          # exit 0 == verified
```

Run it after any change to `pyproject.toml` `py-modules`, `packages.find`, the
entry point, or the module layout. It is not optional diligence: a wheel
missing `audit_log`, `process_lock`, and `metrics` shipped in two major
releases because this script existed and nothing ran it. CI's `package` job now
runs it on every push and pull request. Detail:
`docs/internals/ci-and-packaging.md`.

`scripts/sbom.sh` generates a software bill of materials if you need one. No
workflow runs it.

## Coding conventions

Each rule below is in force in the tree today. The one-line reason is the
failure it prevents; the link is where the mechanism is written out.

### Handlers are coroutines, blocking work goes to a thread

Why: one event loop serves every user, so a synchronous call on the loop stops
the whole bot, not just the caller.

Every command handler is an `async def`.
`modules/base.py - BotModule.__init_subclass__()` validates at class-definition
time that each `COMMANDS` entry names an existing `async def` method, so a typo
raises `TypeError` at import rather than at first use. HTTP, disk, and password
hashing run through `await asyncio.to_thread(...)`. The `_CMD_TIMEOUT` of 60
seconds in `internets.py - IRCBot` cancels an await; it cannot interrupt a
synchronous call already running on the loop, which is exactly how
`modules/mathx.py - MathxModule.cmd_isprime()` became a whole-bot denial of
service. That is what breaking this rule looks like: `docs/known-issues.md`,
item 3. Detail: `docs/writing-modules.md`, section 4.

### Outbound HTTP is size-capped before the body is parsed

Why: a compromised or misconfigured upstream returning a multi-gigabyte body
takes the single process down.

Use `modules/base.py - fetch_json()`, which streams the body and raises
`ResponseTooLarge` past its cap before decoding or parsing. A bare `r.json()`
or an unbounded `r.text` in a module is a defect by project policy.

State it accurately: `fetch_json()` is the preferred helper, not a chokepoint.
Most module call sites still call `requests` directly and implement the
stream-and-cap loop inline, and as measured they all currently do it correctly.
So the invariant holds by convention re-implemented at each site, not by a
property the architecture enforces, and nothing in CI checks it. Treat a new
direct call site as needing its own review. The measurement and the exact
counts are in `docs/dependencies.md`, "Direct dependencies"; the two accepted
shapes are in `docs/writing-modules.md`, section 5.
`weather_providers/pirateweather/_codes.py - safe_get_json()` is the model
inline implementation, and it also shows the correct way to raise without
embedding a keyed URL in the exception text.

### User-supplied URLs and hosts go through `modules/_netsafe.py`

Why: without it, a user-chosen name that resolves to an internal address turns
the bot into an SSRF proxy for the host's private network.

`modules/_netsafe.py - safe_open()` is the guard for anything that hands a URL
to an HTTP library and follows redirects; it resolves to a public address,
rejects private and reserved ranges, and pins the resolved address for the
duration so a DNS answer cannot change between check and connect. Detail:
`docs/internals/modules/_netsafe.md` and `docs/writing-modules.md`, section 5.

### Sanitize third-party and user text with `strip_ctrl`

Why: control bytes reaching a bot-attributed IRC line let upstream data forge
the shape of a line.

Text spliced into a reply goes through `modules/base.py - strip_ctrl()`, which
strips C0 control characters, CR, LF, and NUL and truncates at 400 characters
by default. Apply it before the line is assembled, not after.

Nothing enforces this. `tests/run_tests.py` holds a completeness gate over
`search`, `seen`, `tell`, `stocks`, `remind`, and `location` that fails if any
of them drops the sanitizer (`modules/weather.py` is allowed its equivalent
`_sanitize`), but that gate is a source-text check over an enumerated list: it
does not cover a module outside the list and it does not prove every emit path
in a listed module is covered. `modules/twitch.py` shipped with no sanitizer at
all (`docs/known-issues.md`, item 20). Conventions and the author checklist:
`docs/output-conventions.md`.

### Credentials through `cred()`, never argv

Why: a value on the command line is visible in process listings and shell
history, and a value read straight from `cfg[...]` ignores the secret store.

Every API key, NickServ password, and similar value is read via
`modules/base.py - cred()` so the environment variable and the secret store win
over the config file. Add the canonical name to `secret_store.py -
KNOWN_SECRETS` and its config location to `secret_store.py - CONFIG_LOCATIONS`;
that makes it visible to `secret_store list` and movable by `migrate`. It does
not add it to log redaction: `sender.py - redact_secrets()` matches on
credential verbs (`PASS`, `IDENTIFY`, `AUTHENTICATE`, `OPER`, and friends), not
on a name registry, and it applies to log output only, never to a composed
`PRIVMSG`. Detail: `docs/writing-modules.md`, section 6.

### Snapshot module state under the lock, write outside it

Why: the save runs in a `to_thread` worker while handlers keep mutating the
same dict on the loop, and iterating a live dict from the worker raises
mid-write, so the save is silently skipped.

Shared mutable state is protected by a `threading.Lock`; take the snapshot
under the lock and serialize the copy outside it. Follow `store.py` and
`sender.py`. Three sites currently get this wrong and are the pattern to copy
away from, not toward: `internets.py - _save_shadow_bans()`,
`modules/notes.py - NotesModule._do_add()`, and
`modules/steam.py - SteamModule.cmd_regsteam()` (`docs/known-issues.md`, item
12). Note also that no writer in this repository calls `fsync`, and module
stores have no integrity envelope, so a corrupt file loads as empty and is
overwritten by the next save. The write pattern in full:
`docs/writing-modules.md`, section 7.

### Gate order: authorization, usage, cooldown, work

Why: authorization first so an unauthorized user learns nothing and burns no
quota; usage before the cooldown so a mistyped command is answered rather than
throttled.

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

`rate_limited()` consumes the token when it returns `False`, so calling it
twice charges the user twice. Both orders exist in the tree today and the
difference is real; the comparison and the two exceptions are in
`docs/writing-modules.md`, section 8. Do not add a third pattern.

### Types

Target 3.10: `from __future__ import annotations` and PEP 604 unions (`X | Y`,
never `Union[X, Y]`). There is no formatter or style linter in CI, so touched
code has to be `py_compile`-clean and nothing more is imposed.

## Adding a module or a provider

Do not derive the contract from a neighbouring file. Both paths have a guide
written from the source:

- A new command module: `docs/writing-modules.md`. Start from
  `modules/example.py`, the copy-and-fill skeleton. In summary, the file
  subclasses `modules.base.BotModule`, declares `COMMANDS: dict[str, str]`
  mapping each command word to an `async def cmd_*` method name, exposes a
  top-level `setup(bot)` returning the instance (`internets.py -
  IRCBot.load_module()` refuses a module without it), overrides
  `is_configured()` if it needs a key, and overrides `forget(nick)` if it
  persists user data so `.forgetme` reaches it. New user-derived data also
  changes what `PRIVACY.md` describes; update it in the same PR.
- A new weather provider: `docs/writing-providers.md`. The dispatcher,
  ranking, and health and circuit-breaker behaviour are in `docs/providers.md`.
  Read known issue 2 before you rely on fallback: a provider that returns an
  empty result for most capabilities ends the chain instead of falling through.

A new module needs a test file. `docs/testing.md` has a worked template for
both cases.

## Documentation requirements

Documentation is part of the change, not a follow-up. Update it in the same PR
when you change any of: a command, a configuration key, a module, a weather
provider, security behaviour, persisted state, an integration, operator-facing
behaviour, a CLI option, or the architecture.

`docs/documentation-governance.md`, section 3, holds the what-to-update-when-
you-change-X table: for each change, the code artifacts and the doc artifacts
whose omission does not fail loudly. Read it before the edit rather than after.
It is a reading aid, not a gate; nothing checks it.

Three verification commands, all run from the repo root:

```bash
python scripts/gen-command-reference.py --check docs/command-reference.md
python scripts/verify-doc-citations.py
scripts/build-docs.sh
```

The first two run unconditionally in the `lint` job of
`.github/workflows/tests.yml`, on every push to `main` and every pull request
against it. `scripts/build-docs.sh` is run by no workflow, so a change that
breaks the Sphinx build or overflows a PDF page is found by whoever builds
next: run it yourself when you touch a documentation page.

Two rules the first two commands enforce:

- **Cite by symbol, not by line number.** The symbol form is a backticked
  `file.py - Class.method()`; the retired form is a backticked `file.py:123`. A
  line number goes stale silently the moment anything above it moves: the cited
  line still exists, it just describes something else.
  `scripts/verify-doc-citations.py` parses each cited file's AST and confirms
  the cited symbol exists, so it checks meaning rather than arithmetic. It
  exits non-zero on a failed symbol, an out-of-range line, or a missing file,
  and that failure count must stay at zero. Line-style citations are reported
  as REVIEW rather than PASS by design: they are the style being retired, and
  the remaining count should only ever go down. `--summary` prints counts only.
- **Command lists are generated, not hand-maintained.**
  `scripts/gen-command-reference.py` walks every module, instantiates each
  `BotModule` subclass without running `__init__`, and emits the inventory from
  actual registration. Run it with no arguments to emit Markdown, with
  `--check <file>` to fail on drift.

`scripts/remap-doc-citations.py` rewrites the line numbers of the few
line-style citations that remain, using a `difflib` map across one commit. It
keeps a citation pointing at the same text; it cannot tell you the prose was
already wrong. Prefer converting the citation to symbol style over renumbering
it, and run `verify-doc-citations.py` afterwards either way.

Both CI steps check names, not statements. A paragraph describing behaviour the
code no longer has passes every gate in this repository, which is why prose
accuracy is a review responsibility.

### Building the documentation

```bash
scripts/build-docs.sh html      # HTML only
scripts/build-docs.sh pdf       # PDF only
```

Output lands in `docs/_build/`, which is gitignored. Both targets must build.

The toolchain is Sphinx with MyST for the Markdown sources, `sphinx-autoapi`,
`sphinx-rtd-theme`, `sphinx-copybutton`, `sphinx-design`, and `graphviz`. PDF
additionally needs a TeX Live with `xelatex` and `makeindex`; the script runs
explicit xelatex passes rather than `latexmk`. None of this is in the `dev`
extra, so install it separately.

Three things to know before you write:

- `sphinx-autoapi` publishes the API reference from the source docstrings. A
  module-level docstring is published documentation and is held to the same
  accuracy bar as these Markdown files.
- The build is deliberately not run with `-W`. A warning baseline is expected
  and benign, and every member of it names a path under `docs/autoapi/`. Do not
  chase those. Any warning naming a file outside `docs/autoapi/` is a real
  defect in a hand-written page, and it is yours if you edited that page.
- The exit code does not mean the PDF is well formed: the script swallows
  xelatex output and reports success even when content ran off the printed
  page. Wide tables are the usual cause, which is why the house style keeps
  tables at four columns or fewer. Measuring the overfull-box count from
  `docs/_build/latex/internets.log`, and the budget to compare it against, are
  in `docs/documentation-governance.md`, section 2.

A new prose page goes under `docs/` and must be registered in the `toctree` in
`docs/index.md`, or Sphinx warns that the document is in no toctree.
`CONTRIBUTING.md`, `SECURITY.md`, and `CHANGELOG.md` are pulled into the
rendered set by `{include}` stubs (`docs/contributing.md`,
`docs/security-policy.md`, `docs/changelog.md`) with `heading-offset: 1`, so
their heading structure has to stay well-formed under a one-level shift. Those
three also cannot carry repo-root-relative Markdown links: a link that works on
GitHub does not resolve once the file is included from `docs/`, and Sphinx
reports it as a missing cross-reference. Reference sibling documents from them
as inline code paths instead.

## Dependency policy

`requirements.txt` carries the runtime dependencies as security floors: lower
bounds set to a release known to be free of the CVEs annotated inline beside
each entry, with no upper bounds, so pip always takes the newest compatible
release and any future upstream break surfaces on the next push across the full
matrix. The floors in `pyproject.toml`'s optional extras must match, and
`tests/run_tests.py` fails if they drift below.

`requirements.lock` is the hash-pinned resolution of that file. CI installs it
with `--require-hashes`, so a wheel with the right name and the wrong hash
fails to install rather than executing. Regenerate it whenever
`requirements.txt` changes, from a Dependabot bump or a manual edit, and commit
both files in the same commit:

```bash
scripts/regen-lockfile.sh
```

The script resolves with `pip-compile --generate-hashes --strip-extras
--no-emit-options` inside an ephemeral venv, and it requires Python 3.10
specifically on `PATH`, failing loudly otherwise. The lock has to be resolved
on the lowest supported Python so that transitive dependencies gated on
`python_version < "3.11"` are captured at all.

The cautionary example is live right now. The committed `requirements.lock`
header records that it was generated on Python 3.14, not 3.10. That resolution
dropped `typing_extensions`, which `aiohttp` requires below Python 3.13, so
every Python 3.10 through 3.12 leg of the `test` job fails at `pip install
--require-hashes`, and CI has been red on `main` since 2026-08-13. A second
defect hides the first on Windows: the install step runs three `pip` commands
in one `run:` block and `pwsh` does not fail fast, so the failing install
reports success and the job dies later in pytest with a confusing
`ModuleNotFoundError`. Full write-up in `docs/known-issues.md`, item 6. Do not
regenerate the lock on whatever interpreter happens to be default.

Dependabot watches pip and GitHub Actions daily, grouping security updates
separately from routine bumps so the security group can merge on its own. The
full policy, including the vulnerability-response path and its gaps, is in
`docs/dependencies.md`.

## CI gates

Three workflows, all triggered on push and pull request against `main`. All
declare least-privilege `permissions: contents: read` at the top and elevate
per job only where a SARIF upload needs `security-events: write`. Every action
is pinned to a commit SHA rather than a tag.

`tests.yml` has four jobs:

| Job | What it gates |
|---|---|
| `test` | both suites across `{ubuntu, macos, windows}` x Python `{3.10, 3.11, 3.12, 3.13, 3.14}`, `fail-fast: false` |
| `coverage` | needs `test`; the core-only 75% gate, uploads `coverage.xml` |
| `lint` | `python -m py_compile` over the top-level modules, all of `weather_providers/`, and each `modules/*.py`, then the two documentation gates |
| `package` | `scripts/verify_install.sh`, the wheel build-install-import gate |

There is no formatter or style linter anywhere in CI: `lint` checks syntax,
then citations and command drift.

`security.yml` has three jobs and also runs weekly on Monday 06:00 UTC:

| Job | What it gates |
|---|---|
| `bandit` | informational MEDIUM+ pass, then a gating pass at HIGH confidence and MEDIUM+ severity, then a SARIF upload |
| `pip-audit` | `requirements.lock` with `--strict`; any CVE fails the job |
| `gitleaks` | full-history secret scan (`fetch-depth: 0`) |

`pip-audit` scans the lockfile only, never the editable install and never the
`pyproject.toml` extras, which is why the extras floors have their own check in
`tests/run_tests.py`. It carries one documented suppression,
`--ignore-vuln PYSEC-2025-183`.

`codeql.yml` runs GitHub's semantic SAST for Python with
`queries: security-extended`, on push, on pull request, and weekly on Tuesday
06:00 UTC. The suite is deliberately not `security-and-quality`: the latter's
maintainability queries produced a large alert volume with no security value.
The open `py/overly-permissive-file` alerts are triaged, benign, and
deliberately left. Do not "fix" them without discussion.

Deeper treatment of all three workflows, the suppression, and the packaging
gate: `docs/internals/ci-and-packaging.md`.

## Commit conventions

Lowercase conventional commits, scope optional:

```
docs(security-model): reword the bcrypt drift note to state the risk
feat(scholar): keyless scholarly search - .papers / .thesis / .scholar
fix(deps): raise pyproject extras floors to requirements.txt policy
```

The body explains why, not what: the diff already says what changed. State the
problem the change addresses, the evidence, and what you deliberately did not
do. No emoji, no em-dashes, and no AI-attribution or `Co-Authored-By` trailers.

Land work in small, independently-verifiable, bisectable commits. Keep a
security-sensitive change in its own commit and its own PR rather than coupling
it to unrelated work. Stage with explicit paths rather than a blanket
`git add -A`, and read the `N files changed` summary before pushing.

## Pull requests

- Open against `main`. `CODEOWNERS` assigns the maintainer as reviewer
  automatically.
- One-line summary plus a why-focused body; link the issue with `Fixes #N`.
- Both suites green locally, and all three workflows green on the PR.
- Update `CHANGELOG.md` under `## [Unreleased]` if the change is user-visible
  or operator-visible.
- Fill in the checklist in `.github/PULL_REQUEST_TEMPLATE.md`. It is the short
  form of this page: sanitizer, `cred()`, `is_configured()`, `forget()`, gate
  order, no credentials in `config.ini`, `py_compile` clean.
- If you touched a cited source file, run `scripts/verify-doc-citations.py`
  before pushing.
- If you added, renamed, or removed a command, run
  `scripts/gen-command-reference.py --check docs/command-reference.md`.
- If you touched `requirements.txt`, run `scripts/regen-lockfile.sh` on Python
  3.10 and commit both files together.
- If you touched packaging, run `scripts/verify_install.sh`.

A verified defect you find along the way belongs in `docs/known-issues.md`, the
permanent findings register, whether or not you fix it in the same PR. That
file is the single source for known-broken behaviour; do not restate its
entries elsewhere in the documentation, point at them.

## Cutting a release

Release mechanics live in `docs/release-process.md`: the ordered list of places
the version is hand-edited, the CHANGELOG cut, the breaking-change preamble,
the gates to run, and the signed-tag convention.

Two things a contributor needs before that page. `tests/run_tests.py` carries a
version-literal guard over a fixed set of documents, so a missed version bump
fails the suite rather than shipping; a consequence is that writing a
third-party version as a bare three-part literal in one of those files also
fails the suite, so use a comparison operator or name the package and its major
instead. And SemVer here is about the operator, not an import surface: this is
an application, and MAJOR means an existing working deployment needs the
operator to do something.

## Reporting bugs and vulnerabilities

- Bugs and feature requests: open an issue with the templates in
  `.github/ISSUE_TEMPLATE/`. Blank issues are disabled; usage questions go to
  Discussions.
- Vulnerabilities: follow `SECURITY.md`. Use GitHub private vulnerability
  reporting from the repository Security tab. Do not open a public issue for a
  vulnerability.
