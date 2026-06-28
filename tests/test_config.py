"""Tests for config.py - config.ini loading, CLI parsing, and the
three-tier secret resolution helper.

config.py does all its work at import time: it reads config.ini (staged
in the repo root) and parses sys.argv.  Because the module parses argv on
import, we pin sys.argv to a clean value before importing it here, and the
``reimport`` fixture drives full module re-execution against crafted
temp configs (chdir + importlib.reload), restoring real state on teardown.

Only true externals are mocked: secret_store.get for the tier-1 path of
_secret_or_cfg.  Everything else exercises the real parser and real files.
"""

from __future__ import annotations

import configparser
import importlib
import os
import re
import sys
from pathlib import Path

import pytest

# config.py calls argparse.parse_args() at import.  Pin argv to something
# argparse accepts before importing, then restore so we don't disturb the
# rest of the pytest session.
_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import config  # noqa: E402
import secret_store  # noqa: E402
sys.argv = _SAVED_ARGV


# A minimal-but-complete config.ini that satisfies every key config.py
# reads at import.  Values are deliberately distinct so assertions can't
# pass by coincidence with the real staged config.
MINIMAL_CFG = """\
[irc]
server = irc.test.example
port = 6667
nickname = TestBot
realname = Test Real Name
oper_name =   opername
user_modes =   +iw
oper_modes =   +x
oper_snomask = +cF
[bot]
command_prefix = !
api_cooldown = 0
flood_cooldown = -5
modules_dir = mymods
autoload = a, b ,,c
[logging]
level = warning
log_file = test.log
max_bytes = 1234
backup_count = 7
debug_file =   dbg.log
"""


@pytest.fixture
def reimport():
    """Re-execute config.py fresh against a temp working dir.

    Yields ``load(workdir, argv=None)`` which chdirs into ``workdir``
    (so ``Path('config.ini').resolve()`` points there), sets argv, and
    reloads the module.  On teardown, restores the real cwd/argv and
    reloads config.py against the repo's staged config.ini so other
    tests (and the rest of the suite) see the real module state.
    """
    orig_cwd = os.getcwd()
    orig_argv = sys.argv

    def load(workdir, argv=None):
        os.chdir(str(workdir))
        sys.argv = list(argv) if argv is not None else ["internets"]
        return importlib.reload(config)

    yield load

    os.chdir(orig_cwd)
    sys.argv = ["internets"]
    importlib.reload(config)
    sys.argv = orig_argv


def _write_cfg(workdir, text=MINIMAL_CFG):
    p = Path(workdir) / "config.ini"
    p.write_text(text, encoding="utf-8")
    return p


# ── __version__ ──────────────────────────────────────────────────────────

class TestVersion:
    def test_is_semver_string(self):
        assert isinstance(config.__version__, str)
        assert re.fullmatch(r"\d+\.\d+\.\d+", config.__version__)

    def test_version_matches_cli_program_name(self):
        # The --version action embeds __version__; assert the contract that
        # the constant is the single source the CLI advertises.
        assert config.__version__ == "4.0.0"


# ── _secret_or_cfg three-tier resolution ─────────────────────────────────

class TestSecretOrCfg:
    def test_tier1_secret_store_wins(self, monkeypatch):
        # secret_store has a value -> returned verbatim, cfg never consulted.
        monkeypatch.setattr(secret_store, "get", lambda name: "from_secret")
        bomb = configparser.ConfigParser()
        bomb.add_section("irc")
        bomb.set("irc", "server_password", "from_cfg")
        monkeypatch.setattr(config, "cfg", bomb)
        assert config._secret_or_cfg(
            "server_password", "irc", "server_password"
        ) == "from_secret"

    def test_tier2_cfg_value_when_secret_empty(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name: "")
        p = configparser.ConfigParser()
        p.add_section("irc")
        p.set("irc", "server_password", "  cfgval  ")  # surrounding ws
        monkeypatch.setattr(config, "cfg", p)
        # Falls back to cfg AND strips whitespace.
        assert config._secret_or_cfg(
            "server_password", "irc", "server_password"
        ) == "cfgval"

    def test_tier3_default_when_neither_present(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name: "")
        p = configparser.ConfigParser()
        p.add_section("irc")  # option absent
        monkeypatch.setattr(config, "cfg", p)
        assert config._secret_or_cfg(
            "server_password", "irc", "server_password", default="zzz"
        ) == "zzz"

    def test_tier3_default_empty_by_default(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name: "")
        p = configparser.ConfigParser()
        monkeypatch.setattr(config, "cfg", p)  # no sections at all
        assert config._secret_or_cfg("k", "nosuch", "key") == ""

    def test_secret_value_not_stripped(self, monkeypatch):
        # Tier-1 secret is returned as-is (secret_store owns its own
        # normalization); only the cfg fallback path strips.
        monkeypatch.setattr(secret_store, "get", lambda name: "  spaced  ")
        assert config._secret_or_cfg("k", "irc", "key") == "  spaced  "


# ── reload_config merge semantics ────────────────────────────────────────

class TestReloadConfig:
    def test_reads_base_only_when_no_local(self, tmp_path, monkeypatch):
        base = tmp_path / "config.ini"
        base.write_text("[bot]\ncommand_prefix = .\n", encoding="utf-8")
        fresh = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        monkeypatch.setattr(config, "cfg", fresh)
        monkeypatch.setattr(config, "CONFIG_PATH", str(base))
        monkeypatch.setattr(config, "_LOCAL_CONFIG", tmp_path / "absent.ini")
        files = config.reload_config()
        assert files == [str(base)]
        assert fresh.get("bot", "command_prefix") == "."

    def test_local_overlay_overrides_and_preserves(self, tmp_path, monkeypatch):
        base = tmp_path / "config.ini"
        base.write_text(
            "[bot]\ncommand_prefix = .\napi_cooldown = 10\n"
            "[admin]\npassword_hash =\n",
            encoding="utf-8",
        )
        local = tmp_path / "config.local.ini"
        local.write_text(
            "[bot]\ncommand_prefix = !\n"      # overrides base
            "[admin]\npassword_hash = argon2hash\n",  # fills a base placeholder
            encoding="utf-8",
        )
        fresh = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        monkeypatch.setattr(config, "cfg", fresh)
        monkeypatch.setattr(config, "CONFIG_PATH", str(base))
        monkeypatch.setattr(config, "_LOCAL_CONFIG", local)
        files = config.reload_config()
        assert files == [str(base), str(local)]
        # Overlay wins on a shared key.
        assert fresh.get("bot", "command_prefix") == "!"
        # Base-only key untouched by the overlay.
        assert fresh.get("bot", "api_cooldown") == "10"
        # Overlay supplied a value the base left blank.
        assert fresh.get("admin", "password_hash") == "argon2hash"

    def test_second_reload_does_not_clobber_local_value(self, tmp_path, monkeypatch):
        # The documented hazard: re-reading config.ini alone would wipe a
        # value that only config.local.ini set.  reload_config re-reads
        # BOTH, so the local value must survive repeated reloads.
        base = tmp_path / "config.ini"
        base.write_text("[admin]\npassword_hash =\n", encoding="utf-8")
        local = tmp_path / "config.local.ini"
        local.write_text("[admin]\npassword_hash = keepme\n", encoding="utf-8")
        fresh = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        monkeypatch.setattr(config, "cfg", fresh)
        monkeypatch.setattr(config, "CONFIG_PATH", str(base))
        monkeypatch.setattr(config, "_LOCAL_CONFIG", local)
        config.reload_config()
        config.reload_config()  # second pass must not clobber
        assert fresh.get("admin", "password_hash") == "keepme"

    def test_reads_utf8_box_drawing_headers(self, tmp_path, monkeypatch):
        # config.ini.example uses non-ASCII box-drawing chars in comments;
        # the read is pinned to UTF-8 so this must not raise.
        base = tmp_path / "config.ini"
        base.write_text(
            "; ──────── section ────────\n[bot]\ncommand_prefix = .\n",
            encoding="utf-8",
        )
        fresh = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        monkeypatch.setattr(config, "cfg", fresh)
        monkeypatch.setattr(config, "CONFIG_PATH", str(base))
        monkeypatch.setattr(config, "_LOCAL_CONFIG", tmp_path / "absent.ini")
        files = config.reload_config()
        assert files == [str(base)]


# ── Real staged config.ini constants (import-time parse) ─────────────────

class TestStagedConstants:
    def test_irc_constants(self):
        assert config.SERVER == "irc.example.org"
        assert config.PORT == 6697
        assert isinstance(config.PORT, int)
        assert config.NICKNAME == "Internets"
        assert config.REALNAME == "IRC Bot"

    def test_bot_constants(self):
        assert config.CMD_PREFIX == "."
        assert config.API_CD == 10
        assert config.FLOOD_CD == 3
        assert config.MODULES_DIR == Path("modules")
        assert isinstance(config.MODULES_DIR, Path)

    def test_autoload_is_clean_list(self):
        assert isinstance(config.AUTO_LOAD, list)
        assert "weather" in config.AUTO_LOAD
        # No empty/whitespace entries leaked through the split/strip/filter.
        assert all(m and m == m.strip() for m in config.AUTO_LOAD)

    def test_user_modes_stripped(self):
        assert config.USER_MODES == "+iwx"

    def test_desired_caps_contents(self):
        assert "sasl" in config.DESIRED_CAPS
        assert "message-tags" in config.DESIRED_CAPS
        assert isinstance(config.DESIRED_CAPS, set)

    def test_logging_constants(self):
        assert config.LOG_LEVEL == "INFO"
        assert config.LOG_FILE == "internets.log"
        assert config.LOG_MAX == 5242880
        assert config.LOG_BACKUPS == 3
        assert "%(levelname)s" in config.LOG_FMT

    def test_config_path_is_absolute(self):
        assert os.path.isabs(config.CONFIG_PATH)
        assert config.CONFIG_PATH.endswith("config.ini")


# ── Full re-execution against crafted configs ────────────────────────────

class TestReimportParsing:
    def test_custom_values_parsed(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.SERVER == "irc.test.example"
        assert m.PORT == 6667
        assert m.NICKNAME == "TestBot"
        assert m.REALNAME == "Test Real Name"
        assert m.MODULES_DIR == Path("mymods")

    def test_oper_fields_stripped(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.OPER_N == "opername"
        assert m.USER_MODES == "+iw"
        assert m.OPER_MODES == "+x"
        assert m.OPER_SNOMASK == "+cF"

    def test_cooldowns_floored_at_one(self, tmp_path, reimport):
        # api_cooldown=0 and flood_cooldown=-5 must clamp to 1 so the rate
        # limiter cannot be silently disabled via config.
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.API_CD == 1
        assert m.FLOOD_CD == 1

    def test_autoload_split_strip_filter(self, tmp_path, reimport):
        # "a, b ,,c" -> ['a','b','c'] (stripped, empties dropped).
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.AUTO_LOAD == ["a", "b", "c"]

    def test_loglevel_uppercased_from_config(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.LOG_LEVEL == "WARNING"

    def test_log_overrides_from_config(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.LOG_FILE == "test.log"
        assert m.LOG_MAX == 1234
        assert m.LOG_BACKUPS == 7
        assert m.LOG_DEBUG == "dbg.log"

    def test_flood_cooldown_defaults_when_absent(self, tmp_path, reimport):
        cfg_no_flood = MINIMAL_CFG.replace("flood_cooldown = -5\n", "")
        _write_cfg(tmp_path, cfg_no_flood)
        m = reimport(tmp_path)
        # default "3" -> max(1,3) == 3
        assert m.FLOOD_CD == 3

    def test_modules_dir_defaults_when_absent(self, tmp_path, reimport):
        cfg_no_mods = MINIMAL_CFG.replace("modules_dir = mymods\n", "")
        _write_cfg(tmp_path, cfg_no_mods)
        m = reimport(tmp_path)
        assert m.MODULES_DIR == Path("modules")

    def test_autoload_empty_when_absent(self, tmp_path, reimport):
        cfg_no_auto = MINIMAL_CFG.replace("autoload = a, b ,,c\n", "")
        _write_cfg(tmp_path, cfg_no_auto)
        m = reimport(tmp_path)
        assert m.AUTO_LOAD == []


# ── Fail-closed guards (SystemExit) ──────────────────────────────────────

class TestGuards:
    def test_missing_config_raises_systemexit(self, tmp_path, reimport):
        # Empty dir: no config.ini -> read_files empty -> actionable exit.
        with pytest.raises(SystemExit) as exc:
            reimport(tmp_path)
        msg = str(exc.value)
        assert "config.ini not found" in msg

    def test_empty_command_prefix_raises_systemexit(self, tmp_path, reimport):
        bad = MINIMAL_CFG.replace("command_prefix = !", "command_prefix =")
        _write_cfg(tmp_path, bad)
        with pytest.raises(SystemExit) as exc:
            reimport(tmp_path)
        assert "command_prefix" in str(exc.value)


# ── CLI argument parsing ─────────────────────────────────────────────────

class TestCliArgs:
    def test_loglevel_override_beats_config(self, tmp_path, reimport):
        _write_cfg(tmp_path)  # config level = warning
        m = reimport(tmp_path, argv=["internets", "--loglevel", "error"])
        assert m.LOG_LEVEL == "ERROR"

    def test_debug_file_override_beats_config(self, tmp_path, reimport):
        _write_cfg(tmp_path)  # config debug_file = dbg.log
        m = reimport(tmp_path, argv=["internets", "--debug-file", "/var/x.log"])
        assert m.LOG_DEBUG == "/var/x.log"

    def test_debug_subsystems_parsed(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path, argv=["internets", "--debug", "weather", "store"])
        assert m.cli_args.debug == ["weather", "store"]

    def test_debug_global_empty_list(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path, argv=["internets", "--debug"])
        assert m.cli_args.debug == []

    def test_debug_absent_is_none(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path, argv=["internets"])
        assert m.cli_args.debug is None

    def test_no_console_flag(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path, argv=["internets", "--no-console"])
        assert m.cli_args.no_console is True

    def test_no_console_default_false(self, tmp_path, reimport):
        _write_cfg(tmp_path)
        m = reimport(tmp_path)
        assert m.cli_args.no_console is False
