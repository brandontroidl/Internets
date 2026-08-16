# CI and packaging - repository automation and build metadata

## Purpose

What runs automatically against this repository (three GitHub Actions
workflows plus Dependabot), how the package is built and installed
(`pyproject.toml`), how the dependency pin/floor policy works
(`requirements.txt` vs `requirements.lock`), and what each maintenance script
under `scripts/` does. Everything below is verified against the files at
documentation time; the live CI state observed on 2026-08-15 is recorded in
Findings.

## Workflows

All three workflows pin every third-party action to a full commit SHA with a
version comment (`actions/checkout@3d3c42e5... # v5`, `actions/setup-python
@5fda3b95... # v6`, `github/codeql-action/*@e4fba868... # v4`,
`actions/upload-artifact@ea165f8d... # v4`, `gitleaks/gitleaks-action
@ff98106e... # v2.3.9`). No tag-pinned or unpinned action exists in the repo;
Dependabot's github-actions ecosystem keeps the SHAs current. All workflows
declare least-privilege `permissions: contents: read` at the top, with
per-job elevation (`security-events: write`) only where SARIF upload needs it.

### tests.yml - test, coverage, lint, packaging gates

Triggers: push and pull_request on `main`.

- **test** - a 15-leg matrix: `{ubuntu, macos, windows}-latest` x Python
  `3.10, 3.11, 3.12, 3.13, 3.14`, `fail-fast: false`. Steps: hash-pinned
  install (`pip install -r requirements.lock --require-hashes`, then
  `pip install -e ".[dev]"` unhashed - dev extras never reach production),
  stage `cp config.ini.example config.ini` (config.py reads config.ini at
  import time; the real file is gitignored), then `python tests/run_tests.py`
  followed by `pytest tests/ -v --tb=short --strict-markers`.
  `PYTHONIOENCODING: utf-8` is exported workflow-wide as belt-and-braces for
  Windows cp1252 consoles.
- **coverage** - ubuntu / 3.12, `needs: test`. Runs
  `coverage run -m pytest`, `coverage report --fail-under=75`, uploads
  `coverage.xml` (14-day retention). The gate is **core-only**:
  `pyproject.toml [tool.coverage.run]` omits `modules/*`,
  `weather_providers/*`, `internets.py`, and `console.py`, so 75% measures
  the unit-testable top-level core, not the repo.
- **lint** - `python -m py_compile` over every top-level module (the list is
  maintained by hand and includes `audit_log.py`, `process_lock.py`,
  `metrics.py`), then all of `weather_providers/` and `modules/`. A syntax
  check only; there is no style linter in CI.
- **package** - runs `bash scripts/verify_install.sh` (build wheel, install
  into a throwaway venv, verify RECORD hashes, smoke-test import + entry
  point). The step comment records why it exists: wheels missing
  `audit_log`/`process_lock`/`metrics` shipped uninstallable in v3.0.0 and
  v4.0.0 because CONTRIBUTING named this gate but nothing ran it.

### codeql.yml - semantic SAST

Triggers: push/PR on `main` plus weekly cron (Tuesday 06:00 UTC, offset from
security.yml's Monday run). Single job, Python only, using
`queries: security-extended` - the wider suite than the default. The header
comment records the division of labor: bandit is cheap pattern-matching SAST,
CodeQL catches taint/dataflow issues across function boundaries; both upload
SARIF to the same Security tab.

Triage state: the `security-extended` rollout took the alert count from 123
to 4; the 4 remaining open `py/overly-permissive-file` alerts are triaged
benign and deliberately left open. Do not re-fix them.

### security.yml - bandit, pip-audit, gitleaks

Triggers: push/PR on `main` plus weekly cron (Monday 06:00 UTC, so new CVEs
against pinned deps surface without a code change).

- **bandit** - three passes: informational MEDIUM+ (`-ll --exit-zero`), a
  gating pass that fails CI on HIGH severity + HIGH confidence (`-iii -ll`),
  and a SARIF pass uploaded `if: always()` so findings reach the Security tab
  even when the gate fails. `tests/`, venvs, and build dirs are excluded
  (mirrored in `pyproject.toml [tool.bandit]`).
- **pip-audit** - `pip-audit -r requirements.lock --strict` against the
  hash-pinned lockfile only (an editable local install has no PyPI entry and
  breaks a bare `pip-audit`). One documented suppression:
  `--ignore-vuln PYSEC-2025-183` (pyjwt), disputed upstream and inapplicable
  here because the key size is Apple-issued for WeatherKit ES256 signing.
  The step comment notes there is no SARIF upload because pip-audit does not
  emit SARIF and Dependabot already covers the Security tab. Consequence
  recorded in `pyproject.toml`: the audit covers `requirements.lock` only,
  never the extras' floors.
- **gitleaks** - full-history secret scan (`fetch-depth: 0`), with
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` set as an early opt-in ahead of
  GitHub's Node 20 runner deprecation (comment documents the removal
  condition).

### dependabot.yml

Two ecosystems, both daily against `main`: pip (PR limit 10, commit prefix
`deps`/`deps-dev`) and github-actions (PR limit 5, prefix `ci`). Each
ecosystem defines two groups: a security-updates group (merge fast,
independently) and a grouped minor+patch version-update group (noise
reduction). Major version bumps arrive ungrouped by default.

## Packaging - pyproject.toml

- **Identity**: `internets-irc` 5.0.0, ISC license, `requires-python >=3.10`,
  setuptools build backend. Classifiers cover 3.10-3.14, matching the CI
  matrix.
- **Console script**: `internets = "internets:_entry"` - the sole entry
  point; `verify_install.sh` asserts it resolves post-install.
- **Module inventory**: `[tool.setuptools] py-modules` hand-lists all 13
  top-level modules and `packages.find` includes `modules*` and
  `weather_providers*`. The comment above the list records the failure mode
  it guards: a top-level module omitted here is silently absent from the
  wheel (shipped broken in v3.0.0/v4.0.0), and `scripts/verify_install.sh`
  is the gate to run after touching it.
- **Runtime dependency**: only `requests>=2.32.3` is mandatory. Everything
  else is an extra: `async` (aiohttp), `bcrypt`, `argon2`, `weatherkit`
  (PyJWT + cryptography), `xml` (defusedxml), `all` (union), and `dev`
  (pytest, pytest-asyncio, coverage, bandit, pip-audit, build).
- **Extras floors policy**: extras floors must match `requirements.txt`,
  which carries the security-floor policy (lower bound = most recent
  CVE-free stable at session time, no upper bounds). The inline comment
  documents the incident behind the rule: `weatherkit` floors previously sat
  at PyJWT 2.10.1 / cryptography 44.0.0, so `pip install
  internets-irc[weatherkit]` could resolve versions the project's own
  requirements file called unsafe - and CI cannot catch that, because
  security.yml audits `requirements.lock` only, never the extras.
- **Coverage config**: branch coverage, core-only omit list (see tests.yml
  above), `fail_under = 75`, standard exclude pragmas.

## Pin/floor policy - requirements.txt vs requirements.lock

Two files, two jobs:

- `requirements.txt` - human-maintained **floors** (`requests>=2.32.3`,
  `aiohttp>=3.14.3`, `argon2-cffi>=23.1.0`, `bcrypt>=4.2.0`,
  `PyJWT>=2.13.0`, `cryptography>=50.0.0`, `defusedxml>=0.7.1`), each
  annotated with the CVE/advisory that set it. No upper bounds; the
  cross-platform CI matrix is the stated tripwire for a breaking upstream
  release.
- `requirements.lock` - machine-generated exact pins with `--hash=sha256:`
  entries for every package and transitive dep. CI installs from it with
  `--require-hashes`, which defends against PyPI account takeover and
  dependency confusion (a poisoned wheel with the right name but wrong hash
  fails to install).

`scripts/regen-lockfile.sh` regenerates the lock and its header states the
contract: run it whenever `requirements.txt` changes, commit both files
together, and - critically - resolve on **Python 3.10 specifically** (the
lowest supported version), because conditional transitive deps gated
`python_version < "3.11"` are otherwise silently omitted and break CI's 3.10
jobs. The script fails loudly if no 3.10 interpreter is found, builds an
ephemeral pip-tools venv, and runs `pip-compile --generate-hashes
--resolver=backtracking --strip-extras --no-emit-options`. The lock currently
in the repo violates this contract - see Findings.

## scripts/

- **build-docs.sh** - builds the Sphinx documentation set: `html`, `pdf`, or
  both (default). Requires sphinx + myst-parser + sphinx-autoapi +
  rtd-theme + copybutton + design + graphviz, and TeX Live with xelatex +
  makeindex for the PDF, which is produced by explicit xelatex passes rather
  than latexmk. Usage: `scripts/build-docs.sh [html|pdf]`.
- **sbom.sh** - generates a CycloneDX 1.x JSON SBOM (`sbom.cdx.json`, or
  `OUT=...`) via `pip-audit --format cyclonedx-json` against the
  **currently-installed environment**, deliberately, so the SBOM reflects
  what actually ships rather than what pyproject.toml claims; run it inside
  the same venv used for `python -m build`.
- **verify_install.sh** - the supply-chain install smoke test wired into
  tests.yml's `package` job: builds sdist + wheel, installs the wheel into a
  throwaway venv, verifies every installed file against the SHA-256 hashes
  in the wheel's RECORD (catches tampering or a broken extractor), then
  smoke-tests import, version string, and console entry-point resolution.
  Exit 0 means verified.
- **remap-doc-citations.py** - repairs `file.py:LINE` citations in the docs
  after a source edit shifts line numbers. It builds an exact old->new line
  map with difflib between a git ref and the working tree and rewrites the
  citations; deleted lines are reported UNMAPPABLE and left for a human.
  Report-only by default; `--apply` writes. Usage:
  `scripts/remap-doc-citations.py <git-ref> <file>... [--apply]`.
- **gen-command-reference.py** - generates the user command inventory from
  actual command registration: walks `modules/`, instantiates each
  `BotModule` subclass without running `__init__` (the `test_help.py`
  technique - no network/keys/config needed), and emits one Markdown table
  row per module plus the admin `_CORE` commands. `--check FILE` exits 1 if
  FILE is missing any primary command - the docs drift gate.

## CODEOWNERS and templates

- `CODEOWNERS` (repo root): `* @brandontroidl` - every PR auto-requests the
  single maintainer; the comment invites per-path owners as the project grows.
- `.github/ISSUE_TEMPLATE/bug_report.md` - defect report form, auto-labels
  `bug`, title prefix `[Bug]`.
- `.github/ISSUE_TEMPLATE/feature_request.md` - enhancement form, auto-labels
  `enhancement`, title prefix `[Feature]`.
- `.github/ISSUE_TEMPLATE/config.yml` - `blank_issues_enabled: false`;
  routes questions to GitHub Discussions and security reports to the private
  process in SECURITY.md instead of public issues.
- `.github/PULL_REQUEST_TEMPLATE.md` - summary/linked-issue skeleton asking
  for motivation over mechanics.

## docs/conf.py - the documentation build

`docs/conf.py` is the only source file in the tree that this reference does not
document as bot code, because it is build tooling. What it configures:

| Setting | Effect |
| --- | --- |
| `extensions` | myst-parser for Markdown, sphinx-autoapi for the API pages, napoleon, intersphinx, graphviz, copybutton, sphinx-design |
| `autoapi_dirs` | the repository root, filtered by `autoapi_ignore`; parses statically and never imports the bot, so side-effectful imports cannot break the build |
| `myst_heading_anchors` | 4, so cross-document heading links resolve down to h4 |
| `latex_elements` | letterpaper, 10pt, `verbatimforcewraps` for long code lines, and the table/index fitting preamble described below |

**Table and index fitting.** Markdown tables become `longtable` with
non-wrapping columns in LaTeX, so cells holding long symbol names or paths run
off the printed page. The HTML build never reveals this. The preamble shrinks
the table font and adds `\emergencystretch`; the generated index is set ragged
right because fully-qualified Python names exceed its column width; and the two
tables that still overflowed carry an explicit `{tabularcolumns}` directive with
`p{}` widths (`docs/modules.md` and
`docs/internals/weather-providers/init.md`).

Measure this from `docs/_build/latex/internets.log`, not from the build
script's output: `scripts/build-docs.sh` does not surface xelatex's overfull-box
warnings, so a page that overflows badly still reports a successful build.

```bash
grep -c 'Overfull \\hbox' docs/_build/latex/internets.log
```

Current state: 323 overfull boxes, none severe (worst 95.7pt), zero LaTeX
errors, 850 pages.

## Findings

- defect | requirements.lock (header line 2) vs scripts/regen-lockfile.sh |
  The committed lock says "autogenerated by pip-compile with Python 3.14",
  violating the script's resolve-on-3.10 contract, and omits marker-gated
  transitives. Verified live: the Tests workflow is red on `main` (run
  31848249036, push of 507e1ea, 2026-08-14) - every Python < 3.13 leg fails
  `pip install -r requirements.lock --require-hashes` with "all requirements
  must have their versions pinned", naming `typing_extensions>=4.4` pulled
  by `aiohttp==3.14.3` but absent from the lock. Regenerating the lock per
  the script (on 3.10) is the fix.
- defect | tests.yml - test job "Install dependencies" step | The three pip
  commands share one `run:` block. On Windows the default shell (pwsh) does
  not stop on a failing command, so when the lockfile install fails the step
  still reports success, `pip install -e ".[dev]"` proceeds, and the job
  fails much later in pytest with `ModuleNotFoundError: No module named
  'bcrypt'` / `'argon2'` - observed in the same run (Windows 3.11/3.12
  legs). The Linux/macOS legs fail fast because bash runs with `-e`. Fix:
  split the steps or set a failing-fast shell so the install failure is the
  reported failure.
- questionable | security.yml - pip-audit job scope | Audits
  `requirements.lock` only; `pyproject.toml` extras floors are covered by no
  automated check (acknowledged in the pyproject comment). A floor that
  drifts below the requirements.txt policy is caught only by review.
- questionable | tests.yml lint job | The `py_compile` top-level file list is
  hand-enumerated, the same maintenance shape whose failure (the pyproject
  `py-modules` omission) shipped broken wheels; a new top-level module added
  without updating this list is silently unlinted.
- doc-drift | pyproject.toml dev extra / `asyncio_mode` | CI installs
  pytest-asyncio via `.[dev]` while the suite's own docstrings state the
  project has no pytest-asyncio plugin and drive event loops manually; local
  runs without the dev extra emit `Unknown config option: asyncio_mode`.
  Detailed in docs/internals/tests.md Findings.
