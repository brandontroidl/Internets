# Contributing

Thanks for taking the time to look at this project.

## Local setup

```bash
git clone https://github.com/brandontroidl/Internets.git
cd Internets
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"            # adds pytest, coverage, bandit, pip-audit, build
```

## Running tests

Two suites, both must pass:

```bash
python tests/run_tests.py          # stdlib-only smoke suite
pytest -q tests/                   # full pytest suite
```

## Code style

- Target Python 3.10+; use `from __future__ import annotations` and PEP 604 unions (`X | Y`, not `Union[X, Y]`).
- Async-first: every command handler is a coroutine.  Blocking I/O (`requests`, disk, password hashing) must run via `await asyncio.to_thread(...)`.
- Shared mutable state is protected by a `threading.Lock` — see `store.py` / `sender.py` for the pattern.
- Never read API keys, NickServ passwords, or any other credential directly from `cfg[...]`.  Always route through `modules.base.cred(cfg, name, section, key)` so the secret store wins.
- Never log credential values.  The sender already redacts outbound IRC commands (`PASS`, `NS IDENTIFY`, `OPER`, `AUTHENTICATE`); module code must follow the same discipline.

## Module checklist

Every new module under `modules/`:

- Inherits from `modules.base.BotModule`.
- Defines `COMMANDS: dict[str, str]` mapping command word → method name.
- Overrides `is_configured()` if it needs an API key — the bot's `.help` hides modules where this returns `False`.
- Exposes a top-level `setup(bot) -> ModuleClass` function.
- Adds its key to `secret_store.KNOWN_SECRETS` if it requires a credential.

## Pull requests

- Open against `main`.
- Include a one-line summary; link any related issue with `Fixes #N`.
- CI (`.github/workflows/tests.yml` + `security.yml`) must be green.
- Touched code must be `py_compile`-clean.

## Reporting bugs or security issues

- General bugs / feature requests → open an issue using the templates in `.github/ISSUE_TEMPLATE/`.
- Security vulnerabilities → use GitHub's Private Vulnerability Reporting (Security tab of the repo).  Do NOT open a public issue.
