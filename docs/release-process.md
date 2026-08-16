# Release process

How a release is cut, verified, and published, and what to do when one turns
out to be wrong. Grounded in the repository: `pyproject.toml`, `config.py`,
`docs/conf.py`, `tests/run_tests.py`, `scripts/verify_install.sh`, the three
workflows, and the five existing tags.

`CONTRIBUTING.md` carries the short procedure for a contributor. This page is
the engineering version: the same steps with the failure modes attached, plus
the parts CONTRIBUTING does not cover - post-release verification, withdrawal
criteria, and the emergency path.

## What a release is here

A release is an annotated, GPG-signed git tag on `main`, plus the CHANGELOG
section that describes it. There is no publishing workflow: nothing in
`.github/workflows/` builds or uploads a distribution, and no `twine` step
exists anywhere in the repository. Artifacts are built locally by
`scripts/verify_install.sh` into `dist/` for verification and are not pushed
anywhere by any automation in this repository.

| Property | Value |
|---|---|
| Tag object | Annotated, GPG-signed |
| Tag name | `vX.Y.Z` |
| Tag message | `Internets vX.Y.Z` |
| CHANGELOG heading | `## [X.Y.Z] - YYYY-MM-DD`, no `v` |
| Branch | `main` |

Verified across all five tags (`v2.5.0` through `v5.0.0`): every one is a tag
object, not a lightweight ref, and every one carries a PGP signature. Verify
with `git tag -v vX.Y.Z`.

What the version number means, and what a MAJOR bump obliges you to write, is
[versioning-and-support.md](versioning-and-support.md).

## 1. Preconditions

Before touching a version literal:

- [ ] `main` is green on all three workflows. It is not, currently: see the
      lockfile defect below, which blocks any release that expects a green
      matrix.
- [ ] The working tree is clean and on `main`.
- [ ] Every defect found during the cycle that is not being fixed has an entry
      in [known-issues.md](known-issues.md). That register is the single source
      for known-broken behavior, and a release that leaves it stale ships a
      documentation defect alongside the code.

:::{warning}
**Blocking now.** `requirements.lock` was generated on Python 3.14 instead of
3.10, dropping `typing_extensions>=4.4`, so every Python < 3.13 leg of the Tests
workflow fails at the hash-checked install and `main` has been red since
2026-08-13. Regenerate with `scripts/regen-lockfile.sh` on Python 3.10 before
cutting anything. Item 6 in [known-issues.md](known-issues.md);
[dependencies.md](dependencies.md#the-lockfile) has the mechanism.
:::

## 2. Bump the version

The version is hand-edited in every location below. Nothing derives it from
anything else.

| File | Literal |
|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `config.py` | `__version__ = "X.Y.Z"` |
| `docs/conf.py` | `release = "X.Y.Z"`, `version = "X.Y"`, `html_title = "Internets X.Y.Z"` |
| Prose | `README.md`, `config.ini.example` (the User-Agent example), and the docs pages the guard scans |

`config.py - __version__` is the runtime value; `internets.py`, `botlog.py`,
`admin_cmds.py`, and `console.py` all import it from there, so there is exactly
one runtime definition and no drift risk inside the code itself.

Three tests in `tests/run_tests.py` enforce the rest:

| Test | Asserts |
|---|---|
| `VERSION: __version__ matches pyproject.toml` | The two authoritative literals agree |
| `VERSION: __version__ is defined and follows semver` | Three dotted numeric parts |
| `VERSION: every hand-written version literal in docs matches __version__` | No stale `X.Y.Z` in six named docs, and `docs/conf.py`'s truncated `version` equals MAJOR.MINOR |

The third scans `README.md`, `docs/conf.py`, `docs/deployment.md`,
`docs/configuration.md`, `docs/providers.md`, and `docs/security-model.md`, on
any line mentioning "Internets", "version", or "release", exempting lines with a
comparison operator (`>=`, `==`, `~=`, `<`). One consequence to know before it
bites: writing a **third-party** version as a bare literal in one of those six
files fails the suite. Use a comparison operator, or name the package and its
major without the full triple.

Its file list is hand-enumerated, which is the same maintenance shape that
shipped broken wheels (below). A doc page added later is unscanned unless
someone adds it.

## 3. Finalize the CHANGELOG

1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
2. Put the categories in Keep a Changelog order: Added, Changed, Deprecated,
   Removed, Fixed, Security. Merge duplicate category headings - the Unreleased
   section currently has two `### Fixed` and two `### Security` blocks, which is
   exactly the state this step exists to clean up.
3. **Write the breaking-change preamble** directly under the version heading if
   anything requires operator action, in the shape 3.0.0, 4.0.0, and 5.0.0 use:
   plain language, what breaks, the exact remedy. This is the only thing that
   reaches an operator who does not read the diff. Do not bury it in a bullet.
   [versioning-and-support.md](versioning-and-support.md#what-a-major-release-has-actually-meant)
   has the three worked precedents.
4. Open a fresh empty `## [Unreleased]` above it.

## 4. Run every gate

Serially, on a clean tree, reading each result rather than chaining them:

```bash
python tests/run_tests.py
python -m pytest tests/ -v --tb=short --strict-markers
coverage run -m pytest tests/ --strict-markers && coverage report --fail-under=75
bash scripts/verify_install.sh
scripts/verify-doc-citations.py
scripts/gen-command-reference.py --check docs/command-reference.md
scripts/build-docs.sh
```

Notes that matter:

- **The two test suites are disjoint.** `tests/run_tests.py` is not collected by
  pytest, so neither command is a superset of the other. Both must run.
- **`config.ini` must exist before any test run.** `config.py` reads it at
  import. CI stages it with `cp config.ini.example config.ini`; do the same
  locally if you do not already have one.
- **The coverage gate is core-only.** `pyproject.toml` omits `modules/*`,
  `weather_providers/*`, `internets.py`, and `console.py`, so 75% measures the
  top-level core, not the repository. Do not report it as repo-wide coverage.
- **Do not chain a gate into a state-changing step through a pipe.** A shell
  pipeline reports the last command's status, so `gate | tail && git tag` tags on
  a red gate.

The full matrix (five Pythons, three operating systems) runs in CI, not locally.
A release is cut from a commit whose CI is green across the matrix, which is
the only place the Windows and macOS legs are exercised.

## 5. Verify the built artifact

`scripts/verify_install.sh` is the packaging gate, wired into `tests.yml` as the
`package` job. It exits 0 only if all of the following hold.

1. `python -m build` produces **both** a wheel and an sdist.
2. Both install into a fresh throwaway venv.
3. Every file the wheel installed re-hashes to the SHA-256 recorded in the
   wheel's `RECORD` metadata. This catches tampering and a broken extractor.
4. `import internets` succeeds and `internets.__version__` is truthy.
5. All 13 declared top-level modules import from the wheel. A `ModuleNotFoundError`
   naming the module itself is a packaging failure; one naming a third-party
   dependency is not, and the script distinguishes them.
6. The `internets` console entry point resolves in `console_scripts`.

The script changes directory out of the repository root before steps 4 to 6,
and the reason is recorded in the script: Python puts the current directory on
`sys.path`, so with the cwd at the repo root `import internets` loads
`internets.py` from **source** and `importlib.metadata` finds the stale
`internets_irc.egg-info/` instead of the venv's `dist-info`. Both made the gate
inspect the thing it was supposed to be checking against. That is how the
failure below shipped twice.

### The packaging hazard

:::{warning}
**Verified, and it has shipped twice.** `[tool.setuptools] py-modules` in
`pyproject.toml` is a hand-enumerated list of the 13 top-level modules. A module
missing from that list is silently absent from the wheel. `audit_log`,
`process_lock`, and `metrics` were missing, and because `internets.py` imports
`process_lock` and `admin_cmds.py` imports `audit_log` at module scope, the
console script raised `ModuleNotFoundError` on start. **Releases 3.0.0 and 4.0.0
both shipped an uninstallable package this way.** It was fixed in 5.0.0, and the
gate that should have caught it was only then wired into CI - before that,
`verify_install.sh` existed and nothing ran it.

The same family of defect is live now: `config.ini.example` is in **neither the
wheel nor the sdist**. Confirmed against the committed
`dist/internets_irc-5.0.0-py3-none-any.whl` (232 entries) and
`dist/internets_irc-5.0.0.tar.gz` (313 entries); there is no `MANIFEST.in` and
no `package-data` declaration, so setuptools has no reason to include it.
`secret_store.py - _cmd_init()` reads that template from the working directory,
so `python -m secret_store init` cannot bootstrap a package-only install. It
compounds a second defect in the same install shape: `config.py` `MODULES_DIR`
defaults to the relative path `modules`, resolved against the cwd, so every
autoload entry fails on a packaged install until `[bot] modules_dir` is pointed
at the installed location by hand. Item 14 in
[known-issues.md](known-issues.md).
:::

`verify_install.sh` does not catch either of the two live defects, by
construction. It stages `config.ini.example` from the **repository** into the
temp directory rather than expecting it in the wheel, and its own comment states
that the omission is deliberate because "the bot is installed from a checkout".
[known-issues.md](known-issues.md) item 14 treats the same omission as a defect
with a fix shape. Those are two positions on one question, and the maintainer
has to pick one: either the wheel is a supported install shape and must ship its
template and resolve `modules_dir`, or it is not and the packaging gate should
say so. Until then, the pre-release checklist below assumes the wheel is
supposed to work and tests it as such.

### Pre-release packaging checklist

Run these **in addition to** `verify_install.sh`, in a directory that is not the
repository, with a venv that has never had the source tree on its path. The
point is to exercise what an operator installing the artifact actually does,
which is more than importing modules.

```bash
cd "$(mktemp -d)"
python -m venv v && . v/bin/activate    # Windows: v\Scripts\activate
pip install /path/to/repo/dist/internets_irc-X.Y.Z-py3-none-any.whl
```

- [ ] `python -c "import internets; print(internets.__version__)"` prints the
      version being released.
- [ ] `pip show -f internets-irc` lists all 13 top-level modules, plus
      `modules/` and `weather_providers/`. Compare the count against
      `[tool.setuptools] py-modules` by eye; a shrinking list is the failure that
      shipped twice.
- [ ] `python -c "import audit_log, process_lock, metrics"` succeeds. These
      three are the modules that were actually missing, and they are imported at
      module scope by the entry path, so their absence is fatal rather than
      degrading.
- [ ] The `internets` console script exists on `PATH` and `internets --version`
      prints the release version. `verify_install.sh` checks only that the entry
      point *resolves*; running it is the check that the process starts.
- [ ] `python -m secret_store init` either bootstraps a `config.ini` or fails
      with a message an operator can act on. It currently cannot bootstrap,
      because the template is not packaged - confirm the failure is still the
      known one and not a new one.
- [ ] With a hand-staged `config.ini` whose `[bot] modules_dir` points at the
      installed package directory, the bot starts and the log shows modules
      loading rather than a wall of `'modules/<name>.py' not found`. This is the
      step that would have caught item 14.
- [ ] Repeat the install from the **sdist**, not only the wheel. The two have
      different content lists and the gate above only ever installs the wheel.

If any of these fail, the release is not ready. A green suite does not prove the
artifact runs; that distinction is the whole reason 3.0.0 and 4.0.0 shipped
broken.

## 6. Tag and push

```bash
git tag -s vX.Y.Z -m "Internets vX.Y.Z"
git push origin vX.Y.Z
git tag -v vX.Y.Z
```

Tag the commit that CI went green on, not a later one. Push the branch first, so
the tag has a public commit to point at.

## 7. Post-release verification

Within the hour, from a clean environment:

- [ ] `git tag -v vX.Y.Z` verifies the signature from a fresh clone, not only
      from the machine that made it.
- [ ] The tag points at the commit you expect: `git rev-list -n1 vX.Y.Z`.
- [ ] The three workflows are green on that commit. A tag does not re-trigger
      them; they ran on the push.
- [ ] The CHANGELOG on `main` shows the new version heading with a date, and an
      empty `## [Unreleased]` above it.
- [ ] The published documentation announces the new version. `docs/conf.py`
      `html_title` and `release` are what appear in the HTML and the PDF.
- [ ] An upgrade rehearsal on a non-production deployment: stop, back up, pull
      or install, start, and confirm `.stats`, `.health`, and `.audit verify`
      all answer. [operations.md](operations.md#upgrade-procedure) is the
      procedure.
- [ ] If the release carries a breaking-change preamble, walk the remedy it
      describes end to end at least once. 5.0.0's preamble tells an operator to
      re-run `hashpw.py`; a preamble whose remedy has never been executed is an
      untested recovery path.

## 8. Withdrawing a release

:::{note}
**Proposal, not established policy.** No release has been withdrawn, and the
repository records no procedure. What follows is a recommended default for the
maintainer to confirm or amend.
:::

**Criteria to withdraw rather than fix forward.** Any one of:

- The artifact does not start on a supported platform - the 3.0.0 and 4.0.0
  packaging failure is the worked example.
- The release destroys or corrupts operator state, or silently disables a
  security control (an authorization gate, the audit chain, the secret store's
  permission check, an SSRF guard).
- The breaking-change remedy printed in the CHANGELOG does not actually work, so
  an operator following it stays broken.
- A credential or personal data was committed in the release.

Anything less than that is fixed forward in the next release. A defect that is
merely wrong, even badly wrong, goes to [known-issues.md](known-issues.md) and
the next version.

**How to withdraw, proposed:**

1. **Do not delete or move a pushed tag.** It is signed, it may already be
   fetched, and a moved tag makes two different trees claim one version - a
   worse failure than the one being fixed. `git push --delete` of a tag is also
   a destructive remote operation on a shared branch.
2. Ship `X.Y.Z+1` immediately with the fix, and give it a preamble that names
   the withdrawn version and says plainly not to use it.
3. Add a line to the withdrawn version's own CHANGELOG section pointing at the
   replacement. That section is the artifact an operator reads before upgrading.
4. If the artifact was ever distributed beyond the git tag, retract it at that
   distribution point too. Nothing in this repository publishes anywhere, so
   today this step is empty; it stops being empty the moment a publish step is
   added, and this list is where that has to be recorded.
5. If the release exposed a credential, treat rotation as the primary action and
   the release as secondary. A rotated key is the fix; a deleted tag is not.

## 9. Emergency security release

The normal path is deliberately slow, and a real vulnerability should not wait
for it. The abbreviated path, consistent with `SECURITY.md`:

1. **Handle the report privately.** `SECURITY.md` names GitHub private
   vulnerability reporting from the repository Security tab as the only accepted
   channel, explicitly not a public issue, pull request, discussion, or IRC
   message. Do not open a public issue to track the fix before the fix exists.
2. **Fix on a branch, in one isolated commit.** A security change coupled to
   unrelated in-progress work cannot be reviewed or reverted cleanly.
3. **Prove the fix with a test that fails without it**, exercising the real
   path. For a guard, drive a malformed value through it and watch it deny, and
   drive a good value through and watch it still pass; a guard verified only on
   the deny branch is half-verified and over-blocking is its own failure.
4. **Run the gates that can actually catch a regression here**: both suites,
   `verify_install.sh` if packaging moved, and the security workflow. Do not
   skip the full matrix on the argument that the change is small; the fail-closed
   and stricter-validation changes that security fixes consist of are precisely
   the class CI catches and a targeted local run does not.
5. **Bump appropriately, and do not undersell it.** If the fix locks an operator
   out or requires them to act - re-hash a password, move a credential, change a
   config - it is MAJOR under this project's operator-cost rule, however small
   the diff. 5.0.0 is a two-function change and a major release.
6. **Write the CHANGELOG entry under `### Security`** with what was wrong, what
   an attacker could do, and what the operator must do. The existing Security
   entries are the model: they state the demonstrated impact, not a severity
   label.
7. **Tag and announce through the advisory**, then publish the advisory so the
   fix is discoverable by the people running the vulnerable version.

For a dependency CVE rather than a defect in this code, the path is shorter and
is in [dependencies.md](dependencies.md#vulnerability-response): raise the floor
in `requirements.txt`, raise the matching `pyproject.toml` extras floors,
regenerate the lock on Python 3.10, and record it under `### Security`.
Dependabot groups security updates separately from routine bumps for exactly
this reason - the security group can merge on its own without waiting on a
feature bump.

## Related reading

- [versioning-and-support.md](versioning-and-support.md) - what the number
  means, and the compatibility contract a release is promising.
- [dependencies.md](dependencies.md) - the pin model, lockfile regeneration, and
  vulnerability response.
- [contributing.md](contributing.md) - the contributor-facing short form of this
  procedure.
- [internals/ci-and-packaging.md](internals/ci-and-packaging.md) - the workflows
  and scripts in detail.
- [deployment.md](deployment.md) - install shapes and rollback.
- [known-issues.md](known-issues.md) - the verified-defect register.
