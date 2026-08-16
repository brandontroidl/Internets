#!/usr/bin/env python3
"""Generate the user command inventory from actual command registration.

Documentation that hand-lists commands drifts the moment a module adds one.
This walks every module under modules/, instantiates each BotModule subclass
without running __init__ (the same technique tests/test_help.py uses, so no
network, keys, or config are needed), and emits one Markdown table row per
module: module name, primary commands (one per handler, aliases folded in),
and admin-only core commands from AdminCommandsMixin._CORE.

Usage:
    scripts/gen-command-reference.py            # Markdown to stdout
    scripts/gen-command-reference.py --check FILE
        exit 1 if FILE does not contain every primary command name
        (drift check for docs; run from repo root)
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_SAVED_ARGV = sys.argv
sys.argv = ["internets"]  # config.py parses argv at import time
from modules.base import BotModule            # noqa: E402
from admin_cmds import AdminCommandsMixin     # noqa: E402
sys.argv = _SAVED_ARGV

_SKIP = {"__init__", "base", "geocode", "units"}


def module_classes():
    for path in sorted(pathlib.Path("modules").glob("*.py")):
        if path.stem in _SKIP:
            continue
        mod = importlib.import_module(f"modules.{path.stem}")
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (issubclass(obj, BotModule) and obj is not BotModule
                    and obj.__module__ == mod.__name__):
                yield path.stem, obj
                break


def primary_commands(cls) -> dict[str, list[str]]:
    """Map primary command -> aliases (first registration wins as primary)."""
    prim: dict[str, list[str]] = {}
    by_method: dict[str, str] = {}
    for cmd, method in (getattr(cls, "COMMANDS", {}) or {}).items():
        if method in by_method:
            prim[by_method[method]].append(cmd)
        else:
            by_method[method] = cmd
            prim[cmd] = []
    return prim


def core_commands() -> tuple[list[str], list[str]]:
    pub = sorted(AdminCommandsMixin._CORE_PUBLIC)
    return pub, AdminCommandsMixin._core_admin_cmds()


def emit_markdown() -> str:
    out = ["| Module | Commands (aliases) |", "| --- | --- |"]
    total = 0
    for stem, cls in module_classes():
        prim = primary_commands(cls)
        total += len(prim)
        cells = []
        for cmd, aliases in sorted(prim.items()):
            cells.append(f"`.{cmd}`" + (f" (`.{'`, `.'.join(aliases)}`)" if aliases else ""))
        out.append(f"| {stem} | {', '.join(cells)} |")
    pub, adm = core_commands()
    out.append(f"| core (public) | {', '.join(f'`.{c}`' for c in pub)} |")
    out.append(f"| core (admin) | {', '.join(f'`.{c}`' for c in adm)} |")
    out.append("")
    out.append(f"Primary module commands: {total}. Core public: {len(pub)}. "
               f"Core admin: {len(adm)}.")
    return "\n".join(out)


def check(doc_path: str) -> int:
    text = pathlib.Path(doc_path).read_text(encoding="utf-8")
    missing: list[str] = []
    for stem, cls in module_classes():
        for cmd in primary_commands(cls):
            if f".{cmd}" not in text:
                missing.append(f"{stem}:.{cmd}")
    pub, adm = core_commands()
    for cmd in pub + adm:
        if f".{cmd}" not in text:
            missing.append(f"core:.{cmd}")
    if missing:
        print(f"MISSING from {doc_path}: {', '.join(missing)}")
        return 1
    print(f"{doc_path}: all registered commands present")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        raise SystemExit(check(sys.argv[2]))
    print(emit_markdown())
