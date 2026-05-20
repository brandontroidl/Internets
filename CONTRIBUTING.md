# Contributing to Internets

Thanks for your interest in contributing. This document covers the basics.

## Getting Started

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
pip install requests
python tests/run_tests.py        # should report 0 failures
```

A virtual environment is recommended but not required (the only runtime
dependency is `requests`):

```bash
python -m venv .venv
source .venv/bin/activate         # Linux/macOS
.venv\Scripts\activate            # Windows
pip install requests
```

For full local development (all optional extras + pytest):

```bash
pip install -e ".[all,dev]"
```

Before running the bot locally, set up your secrets and overlay config
(see README "Setup"):

```bash
python -m secret_store init       # secrets.ini.example -> secrets.ini (0600)
$EDITOR secrets.ini               # paste real values, or use `secret_store set`
$EDITOR config.local.ini          # server, nick, admin password_hash, etc.
```

`config.ini` is the committed credential-free template — do not paste
real values there. `secrets.ini` and `config.local.ini` are gitignored.

## Running Tests

**Standalone suite** — no external dependencies beyond `requests`:

```bash
python tests/run_tests.py
```

**pytest suite** — more detailed output, parallel execution, and IDE integration:

```bash
pip install pytest
pytest tests/ -v
```

Both suites must pass. Tests must work on Python 3.10+ across Linux, macOS,
and Windows. The GitHub Actions CI matrix covers all three.

## Writing a Module

See the "Writing a Module" section in README.md. The short version:

1. Create `modules/yourmodule.py`.
2. Subclass `BotModule`, define `COMMANDS`, implement async handlers.
3. Override `is_configured()` if the module needs an API key, so
   `.help` hides it cleanly when the key isn't set.
4. Pull credentials via `modules.base.cred(cfg, "<secret_name>",
   "<section>", "<key>")` so the secret store always wins.
5. Add tests to `tests/run_tests.py` under a new section header.
6. Add matching pytest tests in `tests/test_yourmodule.py` if appropriate.
7. Add a brief entry to `CHANGELOG.md` under `[Unreleased]`.

## Privacy

Contributors must not add new fields to `users.json` or `locations.json`
(or any other store dataset that pivots on user identity) without
updating `PRIVACY.md` to disclose the new field **and** adding a
matching purge path to `.forgetme` in `modules/privacy.py`. The same
applies to new third-party data flows that include user-supplied
content — document them in `PRIVACY.md` under "Third-party data flow".
PRs that touch user data without those two changes will be sent back.

## Code Style

- Python 3.10+ syntax (use `X | Y` unions, not `Optional`).
- Type annotations on all public function signatures.
- Docstrings on all public classes and functions.
- No `eval()`, `exec()`, or `__import__()` in module code.
- All command handlers must be `async def`.
- Blocking I/O goes through `asyncio.to_thread()`.
- Imports at the top of the file (stdlib -> third-party -> local).
- Use `threading.Lock` for shared mutable state touched by both the
  event loop and worker threads. Do not rely on the GIL.
- Never read credentials from `config.ini` directly — use
  `modules.base.cred()` so env / keyring / `secrets.ini` are honored.
- Never log secret values. Module `on_load()` may log presence only.

## Submitting Changes

1. Fork the repo and create a feature branch.
2. Make your changes. Keep commits focused.
3. Add or update tests. Run both `python tests/run_tests.py` and
   `pytest tests/ -v` — 0 failures on both.
4. Update `CHANGELOG.md` under an `[Unreleased]` heading.
5. Open a pull request against `main`.

## Security Issues

If you find a security vulnerability, do **not** open a public issue.
Use GitHub's private vulnerability reporting on the repository
(`Security` tab on the GitHub repo page), or contact the maintainer
through the address listed on the GitHub profile linked from
`pyproject.toml`.

## License

By contributing you agree that your contributions will be licensed under
the ISC License (see LICENSE).
