# Versioning and support

What a version number promises, what a major release has actually cost an
operator in this project's history, which Python versions are supported, and
what happens to configuration and on-disk state when you move between versions
in either direction.

This is the compatibility contract. It is written from the repository: the
CHANGELOG, the git tags, `pyproject.toml`, the CI matrix, `config.py`, and
`store.py`. Where the project has not decided something, this page says so and
offers a default for the maintainer to confirm rather than inventing policy.

## Numbering

`CHANGELOG.md` declares [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
and [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) in its header, and
the practice matches: five releases, all `MAJOR.MINOR.PATCH`, no pre-release or
build-metadata suffixes, no patch release yet.

| Tag | CHANGELOG date | Kind |
|---|---|---|
| `v5.0.0` | 2026-07-22 | Major |
| `v4.0.0` | 2026-06-28 | Major |
| `v3.0.0` | 2026-05-20 | Major |
| `v2.6.0` | 2026-05-20 | Minor |
| `v2.5.0` | 2026-05-19 | Minor |

Every tag is an annotated, GPG-signed tag whose message is `Internets vX.Y.Z`.
Verify one with `git tag -v v5.0.0`. The tag carries a `v` prefix; the CHANGELOG
heading does not. Anything before `2.4.0` is git history only, by the
CHANGELOG's own statement.

### SemVer here is about the operator, not an import surface

This is an application, not a library. Nothing imports `internets-irc` as a
dependency, so "backward incompatible" cannot mean an API signature changed. It
means an existing, working deployment needs the operator to do something before
it works again. `CONTRIBUTING.md` states this rule directly, and the release
history is consistent with it.

Read the boundaries as:

| Bump | Meaning |
|---|---|
| MAJOR | A working deployment needs operator action: a re-hashed password, an edited config, a moved secret |
| MINOR | New commands, modules, providers, or capabilities; existing deployments keep working untouched |
| PATCH | Fixes only. No release has used this level yet |

### Where the version number lives

The version is hand-edited in several files and cross-checked by the test suite
rather than derived from one source:

| Location | Literal |
|---|---|
| `pyproject.toml` | `version = "5.0.0"` |
| `config.py - __version__` | the runtime value, re-exported by `internets.py`, `botlog.py`, `admin_cmds.py`, `console.py` |
| `docs/conf.py` | `release`, the truncated `version` (MAJOR.MINOR), and `html_title` |
| Prose | `README.md`, `config.ini.example`, and six `docs/*.md` pages |

Three gates in `tests/run_tests.py` hold these together: `VERSION: __version__
matches pyproject.toml`, `VERSION: __version__ is defined and follows semver`,
and `VERSION: every hand-written version literal in docs matches __version__`.
The third scans `README.md`, `docs/conf.py`, `docs/deployment.md`,
`docs/configuration.md`, `docs/providers.md`, and `docs/security-model.md` for
any `X.Y.Z` on a line mentioning "Internets", "version", or "release", exempting
lines that carry a comparison operator. A missed bump fails the suite instead of
shipping.

That guard's file list is hand-enumerated, which is the same maintenance shape
that shipped broken wheels (see [release-process.md](release-process.md)). A
doc page added later is not scanned unless someone adds it. This page and
[release-process.md](release-process.md) are deliberately **not** candidates for
that list: both discuss `3.0.0`, `4.0.0`, and `5.0.0` as history, exactly like
`CHANGELOG.md`, which the guard also exempts.

## What a major release has actually meant

Three majors, three different shapes of operator cost. These are the concrete
precedents for what MAJOR buys you.

### 5.0.0 (2026-07-22): admin authentication changed under you

The release preamble is explicit that it is backward-incompatible for admin
authentication. Two changes, both silent until you try to log in:

- **A bcrypt password over 72 UTF-8 bytes stops authenticating.** bcrypt ignores
  every byte past 72, so such a password was previously accepted while only its
  first 72 bytes protected the account, and any string sharing that prefix
  verified. Hashing and verification now both refuse it. If `[admin]
  password_hash` starts with `bcrypt$` and your password is longer than 72
  bytes, re-run `hashpw.py` and paste the new hash; no restart is needed,
  because the hash is re-read on every `.auth`. The only user-visible symptom is
  `wrong password.`, with `bcrypt candidate exceeds the 72-byte limit` in the
  log. Deployments on scrypt (the CLI default, `hashpw.py - hash_scrypt()`) or
  argon2 (`hashpw.py - hash_argon2()`) are unaffected.
- **The password cap became 128 UTF-8 bytes, not 128 characters.** A non-ASCII
  passphrase that fits in 128 characters can exceed 128 bytes and stops
  authenticating. Leading and trailing whitespace is also rejected at creation,
  since the dispatcher strips a command argument before it reaches `.auth` and
  such a password could never have worked over IRC.

This is the canonical example of the rule: no API changed, and an operator who
upgraded without reading the CHANGELOG was locked out of their own bot.

### 4.0.0 (2026-06-28): a config key was removed and a guard began failing closed

- **`[bot] default_location` was removed.** The key is now ignored. `.weather`
  with no saved location prompts the user to run `.regloc` instead of silently
  answering with an operator-chosen default that users mistook for their own
  weather. An operator relying on that key loses the behavior with no error: the
  key simply stops doing anything, because nothing in `config.py` validates
  unknown keys.
- **`is_admin` began failing closed on an unverifiable hostmask binding.** It
  previously granted on an `unknown` sentinel and on a missing current hostmask.
  Deployments where the server does not supply a usable hostmask lose admin
  access rather than keeping it.

4.0.0 also brought the state-file quarantine path discussed under [State
compatibility](#state-compatibility) below, which matters more for rollback than
for upgrade.

### 3.0.0 (2026-05-20): a whole secret tier was deleted

- **The OS keyring backend was removed.** `secret_store` went from three tiers
  to two: `INTERNETS_<NAME>` environment variable, then `config.ini` `[secrets]`
  at mode 0600. The stated rationale is that the bot targets headless
  deployments where `keyring` has no usable backend, and the optional desktop
  integration pulled in roughly ten transitive dependencies for no practical
  benefit. Anything stored in the OS keyring had to be moved into
  `config.ini` `[secrets]` **before** upgrading, or it was simply gone at
  runtime. The `--backend` flag on `secret_store set` / `delete` / `migrate`
  disappeared with it.
- **`config.ini` and `secrets.ini` merged.** `secrets.ini` no longer exists; its
  `[secrets]` section moved to the bottom of `config.ini`, which is now
  gitignored, with `config.ini.example` as the committed credential-free
  template. Existing installs needed a manual concatenation, documented in the
  3.0.0 entry.
- **`python -m secret_store get --reveal` was removed**, because printing a
  stored secret to stdout is a real exposure surface (scrollback, shell history,
  screen recording). The equivalent one-liner at the call site is in the 3.0.0
  entry.

The pattern across all three: what breaks is an *operator artifact* - a
password, a config key, a stored credential - never a code interface.

## Supported Python

`pyproject.toml` sets `requires-python = ">=3.10"` and carries the matching
classifiers for 3.10 through 3.14. `.github/workflows/tests.yml` runs the full
matrix, five Python versions on three operating systems:

| Axis | Values |
|---|---|
| Python | 3.10, 3.11, 3.12, 3.13, 3.14 |
| OS | `ubuntu-latest`, `macos-latest`, `windows-latest` |

with `fail-fast: false`, so one failing leg does not hide the others. The
coverage, lint, and packaging jobs pin 3.12.

3.10 is not merely the floor, it is load-bearing for the dependency lock:
`scripts/regen-lockfile.sh` refuses to run on anything else, because the lock
must be resolved on the lowest supported version for marker-gated transitive
dependencies to be captured. See [dependencies.md](dependencies.md).

:::{warning}
**Known defect.** The committed `requirements.lock` was generated on Python
3.14, which dropped `typing_extensions>=4.4`, so a `--require-hashes` install
from the lock fails on every Python below 3.13 and the Tests workflow has been
red on `main` since 2026-08-13. The supported *range* is unchanged; the
currently committed lock does not honor it. Item 6 in
[known-issues.md](known-issues.md).
:::

Dropping a Python version is a MAJOR change under the operator-cost rule: an
operator whose system interpreter is no longer in the matrix has to install a
new one.

## Config compatibility

`config.py` reads `config.ini` at import time, overlaid by `config.local.ini`
when present, through `config.py - reload_config()`. There is no schema, no
declared key inventory, and no validation pass over the file as a whole. What
that means in practice, in three cases:

**An unknown key is silently ignored.** `configparser` parses it, nothing reads
it, nothing warns. This is how a removed key degrades: after 4.0.0 removed
`[bot] default_location`, a config still carrying it parses cleanly and the key
does nothing. Adding a key to your config that the running version does not know
is equally silent. Treat the CHANGELOG as the only notification channel for a
removed key.

**A missing key is one of two things, depending on how the code reads it.**
`config.py` uses hard subscripting for the keys the bot cannot start without,
and `.get()` with a default for everything else:

| Read style | Keys | Missing behavior |
|---|---|---|
| `cfg["irc"]["server"]` and friends | `[irc]` `server`, `port`, `nickname`, `realname`; `[bot]` `command_prefix`, `api_cooldown`; `[logging]` `level`, `log_file` | `KeyError` traceback during import; the bot does not start |
| `cfg["bot"].get(...)` | `flood_cooldown` (3), `modules_dir` (`modules`), `autoload` (empty), `[logging] max_bytes` / `backup_count` / `debug_file`, the `[irc]` oper and mode fields | Documented default, no warning |

A missing whole section raises the same `KeyError` as a missing required key
inside it. Two keys get an explicit fail-closed check rather than a default: an
empty `command_prefix` raises `SystemExit` with an explanation at import, and
`api_cooldown` / `flood_cooldown` are floored at 1 second so a zero cannot
disable the rate limiter. A completely unreadable `config.ini` raises
`SystemExit` naming the path and pointing at `python -m secret_store init`.

The full per-condition table, including the module-level and `[metrics]` cases,
is [configuration.md](configuration.md#failure-summary). Do not duplicate it
here.

**Reload does not re-run the import-time validation.** `config.py -
reload_config()` re-reads both files but skips the guards above, so a `.rehash`
can install an empty `command_prefix` and turn every channel message into a
command. Item 13 in [known-issues.md](known-issues.md).

### The config compatibility contract

Stated as a rule, from observed behavior:

- Adding a key is always compatible in both directions. An older version ignores
  it; a newer version defaults it.
- Removing a key is compatible in the *file* but not in *behavior*: the file
  still parses, the behavior silently changes. This is the case that needs a
  CHANGELOG preamble, and 4.0.0 is the precedent.
- Changing the *meaning* of an existing key is the incompatible case with no
  detection at all. Nothing in the repository does this today. The proposal
  below is that it never should: rename instead, so the old name becomes a
  removal and the new name a default.

## State compatibility

Two persistence patterns, with different compatibility properties. The full
mechanics are in
[state-and-persistence.md](state-and-persistence.md#two-persistence-patterns);
what follows is only what changes between versions.

### `store.py` carries a schema version

`store.py` writes its three datasets (locations, channels, users) inside a
versioned integrity envelope built by `store.py - _wrap_v2()`:

```json
{"schema": 2, "checksum": "<sha256>", "data": {}}
```

`store.py - _unwrap()` decides what to do with what it finds:

| File shape | Result |
|---|---|
| `schema` == 2 with a matching checksum | Loaded |
| No `schema` key (legacy v1 bare payload) | Accepted, and re-written as v2 on the next flush |
| `schema` != 2 | `_StoreRejected`: the file is quarantined |
| Envelope missing or failing its checksum | `_StoreRejected`: the file is quarantined |

That is a real upgrade path in one direction only. A v1 file is silently
upgraded. A file from a hypothetical future schema 3 is not downgraded, it is
rejected.

### The module-owned stores carry nothing

`seen.json`, `tells.json`, `notes.json`, `reminders.json`, `steamids.json`, and
`shadow_bans.json` are bare JSON with an atomic write and no envelope, no
version, and no checksum. `modules/notes.py - NotesModule.on_load()` is the
representative shape: parse, coerce to the expected shape, and on any exception
log a warning and start empty. The next save then overwrites the file.

The compatibility consequence: these files have no version to disagree about, so
they never *reject* anything, and a shape they cannot parse is data loss on the
next save rather than a refusal. A module that changes its on-disk shape has no
mechanism to detect the old one, so a shape change must be made
backward-compatible in the loader itself or the data is silently discarded.
Nothing in the repository has done this yet.

### Skipping versions

Skipping forward is safe for state. There is one `store.py` schema version in
existence and a legacy pre-envelope form that is still accepted, so upgrading
from any tagged release directly to the newest reads the same files. The module
stores have no version to skip.

The risk in skipping is not the files, it is the CHANGELOG: each major carried
its own operator action, and skipping 4.0.0 to land on 5.0.0 does not skip
4.0.0's removed `[bot] default_location` or its fail-closed `is_admin`. Read
every intervening major's preamble, not only the target's.

### Rolling back

The state files are what to think about, not the code.
[deployment.md](deployment.md#rollback) has the procedure. The version-specific
fact:

:::{warning}
**Quarantine arrived in 4.0.0** (CHANGELOG 4.0.0, "Store quarantine instead of
clobber"; verified as commit `a837365`, which is reachable from `v4.0.0` and
`v5.0.0` and from no earlier tag). Before it, `store.py - Store._read()` reset
to empty on a checksum, size, shape, or parse failure and the next flush
overwrote the only on-disk copy. So a rollback **below 4.0.0** turns one bad
read into permanent loss of saved locations, channel-rejoin state, and privacy
opt-out flags - and the bot then resumes tracking users who had opted out.
Copy the state files aside before starting an older binary against them.
:::

The one-deep `<name>.bak` written by `store.py - Store._write()` arrived in the
same release, so a pre-4.0.0 binary also leaves you no backup copy.

Two further rollback facts:

- **The audit log does not roll back.** `audit_log.py - AuditLog.record()`
  appends to a single chain that `audit_log.py - AuditLog.verify()` walks from
  genesis, with no version-scoped segment boundary. Rolling the code back simply
  continues the existing chain. Note that records written by 3.0.0 and later
  carry `"v": 2` and are HMAC-keyed; `audit.log` and `audit.log.key` must travel
  together or nothing verifies.
- **Rolling back below 3.0.0 needs the secret store moved back**, because
  3.0.0 merged `secrets.ini` into `config.ini`. An older binary looks for a file
  that no longer exists and runs keyless.

## Upgrade procedure

The steps, with their verification points, are
[operations.md](operations.md#upgrade-procedure); the deployment-shape notes
(checkout versus wheel, `modules_dir` re-pointing, `.reloadall` versus restart)
are [deployment.md](deployment.md#upgrade). Do not follow a copy of them from
here.

The version-specific preflight this page adds:

- [ ] Read the CHANGELOG preamble for the target version **and every major you
      are skipping over**. That is the only channel for a removed key or a
      credential that must move first.
- [ ] If moving across 3.0.0, confirm no secret is still only in the OS keyring.
- [ ] If moving across 5.0.0 with `password_hash` starting `bcrypt$`, confirm
      the password is at or under 72 UTF-8 bytes, or re-hash before you upgrade,
      not after you lock yourself out.
- [ ] Copy the deployment directory aside. It is the only rollback path, and the
      state-file hazards above are the reason.
- [ ] Confirm the running Python is inside `requires-python` for the target.
- [ ] After starting, grep the log for `Store: <path> unusable` before assuming
      the state survived.

## Deprecation policy

:::{note}
**Proposal, not established policy.** The repository has no deprecation policy
recorded anywhere, and the removals in 3.0.0 and 4.0.0 were immediate: the
keyring backend, `--reveal`, and `[bot] default_location` all went from present
to absent in one release with a CHANGELOG entry and no intermediate warning
period. What follows is a recommended default for the maintainer to confirm,
amend, or reject.
:::

Proposed lifecycle for removing a command, a config key, or a module:

1. **Announce in the CHANGELOG** under a `Deprecated` heading in the release
   that first marks it. Keep a Changelog already defines that category and the
   project already follows that format, so this costs nothing new. Name the
   replacement and the release that will remove it.
2. **Keep it working for one MAJOR.** A thing deprecated in `X.Y.Z` keeps
   working through all of `X` and is removed in `X+1.0.0`. That gives an
   operator who upgrades only on majors exactly one release cycle of overlap.
3. **Warn at the point of use**, once, where a warning is cheap and visible: a
   `log.warning` at config load for a key, a line in the command's reply for a
   command. Silence is what made `[bot] default_location` disappear invisibly.
4. **Remove in the next MAJOR**, with a breaking-change preamble under the
   version heading in the shape 3.0.0, 4.0.0, and 5.0.0 already use: plain
   language, what breaks, the exact remedy.

Two carve-outs the proposal should keep:

- **A security removal skips the waiting period.** `--reveal` was removed
  because printing a secret to stdout is an exposure surface; holding that for a
  major would have been the wrong call. A removal that closes a real exposure
  goes in the next release with a preamble, and the policy should say so rather
  than being quietly violated.
- **A never-worked feature is not a deprecation.** Removing something that was
  built but wired to nothing is a fix, not a removal, and does not need the
  cycle.

## Support expectations

`SECURITY.md` states the supported set, and it is narrow:

| Version | Supported |
|---|---|
| `main` | yes |
| Latest tagged release | yes |
| Any earlier tag | no |

There are no backport branches. Fixes land on `main` and reach users in the next
tagged release; an older tag receives nothing. The remedy for a defect in an old
tag is to upgrade.

This is a single-maintainer project. `CODEOWNERS` is `* @brandontroidl`, one
name, and every pull request auto-requests that one reviewer. Support is
best-effort by one person, on their own time. Concretely, and stated plainly
rather than dressed up as a service level:

- There is no response-time commitment for issues or pull requests.
- `SECURITY.md` says to expect an acknowledgement of a private vulnerability
  report in about a week. That is an expectation the maintainer set, not a
  guarantee, and it is the only timing statement anywhere in the repository.
- There is no bug bounty.
- There is no release schedule. Releases happen when there is something to
  release; the five tagged releases to date span 2026-05-19 to 2026-07-22.
- Whether a finding becomes a fix is the maintainer's decision, not the
  reporter's. [known-issues.md](known-issues.md) is the register of verified
  defects that have deliberately not been changed, and it exists precisely so
  that "known and not fixed" is a visible state rather than a silent one.

An operator who needs a guaranteed response window needs to run their own fork
and accept the maintenance, or fund the arrangement separately. Neither is
offered here.

## Related reading

- [dependencies.md](dependencies.md) - the pin/floor model, the supported-Python
  coupling in the lockfile, and third-party service governance.
- [release-process.md](release-process.md) - how a release is actually cut and
  verified.
- [deployment.md](deployment.md) - install shapes, upgrade notes, rollback.
- [operations.md](operations.md#upgrade-procedure) - the step-by-step upgrade.
- [configuration.md](configuration.md) - every key and its failure behavior.
- [state-and-persistence.md](state-and-persistence.md) - the two persistence
  patterns in full.
- [known-issues.md](known-issues.md) - the verified-defect register.
