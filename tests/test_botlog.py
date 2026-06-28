"""Tests for botlog.py - safe formatter, debug filter, validation, helpers.

botlog runs startup validation at import time.  The staged config.ini has an
empty password_hash (auth disabled) and valid IRC modes, so a plain `import
botlog` succeeds; these tests exercise the units in-process against that
already-imported module.  The two import-time `sys.exit(1)` guards that fire
on a *bad* config (bad mode string, bad hash prefix) are driven through the
real code: the hash guard via the `_validate_hash()` function in-process, the
mode guard via a fresh subprocess import with a tampered config value.
"""

from __future__ import annotations

import os
import sys
import logging
import subprocess

import pytest

# config.py parses sys.argv at import time; pytest's own argv (test paths, -q)
# would make its argparse abort.  Neutralize argv to just the prog name before
# importing botlog (which imports config).  Real CLI flags are exercised in the
# subprocess tests below, not here.
_saved_argv = sys.argv
sys.argv = [sys.argv[0]]
try:
    import botlog
finally:
    sys.argv = _saved_argv


# ── helpers ──────────────────────────────────────────────────────────────

def _record(msg, args=(), name="internets.test", level=logging.INFO):
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class _Collector:
    """Stand-in for the `reply` callback; records every line."""
    def __init__(self):
        self.lines = []

    def __call__(self, s):
        self.lines.append(s)

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def clean_filter():
    """Snapshot the shared module log_filter and restore it afterwards.

    apply_debug / apply_loglevel mutate the process-global botlog.log_filter;
    isolate each test so order doesn't matter and the suite doesn't leak state.
    """
    f = botlog.log_filter
    saved = (f.base_level, f.global_debug, f.active_subsystems())
    f.set_base_level(logging.INFO)
    f.global_debug = False
    f.clear_subsystems()
    yield f
    f.set_base_level(saved[0])
    f.global_debug = saved[1]
    f.clear_subsystems()
    for s in saved[2]:
        f.add_subsystem(s)


# ── _SafeFormatter ───────────────────────────────────────────────────────

class TestSafeFormatterClean:
    def test_clean_strips_c0_controls(self):
        fmt = botlog._SafeFormatter("%(message)s")
        # NUL, BEL, CR, LF all stripped; surrounding text kept.
        assert fmt._clean("a\x00b\x07c\rd\ne") == "abcde"

    def test_clean_preserves_tab(self):
        fmt = botlog._SafeFormatter("%(message)s")
        assert fmt._clean("col1\tcol2") == "col1\tcol2"

    def test_clean_strips_del_and_c1(self):
        fmt = botlog._SafeFormatter("%(message)s")
        # 0x7f DEL, 0x80 and 0x9f C1 controls.
        assert fmt._clean("x\x7fy\x80z\x9fw") == "xyzw"

    def test_clean_strips_esc(self):
        fmt = botlog._SafeFormatter("%(message)s")
        # 0x1b ESC (CSI injection vector) removed.
        assert fmt._clean("\x1b[31mred\x1b[0m") == "[31mred[0m"

    def test_clean_passes_through_non_str(self):
        fmt = botlog._SafeFormatter("%(message)s")
        assert fmt._clean(123) == 123
        assert fmt._clean(None) is None
        assert fmt._clean(b"\x00") == b"\x00"  # bytes is not str -> untouched


class TestSafeFormatterFormat:
    def test_format_sanitizes_plain_msg(self):
        fmt = botlog._SafeFormatter("%(message)s")
        out = fmt.format(_record("hello\nworld\x00!"))
        assert out == "helloworld!"

    def test_format_sanitizes_tuple_args(self):
        fmt = botlog._SafeFormatter("%(message)s")
        # The attacker-controlled %s argument carries CR/LF/NUL.
        out = fmt.format(_record("cmd: %s", ("a\r\nb\x00c",)))
        assert out == "cmd: abc"

    def test_format_sanitizes_dict_args(self):
        fmt = botlog._SafeFormatter("%(message)s")
        # A single mapping arg must be wrapped in a 1-tuple so LogRecord
        # stores it as record.args (its dict-unwrap special case).
        rec = _record("user %(who)s", ({"who": "ev\nil\x1b"},))
        out = fmt.format(rec)
        assert out == "user evil"

    def test_format_keeps_numeric_args(self):
        fmt = botlog._SafeFormatter("%(message)s")
        out = fmt.format(_record("n=%d", (42,)))
        assert out == "n=42"

    def test_format_does_not_mutate_original_record(self):
        fmt = botlog._SafeFormatter("%(message)s")
        rec = _record("x\ny %s", ("a\nb",))
        fmt.format(rec)
        # Other handlers must still see the raw, unsanitized record.
        assert rec.msg == "x\ny %s"
        assert rec.args == ("a\nb",)

    def test_format_preserves_tab_through_full_format(self):
        fmt = botlog._SafeFormatter("%(message)s")
        out = fmt.format(_record("a\tb\nc"))
        assert out == "a\tbc"


# ── DebugFilter ──────────────────────────────────────────────────────────

class TestDebugFilter:
    def test_at_or_above_base_passes(self):
        f = botlog.DebugFilter(logging.INFO)
        assert f.filter(_record("x", level=logging.WARNING)) is True
        assert f.filter(_record("x", level=logging.INFO)) is True

    def test_below_base_dropped(self):
        f = botlog.DebugFilter(logging.INFO)
        assert f.filter(_record("x", level=logging.DEBUG)) is False

    def test_global_debug_lets_low_records_through(self):
        f = botlog.DebugFilter(logging.INFO)
        f.global_debug = True
        assert f.filter(_record("x", level=logging.DEBUG)) is True

    def test_subsystem_exact_match(self):
        f = botlog.DebugFilter(logging.INFO)
        f.add_subsystem("internets.weather")
        assert f.filter(_record("x", name="internets.weather",
                                level=logging.DEBUG)) is True

    def test_subsystem_child_match(self):
        f = botlog.DebugFilter(logging.INFO)
        f.add_subsystem("internets.weather")
        assert f.filter(_record("x", name="internets.weather.api",
                                level=logging.DEBUG)) is True

    def test_subsystem_prefix_not_a_false_match(self):
        # "internets.weatherx" must NOT match subsystem "internets.weather"
        # (guard against a bare startswith without the dot boundary).
        f = botlog.DebugFilter(logging.INFO)
        f.add_subsystem("internets.weather")
        assert f.filter(_record("x", name="internets.weatherx",
                                level=logging.DEBUG)) is False

    def test_unrelated_subsystem_dropped(self):
        f = botlog.DebugFilter(logging.INFO)
        f.add_subsystem("internets.weather")
        assert f.filter(_record("x", name="internets.dns",
                                level=logging.DEBUG)) is False

    def test_set_base_level(self):
        f = botlog.DebugFilter(logging.INFO)
        f.set_base_level(logging.ERROR)
        assert f.base_level == logging.ERROR
        # WARNING now below base and no debug -> dropped.
        assert f.filter(_record("x", level=logging.WARNING)) is False
        assert f.filter(_record("x", level=logging.ERROR)) is True

    def test_add_remove_clear_subsystems(self):
        f = botlog.DebugFilter()
        f.add_subsystem("internets.a")
        f.add_subsystem("internets.b")
        assert f.active_subsystems() == {"internets.a", "internets.b"}
        f.remove_subsystem("internets.a")
        assert f.active_subsystems() == {"internets.b"}
        f.clear_subsystems()
        assert f.active_subsystems() == set()

    def test_active_subsystems_returns_copy(self):
        f = botlog.DebugFilter()
        f.add_subsystem("internets.a")
        snap = f.active_subsystems()
        snap.add("internets.evil")
        # Mutating the returned set must not corrupt internal state.
        assert f.active_subsystems() == {"internets.a"}

    def test_remove_missing_is_noop(self):
        f = botlog.DebugFilter()
        f.remove_subsystem("internets.nope")  # must not raise
        assert f.active_subsystems() == set()

    def test_default_base_level_is_info(self):
        f = botlog.DebugFilter()
        assert f.base_level == logging.INFO
        assert f.global_debug is False


# ── _setup_logging ───────────────────────────────────────────────────────

class TestSetupLogging:
    def _run_with_logfile(self, tmp_path, monkeypatch, debug_file=""):
        root = logging.getLogger("internets")
        saved = list(root.handlers)
        monkeypatch.setattr(botlog, "LOG_FILE", str(tmp_path / "internets.log"))
        monkeypatch.setattr(botlog, "LOG_DEBUG", debug_file)
        filt = botlog._setup_logging()
        new_handlers = list(root.handlers)
        # Restore the original handlers so the rest of the suite logs normally.
        for h in new_handlers:
            try:
                h.close()
            except Exception:
                pass
        root.handlers[:] = saved
        return filt, new_handlers, root

    def test_returns_debug_filter_and_sets_handlers(self, tmp_path, monkeypatch):
        filt, handlers, root = self._run_with_logfile(tmp_path, monkeypatch)
        assert isinstance(filt, botlog.DebugFilter)
        # File handler + stream handler, no debug file.
        assert len(handlers) == 2
        assert root.level == logging.DEBUG
        # Every handler carries the safe formatter and the debug filter.
        for h in handlers:
            assert isinstance(h.formatter, botlog._SafeFormatter)
            assert filt in h.filters

    def test_base_level_seeded_from_log_level(self, tmp_path, monkeypatch):
        filt, _, _ = self._run_with_logfile(tmp_path, monkeypatch)
        expected = getattr(logging, botlog.LOG_LEVEL, logging.INFO)
        assert filt.base_level == expected

    def test_debug_file_adds_third_handler(self, tmp_path, monkeypatch):
        dbg = str(tmp_path / "debug.log")
        filt, handlers, _ = self._run_with_logfile(tmp_path, monkeypatch,
                                                    debug_file=dbg)
        assert len(handlers) == 3
        assert any(getattr(h, "_debug_file", False) for h in handlers)


# ── get_hash ─────────────────────────────────────────────────────────────

class TestGetHash:
    def test_returns_stripped_string(self):
        h = botlog.get_hash()
        assert isinstance(h, str)
        # No surrounding whitespace.
        assert h == h.strip()

    def test_stable_across_calls(self):
        assert botlog.get_hash() == botlog.get_hash()


# ── _validate_hash (fail-closed on bad prefix) ───────────────────────────

class TestValidateHash:
    def test_empty_hash_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(botlog, "get_hash", lambda: "")
        # Returns normally (auth disabled, first-run); no SystemExit.
        assert botlog._validate_hash() is None

    @pytest.mark.parametrize("prefix", ["scrypt", "bcrypt", "argon2"])
    def test_valid_prefixes_accepted(self, monkeypatch, prefix):
        monkeypatch.setattr(botlog, "get_hash", lambda: f"{prefix}$N=16$abc")
        assert botlog._validate_hash() is None

    def test_unknown_algo_prefix_exits_1(self, monkeypatch):
        monkeypatch.setattr(botlog, "get_hash", lambda: "md5$deadbeef")
        with pytest.raises(SystemExit) as ei:
            botlog._validate_hash()
        assert ei.value.code == 1

    def test_hash_without_separator_exits_1(self, monkeypatch):
        # No '$' -> prefix resolves to "" -> not a known algo -> fail closed,
        # even though the string starts with a valid algo name.
        monkeypatch.setattr(botlog, "get_hash", lambda: "scryptnodollar")
        with pytest.raises(SystemExit) as ei:
            botlog._validate_hash()
        assert ei.value.code == 1

    def test_garbage_prefix_exits_1(self, monkeypatch):
        monkeypatch.setattr(botlog, "get_hash", lambda: "$weird")
        with pytest.raises(SystemExit) as ei:
            botlog._validate_hash()
        assert ei.value.code == 1


# ── mode-validation regex (the guard behind the import-time exit) ─────────

class TestModeRegex:
    @pytest.mark.parametrize("val", ["", "+iwx", "+o-v", "abc DEF", "+", "-",
                                     "+ -", "iow"])
    def test_valid_mode_strings_match(self, val):
        assert botlog._MODE_VALID.match(val) is not None

    @pytest.mark.parametrize("val", ["z!bad", "+o;v", "a$b", "mode#1",
                                     "a\nb", "x\x00y", "+1"])
    def test_invalid_mode_strings_rejected(self, val):
        assert botlog._MODE_VALID.match(val) is None


class TestImportTimeModeGuard:
    """Drive the real import-time `sys.exit(1)` for a bad IRC mode string by
    importing botlog in a fresh interpreter with a tampered config value."""

    def _import_with_mode(self, attr, value):
        code = (
            "import config\n"
            f"config.{attr} = {value!r}\n"
            "import botlog\n"
            "print('NO_EXIT')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(botlog.__file__),
            capture_output=True, text=True,
        )

    def test_bad_oper_modes_aborts_startup(self):
        r = self._import_with_mode("OPER_MODES", "z!bad")
        assert r.returncode == 1
        assert "NO_EXIT" not in r.stdout
        assert "Invalid oper_modes" in (r.stdout + r.stderr)

    def test_bad_user_modes_aborts_startup(self):
        r = self._import_with_mode("USER_MODES", "no;good")
        assert r.returncode == 1
        assert "NO_EXIT" not in r.stdout

    def test_valid_modes_import_succeeds(self):
        r = self._import_with_mode("OPER_MODES", "+o-v")
        assert r.returncode == 0
        assert "NO_EXIT" in r.stdout


# ── apply_debug ──────────────────────────────────────────────────────────

class TestApplyDebug:
    def test_no_args_enables_global(self, clean_filter):
        rep = _Collector()
        botlog.apply_debug([], rep)
        assert clean_filter.global_debug is True
        assert "ON" in rep.text

    def test_on_enables_global(self, clean_filter):
        rep = _Collector()
        botlog.apply_debug(["on"], rep)
        assert clean_filter.global_debug is True

    def test_off_disables_and_clears(self, clean_filter):
        clean_filter.global_debug = True
        clean_filter.add_subsystem("internets.weather")
        rep = _Collector()
        botlog.apply_debug(["off"], rep)
        assert clean_filter.global_debug is False
        assert clean_filter.active_subsystems() == set()
        assert "OFF" in rep.text

    def test_bare_subsystem_gets_namespaced(self, clean_filter):
        rep = _Collector()
        botlog.apply_debug(["weather"], rep)
        assert "internets.weather" in clean_filter.active_subsystems()
        assert logging.getLogger("internets.weather").level == logging.DEBUG
        assert "internets.weather debug ON" in rep.text

    def test_fully_qualified_subsystem_kept(self, clean_filter):
        rep = _Collector()
        botlog.apply_debug(["internets.dns"], rep)
        assert "internets.dns" in clean_filter.active_subsystems()

    def test_subsystem_off_removes_it(self, clean_filter):
        botlog.apply_debug(["weather"], _Collector())
        rep = _Collector()
        botlog.apply_debug(["weather", "off"], rep)
        assert "internets.weather" not in clean_filter.active_subsystems()
        assert logging.getLogger("internets.weather").level == logging.NOTSET
        assert "debug OFF" in rep.text


# ── apply_loglevel ───────────────────────────────────────────────────────

class TestApplyLogLevel:
    def test_no_args_reports_base_level(self, clean_filter):
        rep = _Collector()
        assert botlog.apply_loglevel([], rep) is None
        assert "base level = INFO" in rep.text

    def test_no_args_reports_global_debug_and_subsystems(self, clean_filter):
        clean_filter.global_debug = True
        clean_filter.add_subsystem("internets.weather")
        rep = _Collector()
        botlog.apply_loglevel([], rep)
        assert "global debug = ON" in rep.text
        assert "debug subsystems: internets.weather" in rep.text

    def test_no_args_reports_debug_file(self, clean_filter, monkeypatch):
        monkeypatch.setattr(botlog, "LOG_DEBUG", "/var/log/debug.log")
        rep = _Collector()
        botlog.apply_loglevel([], rep)
        assert "debug file = /var/log/debug.log" in rep.text

    def test_set_valid_base_level(self, clean_filter):
        rep = _Collector()
        clean_filter.global_debug = True
        assert botlog.apply_loglevel(["debug"], rep) is None
        assert clean_filter.base_level == logging.DEBUG
        # Setting an explicit base level also clears global debug.
        assert clean_filter.global_debug is False
        assert "Base level set to DEBUG" in rep.text

    def test_set_invalid_base_level_returns_error(self, clean_filter):
        rep = _Collector()
        err = botlog.apply_loglevel(["trace"], rep)
        assert err is not None
        assert "Invalid level" in err
        # Unchanged.
        assert clean_filter.base_level == logging.INFO

    def test_two_args_rejects_non_internets_logger(self, clean_filter):
        err = botlog.apply_loglevel(["weather", "DEBUG"], _Collector())
        assert err == "Logger must start with 'internets'"

    def test_two_args_debug_adds_subsystem(self, clean_filter):
        rep = _Collector()
        assert botlog.apply_loglevel(["internets.weather", "DEBUG"], rep) is None
        assert "internets.weather" in clean_filter.active_subsystems()
        assert logging.getLogger("internets.weather").level == logging.DEBUG
        assert "internets.weather = DEBUG" in rep.text

    def test_two_args_notset_removes_subsystem(self, clean_filter):
        botlog.apply_loglevel(["internets.weather", "DEBUG"], _Collector())
        rep = _Collector()
        botlog.apply_loglevel(["internets.weather", "NOTSET"], rep)
        assert "internets.weather" not in clean_filter.active_subsystems()
        assert logging.getLogger("internets.weather").level == logging.NOTSET
        assert "NOTSET (inherits parent)" in rep.text

    def test_two_args_explicit_level_removes_subsystem(self, clean_filter):
        botlog.apply_loglevel(["internets.weather", "DEBUG"], _Collector())
        rep = _Collector()
        botlog.apply_loglevel(["internets.weather", "WARNING"], rep)
        assert "internets.weather" not in clean_filter.active_subsystems()
        assert logging.getLogger("internets.weather").level == logging.WARNING
        assert "internets.weather = WARNING" in rep.text

    def test_two_args_invalid_level_returns_error(self, clean_filter):
        err = botlog.apply_loglevel(["internets.weather", "BOGUS"], _Collector())
        assert err is not None
        assert "Invalid level" in err

    def test_two_args_bare_logger_namespaced(self, clean_filter):
        # No dot -> "internets.<target>"; here target is already "internets"
        # so it becomes "internets.internets" (characterization).
        rep = _Collector()
        botlog.apply_loglevel(["internets", "DEBUG"], rep)
        assert "internets.internets" in clean_filter.active_subsystems()

    def test_three_args_usage_error(self, clean_filter):
        err = botlog.apply_loglevel(["a", "b", "c"], _Collector())
        assert err == "usage: loglevel [LEVEL | <logger> LEVEL]"


# ── module-level exports ─────────────────────────────────────────────────

class TestModuleExports:
    def test_log_is_internets_logger(self):
        assert botlog.log is logging.getLogger("internets")

    def test_log_filter_is_debug_filter(self):
        assert isinstance(botlog.log_filter, botlog.DebugFilter)

    def test_valid_levels_constant(self):
        assert botlog.VALID_LEVELS == ("DEBUG", "INFO", "WARNING", "ERROR")
