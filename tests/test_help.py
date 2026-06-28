"""Regression tests for the .help system across every module.

Guards the whole help surface, not one module:
  * every primary command (one per handler method) is documented in
    help_lines() - catches "added to COMMANDS, forgot the help line";
  * help lines are IRC-safe (well under the 512-byte line limit, correctly
    indented) so they never get truncated or mis-rendered;
  * alias separators are normalized (no spaced " / ." form);
  * the shared help_row() formatter behaves.

Output is also flood-safe by construction: all replies go through
sender.py's token bucket (5 burst, ~40/min), comfortably inside a 10/3
network limit - see test_help_row_is_compact for the per-line bound.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
from configparser import ConfigParser
from unittest.mock import MagicMock

import pytest

import weather_providers as _wp
from modules.base import BotModule, help_row

# Configure the provider registry so weather.help_lines() (which reads the
# dispatcher for an active-provider count) works without network/keys.
_wp.configure(ConfigParser())

_SKIP = {"__init__", "base", "geocode", "units"}
_MODFILES = sorted(
    p.stem for p in pathlib.Path("modules").glob("*.py") if p.stem not in _SKIP
)


def _module_class(stem: str):
    mod = importlib.import_module(f"modules.{stem}")
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if (issubclass(obj, BotModule) and obj is not BotModule
                and obj.__module__ == mod.__name__):
            return obj
    return None


def _help(stem: str):
    cls = _module_class(stem)
    if cls is None:
        return None, []
    inst = cls.__new__(cls)        # skip __init__ - help_lines only needs prefix
    inst.bot = MagicMock()         # cover any module that reads self.bot
    return cls, cls.help_lines(inst, ".")


def _primary_cmds(cls) -> set[str]:
    """One representative command per handler method (the documented form)."""
    prim: dict[str, str] = {}
    for cmd, method in (getattr(cls, "COMMANDS", {}) or {}).items():
        prim.setdefault(method, cmd)
    return set(prim.values())


def _documented(cmd: str, lines: list[str]) -> bool:
    p = "."
    text = "\n".join(lines) + "\n"
    if f"{p}{cmd} " in text or f"{p}{cmd}/" in text or f"{p}{cmd}\n" in text:
        return True
    return any(ln.lstrip().split(None, 1)[0:1] == [f"{p}{cmd}"] for ln in lines)


# ── shared formatter ─────────────────────────────────────────────────────

class TestHelpRow:
    def test_prepends_prefix_and_indent(self):
        out = help_row(".", "advice", "Random advice")
        assert out.startswith("  .advice")
        assert out.rstrip().endswith("Random advice")

    def test_short_usage_is_padded(self):
        out = help_row(".", "x", "desc")
        # usage column padded so descriptions align across a module's rows.
        assert out.startswith("  .x ")
        assert out.endswith("desc")

    def test_long_usage_falls_back_to_single_space(self):
        out = help_row(".", "regloc/.register_location <zip|city>", "Save it")
        assert out.endswith(" Save it")

    def test_help_row_is_compact(self):
        # Even a generous usage+desc stays far under the IRC line limit.
        out = help_row(".", "remind <when> <msg>", "Schedule a reminder " * 3)
        assert len(out.encode("utf-8")) < 450


# ── per-module guards ────────────────────────────────────────────────────

@pytest.mark.parametrize("stem", _MODFILES)
class TestModuleHelp:
    def test_all_primary_commands_documented(self, stem):
        cls, lines = _help(stem)
        if cls is None:
            pytest.skip("no BotModule subclass")
        missing = sorted(c for c in _primary_cmds(cls) if not _documented(c, lines))
        assert not missing, f"{stem}: undocumented command(s) in help_lines: {missing}"

    def test_lines_are_irc_safe(self, stem):
        cls, lines = _help(stem)
        if cls is None:
            pytest.skip("no BotModule subclass")
        for ln in lines:
            assert len(ln.encode("utf-8")) <= 450, f"{stem}: help line too long: {ln!r}"
            assert ln.startswith("  "), f"{stem}: help line not indented: {ln!r}"
            assert "\n" not in ln and "\r" not in ln, f"{stem}: embedded newline: {ln!r}"

    def test_no_spaced_alias_separator(self, stem):
        # Aliases must read ".cmd/.alias", never ".cmd / .alias".
        cls, lines = _help(stem)
        if cls is None:
            pytest.skip("no BotModule subclass")
        for ln in lines:
            assert " / ." not in ln, f"{stem}: spaced alias separator: {ln!r}"
