# Contributing to Internets

Thanks for your interest in contributing. This document covers the basics.

## Getting Started

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
pip install requests
python tests/run_tests.py        # should report 0 failures
```

No virtual environment is strictly required (the only runtime dependency is
`requests`), but using one is recommended:

```bash
python -m venv .venv
source .venv/bin/activate         # Linux/macOS
.venv\Scripts\activate            # Windows
pip install requests
```

## Running Tests

The standalone test suite has no external dependencies beyond `requests`:

```bash
python tests/run_tests.py
```

If you have `pytest` installed the same tests also run under it:

```bash
pip install pytest
pytest tests/ -v
```

Tests must pass on Python 3.10+ across Linux, macOS, and Windows.  The GitHub
Actions CI matrix covers all three.

## Writing a Module

See the "Writing a Module" section in README.md.  The short version:

1. Create `modules/yourmodule.py`.
2. Subclass `BotModule`, define `COMMANDS`, implement async handlers.
3. Add tests to `tests/run_tests.py` under a new section header.
4. Add a brief entry to `CHANGELOG.md`.

## Code Style

- Python 3.10+ syntax (use `X | Y` unions, not `Optional`).
- Type annotations on all public function signatures.
- Docstrings on all public classes and functions.
- No `eval()`, `exec()`, or `__import__()` in module code.
- All command handlers must be `async def`.
- Blocking I/O must go through `asyncio.to_thread()`.
- Keep imports at the top of the file (stdlib → third-party → local).

## Submitting Changes

1. Fork the repo and create a feature branch.
2. Make your changes.  Keep commits focused.
3. Add or update tests.  Run `python tests/run_tests.py` — 0 failures.
4. Update `CHANGELOG.md` under an `[Unreleased]` heading.
5. Open a pull request against `main`.

## Security Issues

If you find a security vulnerability, please **do not** open a public issue.
Email the maintainer directly (see `config.ini` for contact info in the
`user_agent` field) or use GitHub's private vulnerability reporting.

## License

By contributing you agree that your contributions will be licensed under the
MIT License (see LICENSE).
