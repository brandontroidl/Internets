# Pull request

## Summary

A short description of what this PR changes and why. Focus on the
motivation — reviewers can read the diff for the mechanics.

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

- [ ] Tests added or updated; both `pytest -q tests/` and
      `python tests/run_tests.py` pass locally.
- [ ] Documentation updated where relevant (`README.md`,
      `CHANGELOG.md`, or inline docstrings).
- [ ] No credentials committed to `config.ini` — the shipped template
      stays credential-free.
- [ ] Any new module that depends on an API key overrides
      `is_configured()` so it is skipped cleanly when the key is absent.
- [ ] Any new credential is read via `modules.base.cred()` **and**
      registered in `secret_store.KNOWN_SECRETS` so log redaction
      covers it.
- [ ] If this change alters data the bot stores, `PRIVACY.md` is
      updated and `.forgetme` (or the relevant purge path) removes the
      new data on request.
- [ ] `python -m py_compile <file>` is clean on every file touched by
      this PR.

## Screenshots or IRC transcript (optional)

If the change is user-visible — new command output, formatting, etc. —
paste a short transcript or screenshot here. Scrub nicks, hostmasks, and
channel names you do not want public.
