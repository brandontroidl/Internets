# Pull request

## Summary

A short description of what this PR changes and why. Focus on the
motivation - reviewers can read the diff for the mechanics.

## Linked issue

Fixes #

(Use `Refs #` if the PR only partially addresses an issue, or remove
this section if there is no linked issue.)

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor (no behaviour change)
- [ ] Tests
- [ ] Security fix

## Checklist

Always:

- [ ] Tests added or updated; both `python -m pytest tests/ -q` and
      `python tests/run_tests.py` pass locally.
- [ ] `python -m py_compile <file>` is clean on every file touched.
- [ ] No credentials committed. The shipped `config.ini.example` stays
      credential-free, and `config.ini` / `config.local.ini` are never
      committed.
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if the change is
      user-visible or operator-visible.

If this PR touches a module or a command:

- [ ] Any third-party or user-supplied text spliced into an IRC line
      goes through `modules.base.strip_ctrl`.
- [ ] Outbound HTTP uses `modules.base.fetch_json` or an explicit
      stream-and-cap loop - never a bare `r.json()` or `r.text`.
- [ ] Handler gates are in the house order: authorization, usage,
      cooldown, work.
- [ ] Any new credential is read via `modules.base.cred()` and
      registered in `secret_store.KNOWN_SECRETS` (plus
      `CONFIG_LOCATIONS` if `migrate` should relocate it). Note this
      does not add it to log redaction, which matches on credential
      verbs, so also confirm the value cannot reach a log line or a
      composed reply.
- [ ] Any new module that depends on an API key overrides
      `is_configured()` so it is skipped cleanly when the key is absent.
- [ ] If the change stores new data about users, the owning module
      overrides `forget(nick)` so `.forgetme` erases it, and
      `PRIVACY.md` plus `docs/state-and-persistence.md` are updated to
      match.
- [ ] If a command was added, renamed, or removed:
      `scripts/gen-command-reference.py --check docs/command-reference.md`
      passes.

If this PR touches source that the documentation cites:

- [ ] `scripts/verify-doc-citations.py` passes.

If this PR touches dependencies:

- [ ] `scripts/regen-lockfile.sh` was run **on Python 3.10**, and
      `requirements.txt` and `requirements.lock` are committed together.

If this PR touches packaging (`pyproject.toml` `py-modules`,
`packages.find`, the entry point, or the module layout):

- [ ] `scripts/verify_install.sh` exits 0.

## Screenshots or IRC transcript (optional)

If the change is user-visible - new command output, formatting, etc. -
paste a short transcript or screenshot here. Scrub nicks, hostmasks, and
channel names you do not want public.
