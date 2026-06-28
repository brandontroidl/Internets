"""Tests for admin_cmds.py - the AdminCommandsMixin handlers + module helpers.

The handlers are coroutines that hang off an IRCBot instance.  We drive them
against a hand-built ``FakeBot`` that subclasses the REAL mixin (so the actual
cmd_* code runs) and supplies only the attributes/methods the handlers touch:
preply/send/privmsg sinks, locks, the module registry, a tiny store, shadow-ban
state, and the auth knobs.  True externals are stubbed: the audit-log singleton
is redirected to a temp file, and password verification / get_hash are
monkeypatched so no bcrypt/argon2 backend or real config is needed.

Coroutines are driven with ``asyncio.run`` (matching tests/test_ipintel.py);
the project has no pytest-asyncio plugin.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

# config.py calls argparse.parse_args() at import time; admin_cmds imports
# config.  Pin argv to a clean value so pytest's own argv isn't parsed.
_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import admin_cmds  # noqa: E402
from admin_cmds import AdminCommandsMixin  # noqa: E402
import audit_log  # noqa: E402

sys.argv = _SAVED_ARGV


# ── Test doubles ─────────────────────────────────────────────────────────


class FakeModule:
    def __init__(self, commands=None, configured=True, help_lines=None):
        self.COMMANDS = commands if commands is not None else {}
        self._configured = configured
        self._help = help_lines if help_lines is not None else []

    def is_configured(self):
        return self._configured

    def help_lines(self, p):
        return list(self._help)


class FakeStore:
    def __init__(self, users_by_chan=None):
        self._users = users_by_chan or {}

    def channel_users(self, ch):
        return self._users.get(ch, {})


class FakeSender:
    MAX_QUEUE = 100

    class _Q:
        def qsize(self):
            return 3

    def __init__(self):
        self._q = self._Q()


class FakeBot(AdminCommandsMixin):
    """Concrete host for the mixin, with all collaborators captured."""

    def __init__(self):
        self._nick = "TestBot"
        self._authed = {}
        self._auth_fails = {}
        self._auth_lock = threading.Lock()
        self._mod_lock = threading.Lock()
        self._nick_hosts = {}
        self._modules = {}
        self._commands = {}

        self._AUTH_CLEANUP_THRESHOLD = 100
        self._AUTH_MAX_FAILS = 5
        self._AUTH_LOCKOUT = 300

        self.cfg = {}
        self._store = FakeStore()
        self.active_channels = set()
        self._shadow_bans = set()
        self._shadow_ban_reasons = {}

        self._admin = True  # is_admin() return value

        # Capture sinks
        self.replies: list[tuple[str, str, str]] = []
        self.sent: list[str] = []
        self.privmsgs: list[tuple[str, str]] = []
        self.shutdowns: list[str] = []
        self.saved_shadow = 0

        # Module-management stubs - overridable per-test
        self._load_result = (True, "loaded")
        self._unload_result = (True, "unloaded")
        self._reload_result = (True, "reloaded")

    # ── collaborators the mixin calls ──
    def preply(self, nick, reply_to, msg):
        self.replies.append((nick, reply_to, msg))

    def send(self, msg, priority=1):
        self.sent.append(msg)

    def privmsg(self, target, text):
        self.privmsgs.append((target, text))

    def is_admin(self, nick):
        return self._admin

    def load_module(self, name):
        return self._load_result

    def unload_module(self, name):
        return self._unload_result

    def reload_module(self, name):
        return self._reload_result

    def request_shutdown(self, reason="Shutting down"):
        self.shutdowns.append(reason)

    def _save_shadow_bans(self):
        self.saved_shadow += 1

    # ── helpers for assertions ──
    @property
    def msgs(self):
        return [m for _, _, m in self.replies]

    def text(self):
        return "\n".join(self.msgs)


def run(coro):
    return asyncio.run(coro)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Redirect the module-level audit singleton to a temp log."""
    al = audit_log.AuditLog(str(tmp_path / "audit.log"))
    monkeypatch.setattr(admin_cmds, "_audit", lambda: al)
    return al


@pytest.fixture
def bot(audit):
    return FakeBot()


# ── Module-level pure helpers ────────────────────────────────────────────


class TestHumanizeDelta:
    def test_seconds(self):
        assert admin_cmds._humanize_delta(0) == "0s"
        assert admin_cmds._humanize_delta(45) == "45s"

    def test_negative_clamped(self):
        assert admin_cmds._humanize_delta(-100) == "0s"

    def test_minutes(self):
        assert admin_cmds._humanize_delta(90) == "1m 30s"

    def test_hours(self):
        assert admin_cmds._humanize_delta(3700) == "1h 1m"

    def test_days(self):
        assert admin_cmds._humanize_delta(90000) == "1d 1h"


class TestHelpGrid:
    def test_empty(self):
        assert admin_cmds._help_grid([]) == []

    def test_uppercases_and_pads(self):
        rows = admin_cmds._help_grid(["a", "b", "c", "d", "e"], cols=4, col_w=6)
        assert len(rows) == 2
        # First row: 4 cols, last not padded.
        assert rows[0] == "A".ljust(6) + "B".ljust(6) + "C".ljust(6) + "D"
        assert rows[1] == "E"


class TestWrapList:
    def test_empty_returns_lead(self):
        assert admin_cmds._wrap_list([], "  Lead: ") == ["  Lead:"]

    def test_single_line(self):
        rows = admin_cmds._wrap_list(["a", "b"], "L: ", width=80)
        assert rows == ["L: a b"]

    def test_wraps_on_width(self):
        items = ["xxxxx", "yyyyy", "zzzzz"]
        rows = admin_cmds._wrap_list(items, "L: ", width=10)
        assert len(rows) >= 2
        # continuation lines align under the lead text
        assert all(r.startswith("L: ") or r.startswith("   ") for r in rows)


class TestAuditParse:
    def test_valid_dict(self):
        assert admin_cmds._audit_parse('{"a": 1}') == {"a": 1}

    def test_non_dict_json_is_none(self):
        assert admin_cmds._audit_parse('[1,2,3]') is None

    def test_garbage_is_none(self):
        assert admin_cmds._audit_parse("not json") is None


class TestAuditHaystack:
    def test_string_args(self):
        e = {"ts": "T", "actor": "al", "host": "h", "action": "raw", "args": "WHOIS"}
        hs = admin_cmds._audit_haystack(e)
        assert "al" in hs and "WHOIS" in hs and "raw" in hs

    def test_dict_args_serialized(self):
        e = {"actor": "al", "action": "say", "args": {"target": "#x", "text": "hi"}}
        hs = admin_cmds._audit_haystack(e)
        assert "#x" in hs and "hi" in hs


class TestAuditFormat:
    def test_truncates_iso_ts(self):
        e = {"ts": "2026-06-28T08:00:00.123456Z", "actor": "al",
             "action": "auth", "args": ""}
        out = admin_cmds._audit_format(e)
        assert "2026-06-28 08:00:00" in out
        assert "al" in out and "auth" in out

    def test_dict_args(self):
        e = {"ts": "?", "actor": "a", "action": "say", "args": {"k": "v"}}
        out = admin_cmds._audit_format(e)
        assert '"k":"v"' in out or "k" in out

    def test_long_args_truncated(self):
        e = {"ts": "?", "actor": "a", "action": "raw", "args": "X" * 300}
        out = admin_cmds._audit_format(e)
        assert out.endswith("...")


class TestStateFile:
    def test_section_present(self):
        cfg = {"seen": {"file": "/tmp/seen.json"}}
        p = admin_cmds._state_file(cfg, "seen", "default.json")
        assert str(p) == "/tmp/seen.json"

    def test_section_absent_uses_default(self):
        p = admin_cmds._state_file({}, "seen", "default.json")
        assert p == Path("default.json")


class TestReadJsonDict:
    def test_missing_returns_empty(self, tmp_path):
        assert admin_cmds._read_json_dict(tmp_path / "nope.json") == {}

    def test_valid_dict(self, tmp_path):
        p = tmp_path / "d.json"
        p.write_text('{"a": 1}')
        assert admin_cmds._read_json_dict(p) == {"a": 1}

    def test_non_dict_returns_empty(self, tmp_path):
        p = tmp_path / "d.json"
        p.write_text('[1,2]')
        assert admin_cmds._read_json_dict(p) == {}

    def test_garbage_returns_empty(self, tmp_path):
        p = tmp_path / "d.json"
        p.write_text("{not json")
        assert admin_cmds._read_json_dict(p) == {}


class TestReadRssKb:
    def test_returns_int_or_none(self):
        v = admin_cmds._read_rss_kb()
        assert v is None or (isinstance(v, int) and v > 0)


class TestCountAuditMentions:
    def test_no_file(self, audit):
        # audit log not yet written
        assert admin_cmds._count_audit_mentions("alice") == {"as_actor": 0, "in_args": 0}

    def test_counts_actor_and_args(self, audit):
        audit.record("alice", "alice@host", "auth", None)
        audit.record("bob", "bob@host", "shadow-ban", {"nick": "alice"})
        res = admin_cmds._count_audit_mentions("alice")
        assert res["as_actor"] == 1
        assert res["in_args"] == 1


# ── Auth ─────────────────────────────────────────────────────────────────


class TestAuth:
    def test_no_hash_configured(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "")
        run(bot.cmd_auth("alice", "alice", "pw"))
        assert "no password_hash" in bot.text()

    def test_no_arg_usage(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        run(bot.cmd_auth("alice", "alice", None))
        assert "AUTH <password>" in bot.text()

    def test_password_too_long(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        run(bot.cmd_auth("alice", "alice", "x" * 200))
        assert "too long" in bot.text()

    def test_wrong_password_increments_fails(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: False)
        run(bot.cmd_auth("alice", "alice", "bad"))
        assert "wrong password" in bot.text()
        assert bot._auth_fails["alice"][0] == 1

    def test_success_binds_hostmask(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: True)
        bot._nick_hosts["alice"] = "alice@host"
        run(bot.cmd_auth("alice", "alice", "good"))
        assert "authenticated" in bot.text()
        assert bot._authed["alice"] == "alice@host"

    def test_success_refused_without_hostmask(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: True)
        # No hostmask tracked -> fail closed, do not persist binding.
        run(bot.cmd_auth("alice", "alice", "good"))
        assert "can't confirm your hostmask" in bot.text()
        assert "alice" not in bot._authed

    def test_success_refused_with_unknown_sentinel(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: True)
        bot._nick_hosts["alice"] = "unknown"
        run(bot.cmd_auth("alice", "alice", "good"))
        assert "can't confirm" in bot.text()
        assert "alice" not in bot._authed

    def test_lockout_after_max_fails(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        called = []
        monkeypatch.setattr(admin_cmds, "verify_password",
                            lambda pw, h: called.append(1) or False)
        bot._auth_fails["alice"] = (bot._AUTH_MAX_FAILS, time.time())
        run(bot.cmd_auth("alice", "alice", "x"))
        assert "too many failed attempts" in bot.text()
        # verify_password must not even be invoked while locked out
        assert called == []

    def test_lockout_window_expiry_unblocks_gate(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: False)
        # Last attempt older than the lockout window: the gate no longer
        # blocks (fails treated as 0 for the >= MAX check), so verify runs
        # and the attempt is allowed through to "wrong password".  The
        # stored counter is re-read inside the lock for the increment, so
        # it climbs from the prior value (5) to 6 rather than resetting.
        bot._auth_fails["alice"] = (bot._AUTH_MAX_FAILS,
                                    time.time() - bot._AUTH_LOCKOUT - 10)
        run(bot.cmd_auth("alice", "alice", "x"))
        assert "wrong password" in bot.text()
        assert bot._auth_fails["alice"][0] == bot._AUTH_MAX_FAILS + 1

    def test_config_value_error(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")

        def boom(pw, h):
            raise ValueError("Unrecognised hash format")

        monkeypatch.setattr(admin_cmds, "verify_password", boom)
        run(bot.cmd_auth("alice", "alice", "x"))
        assert "config error" in bot.text()

    def test_backend_exception_counts_as_failure(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")

        def boom(pw, h):
            raise RuntimeError("backend exploded")

        monkeypatch.setattr(admin_cmds, "verify_password", boom)
        run(bot.cmd_auth("alice", "alice", "x"))
        assert "wrong password" in bot.text()
        assert bot._auth_fails["alice"][0] == 1

    def test_cleanup_threshold_prunes_stale(self, bot, monkeypatch):
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$x")
        monkeypatch.setattr(admin_cmds, "verify_password", lambda pw, h: False)
        bot._AUTH_CLEANUP_THRESHOLD = 0
        old = time.time() - bot._AUTH_LOCKOUT - 100
        bot._auth_fails = {"stale": (1, old)}
        run(bot.cmd_auth("alice", "alice", "x"))
        # Stale entry pruned by the cleanup pass; fresh failure recorded.
        assert "stale" not in bot._auth_fails
        assert "alice" in bot._auth_fails


class TestDeauth:
    def test_ends_active_session(self, bot):
        bot._authed["alice"] = "alice@host"
        run(bot.cmd_deauth("alice", "alice", None))
        assert "session ended" in bot.text()
        assert "alice" not in bot._authed

    def test_not_authenticated(self, bot):
        run(bot.cmd_deauth("alice", "alice", None))
        assert "not authenticated" in bot.text()


# ── Admin gate ───────────────────────────────────────────────────────────


class TestAdminGate:
    @pytest.mark.parametrize("method,arg", [
        ("cmd_load", "weather"),
        ("cmd_unload", "weather"),
        ("cmd_reload", "weather"),
        ("cmd_reloadall", None),
        ("cmd_restart", None),
        ("cmd_rehash", None),
        ("cmd_mode", "+i"),
        ("cmd_snomask", "+c"),
        ("cmd_raw", "WHOIS x"),
        ("cmd_say", "#c hi"),
        ("cmd_act", "#c hi"),
        ("cmd_nick", "NewNick"),
        ("cmd_uptime", None),
        ("cmd_stats", None),
        ("cmd_audit", None),
        ("cmd_fingerprint", "x"),
        ("cmd_shadow_ban", "x"),
        ("cmd_shadow_unban", "x"),
        ("cmd_shadow_list", None),
        ("cmd_loglevel", "DEBUG"),
        ("cmd_debug", "on"),
        ("cmd_shutdown", None),
    ])
    def test_non_admin_refused(self, bot, method, arg):
        bot._admin = False
        run(getattr(bot, method)("eve", "#chan", arg))
        assert "auth first" in bot.text()
        # No side effects leaked through the gate.
        assert bot.sent == []
        assert bot.privmsgs == []
        assert bot.shutdowns == []


# ── Info / help ──────────────────────────────────────────────────────────


class TestVersion:
    def test_version(self, bot):
        run(bot.cmd_version("alice", "#c", None))
        assert "Internets" in bot.text()
        assert admin_cmds.__version__ in bot.text()


class TestModules:
    def test_no_modules(self, bot):
        run(bot.cmd_modules("alice", "#c", None))
        assert "No modules loaded." in bot.text()

    def test_lists_loaded(self, bot):
        bot._modules = {"weather": FakeModule({"w": "cmd_w", "aqi": "cmd_w"})}
        run(bot.cmd_modules("alice", "#c", None))
        assert "weather (2)" in bot.text()
        assert "Loaded (1)" in bot.text()


class TestHelp:
    def _mods(self):
        return {
            "weather": FakeModule(
                {"weather": "cmd_w", "w": "cmd_w"}, configured=True,
                help_lines=[".weather <loc> - forecast", ".w - alias"]),
            "secret": FakeModule(
                {"sec": "cmd_sec"}, configured=False,
                help_lines=[".sec - hidden module"]),
        }

    def test_default_compact_list(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", None))
        t = bot.text()
        assert "modules" in t
        assert ".help <module>" in t

    def test_default_non_admin_hides_admin_note(self, bot):
        bot._admin = False
        bot._modules = self._mods()
        run(bot.cmd_help("eve", "#c", None))
        t = bot.text()
        # Non-admins never see the admin ops hint or hidden modules.
        assert "help admin" not in t
        assert "secret" not in t

    def test_help_all_grid(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", "all"))
        t = bot.text()
        assert "all commands" in t
        assert "WEATHER" in t.upper()

    def test_help_admin_as_admin(self, bot):
        run(bot.cmd_help("alice", "#c", "admin"))
        assert "admin commands" in bot.text()
        assert "RAW" in bot.text().upper()

    def test_help_admin_non_admin_refused(self, bot):
        bot._admin = False
        run(bot.cmd_help("eve", "#c", "admin"))
        assert "no command 'admin'" in bot.text()

    def test_help_module_roster(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", "weather"))
        t = bot.text()
        assert "[weather]" in t
        assert "forecast" in t

    def test_help_specific_command(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", "w"))
        # Resolves to the module owning .w and shows a matching line.
        assert ".w" in bot.text() or "alias" in bot.text()

    def test_help_unknown(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", "doesnotexist"))
        assert "no command 'doesnotexist'" in bot.text()

    def test_help_prefix_stripped(self, bot):
        bot._modules = self._mods()
        run(bot.cmd_help("alice", "#c", ".weather"))
        assert "[weather]" in bot.text()


# ── Module management ────────────────────────────────────────────────────


class TestModuleManagement:
    def test_load_usage(self, bot):
        run(bot.cmd_load("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_load_success(self, bot):
        bot._load_result = (True, "Loaded weather.")
        run(bot.cmd_load("alice", "#c", "weather"))
        assert "Loaded weather." in bot.text()

    def test_unload_usage(self, bot):
        run(bot.cmd_unload("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_unload_success(self, bot):
        bot._unload_result = (True, "Unloaded weather.")
        run(bot.cmd_unload("alice", "#c", "weather"))
        assert "Unloaded weather." in bot.text()

    def test_reload_usage(self, bot):
        run(bot.cmd_reload("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_reload_success(self, bot):
        bot._reload_result = (True, "Reloaded weather.")
        run(bot.cmd_reload("alice", "#c", "weather"))
        assert "Reloaded weather." in bot.text()

    def test_reloadall_none_loaded(self, bot):
        run(bot.cmd_reloadall("alice", "#c", None))
        assert "No modules loaded." in bot.text()

    def test_reloadall_reports_ok_and_fail(self, bot):
        bot._modules = {"a": FakeModule(), "b": FakeModule()}
        results = iter([(True, "ok"), (False, "boom")])
        bot.reload_module = lambda n: next(results)
        run(bot.cmd_reloadall("alice", "#c", None))
        t = bot.text()
        assert "Reloading: a, b" in t
        assert "OK:" in t and "FAILED:" in t

    def test_restart_sets_flag_and_shuts_down(self, bot):
        run(bot.cmd_restart("alice", "#c", None))
        assert "Restarting" in bot.text()
        assert bot._restart_flag is True
        assert bot.shutdowns == ["Restarting ..."]

    def test_rehash_success(self, bot, monkeypatch):
        monkeypatch.setattr("config.reload_config", lambda: [])
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "argon2$abc")
        bot._authed["alice"] = "alice@host"
        run(bot.cmd_rehash("alice", "#c", None))
        t = bot.text()
        assert "argon2 hash active" in t
        assert "Cleared 1 admin session" in t
        assert bot._authed == {}

    def test_rehash_no_hash(self, bot, monkeypatch):
        monkeypatch.setattr("config.reload_config", lambda: [])
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "")
        run(bot.cmd_rehash("alice", "#c", None))
        assert "no password_hash set" in bot.text()

    def test_rehash_bad_hash_prefix(self, bot, monkeypatch):
        monkeypatch.setattr("config.reload_config", lambda: [])
        monkeypatch.setattr(admin_cmds, "get_hash", lambda: "md5$deadbeef")
        run(bot.cmd_rehash("alice", "#c", None))
        assert "Bad password_hash format" in bot.text()

    def test_rehash_config_read_fails(self, bot, monkeypatch):
        def boom():
            raise OSError("cannot read")
        monkeypatch.setattr("config.reload_config", boom)
        run(bot.cmd_rehash("alice", "#c", None))
        assert "failed to read config" in bot.text()


# ── Modes / raw ──────────────────────────────────────────────────────────


class TestModes:
    def test_mode_usage(self, bot):
        run(bot.cmd_mode("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_mode_invalid(self, bot):
        run(bot.cmd_mode("alice", "#c", "+i!bad"))
        assert "invalid mode string" in bot.text()

    def test_mode_valid(self, bot):
        run(bot.cmd_mode("alice", "#c", "+iw"))
        assert "MODE TestBot +iw" in bot.sent

    def test_snomask_usage(self, bot):
        run(bot.cmd_snomask("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_snomask_invalid(self, bot):
        run(bot.cmd_snomask("alice", "#c", "+c d"))
        assert "invalid snomask string" in bot.text()

    def test_snomask_valid(self, bot):
        run(bot.cmd_snomask("alice", "#c", "+cF"))
        assert "MODE TestBot +s +cF" in bot.sent

    def test_raw_usage(self, bot):
        run(bot.cmd_raw("alice", "#c", "   "))
        assert "usage:" in bot.text()

    def test_raw_rejects_crlf(self, bot):
        run(bot.cmd_raw("alice", "#c", "WHOIS x\r\nQUIT"))
        assert "CR/LF/NUL" in bot.text()
        assert bot.sent == []

    def test_raw_rejects_oversize(self, bot):
        run(bot.cmd_raw("alice", "#c", "A" * 600))
        assert "exceeds 510 bytes" in bot.text()
        assert bot.sent == []

    def test_raw_valid(self, bot):
        run(bot.cmd_raw("alice", "#c", "WHOIS bob"))
        assert "WHOIS bob" in bot.sent
        assert ">> WHOIS bob" in bot.text()


# ── say / act / nick ─────────────────────────────────────────────────────


class TestSplitTargetAndText:
    def test_empty(self, bot):
        assert bot._split_target_and_text(None, "#c") == (None, None)
        assert bot._split_target_and_text("   ", "#c") == (None, None)

    def test_channel_target(self, bot):
        assert bot._split_target_and_text("#other hello there", "#c") == \
            ("#other", "hello there")

    def test_nick_target(self, bot):
        assert bot._split_target_and_text("bob hi", "#c") == ("bob", "hi")

    def test_single_word_falls_back_to_reply_to(self, bot):
        assert bot._split_target_and_text("hi", "#c") == ("#c", "hi")


class TestSay:
    def test_usage_on_empty(self, bot):
        run(bot.cmd_say("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_default_target(self, bot):
        run(bot.cmd_say("alice", "#c", "hello"))
        assert ("#c", "hello") in bot.privmsgs

    def test_explicit_target(self, bot):
        run(bot.cmd_say("alice", "#c", "#other hey there"))
        assert ("#other", "hey there") in bot.privmsgs

    def test_invalid_target_with_comma(self, bot):
        run(bot.cmd_say("alice", "#c", "#a,#b message"))
        assert "invalid target" in bot.text()
        assert bot.privmsgs == []


class TestAct:
    def test_wraps_ctcp_action(self, bot):
        run(bot.cmd_act("alice", "#c", "waves"))
        assert ("#c", "\x01ACTION waves\x01") in bot.privmsgs

    def test_usage(self, bot):
        run(bot.cmd_act("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_invalid_target_with_comma(self, bot):
        run(bot.cmd_act("alice", "#c", "#a,#b waves"))
        assert "invalid target" in bot.text()
        assert bot.privmsgs == []


class TestNick:
    def test_usage(self, bot):
        run(bot.cmd_nick("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_invalid(self, bot):
        run(bot.cmd_nick("alice", "#c", "123bad"))
        assert "invalid nick" in bot.text()
        assert bot.sent == []

    def test_same_nick(self, bot):
        run(bot.cmd_nick("alice", "#c", "TestBot"))
        assert "already using that nick" in bot.text()
        assert bot.sent == []

    def test_valid(self, bot):
        run(bot.cmd_nick("alice", "#c", "NewBot"))
        assert "NICK NewBot" in bot.sent
        assert "waiting for server confirmation" in bot.text()


# ── Diagnostics ──────────────────────────────────────────────────────────


class TestUptime:
    def test_not_connected(self, bot):
        bot._stats_boot_ts = time.time() - 120
        run(bot.cmd_uptime("alice", "#c", None))
        t = bot.text()
        assert "process up" in t
        assert "not connected" in t

    def test_connected(self, bot):
        bot._stats_boot_ts = time.time() - 120
        bot._stats_connect_ts = time.time() - 30
        run(bot.cmd_uptime("alice", "#c", None))
        assert "connected" in bot.text()


class TestStats:
    def test_renders_all_lines(self, bot):
        bot._stats_boot_ts = time.time() - 500
        bot._stats_connect_ts = time.time() - 100
        bot._stats_cmd_count = 7
        bot._stats_msg_in = 50
        bot._stats_msg_out = 40
        bot._sender = FakeSender()
        bot._modules = {"weather": FakeModule(configured=True),
                        "hidden": FakeModule(configured=False)}
        bot.active_channels = {"#a", "#b"}
        run(bot.cmd_stats("alice", "#c", None))
        t = bot.text()
        assert "stats" in t
        assert "1 configured / 2 loaded" in t
        assert "channels 2" in t
        assert "cmds" in t and "7" in t
        assert "3 / 100 slots" in t  # qsize 3 / MAX_QUEUE 100

    def test_audit_count_reflects_records(self, bot, audit):
        audit.record("alice", "h", "auth", None)
        run(bot.cmd_stats("alice", "#c", None))
        assert "1 records" in bot.text()


class TestAudit:
    def test_empty_log(self, bot):
        run(bot.cmd_audit("alice", "#c", None))
        assert "audit log is empty" in bot.text()

    def test_default_lists_records(self, bot, audit):
        audit.record("alice", "alice@h", "auth", None)
        audit.record("alice", "alice@h", "raw", "WHOIS bob")
        run(bot.cmd_audit("alice", "#c", None))
        t = bot.text()
        assert "audit log - last 2 of 2" in t
        assert "WHOIS bob" in t

    def test_grep_filters(self, bot, audit):
        audit.record("alice", "alice@h", "auth", None)
        audit.record("alice", "alice@h", "raw", "WHOIS bob")
        run(bot.cmd_audit("alice", "#c", "grep WHOIS"))
        t = bot.text()
        assert "audit grep" in t
        assert "WHOIS bob" in t
        assert "1 match" in t

    def test_grep_no_match(self, bot, audit):
        audit.record("alice", "alice@h", "auth", None)
        run(bot.cmd_audit("alice", "#c", "grep nonexistent"))
        assert "no matching entries" in bot.text()

    def test_numeric_arg(self, bot, audit):
        for i in range(5):
            audit.record("alice", "h", "auth", str(i))
        run(bot.cmd_audit("alice", "#c", "2"))
        assert "last 2 of 5" in bot.text()

    def test_tail(self, bot, audit):
        for i in range(8):
            audit.record("alice", "h", "auth", str(i))
        run(bot.cmd_audit("alice", "#c", "tail"))
        assert "last 5 of 8" in bot.text()

    def test_verify_intact(self, bot, audit):
        audit.record("alice", "h", "auth", None)
        run(bot.cmd_audit("alice", "#c", "verify"))
        assert "audit chain intact" in bot.text()

    def test_verify_broken(self, bot, audit):
        audit.record("alice", "h", "auth", None)
        audit.record("alice", "h", "raw", "x")
        # Corrupt the second record's args to break the HMAC chain.
        lines = audit.path.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["args"] = "tampered"
        lines[1] = json.dumps(obj)
        audit.path.write_text("\n".join(lines) + "\n")
        run(bot.cmd_audit("alice", "#c", "verify"))
        assert "audit chain BROKEN" in bot.text()

    def test_bad_usage(self, bot, audit):
        audit.record("alice", "h", "auth", None)
        run(bot.cmd_audit("alice", "#c", "wat"))
        assert "usage:" in bot.text()


# ── Fingerprint ──────────────────────────────────────────────────────────


class TestFingerprint:
    def test_usage(self, bot):
        run(bot.cmd_fingerprint("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_unknown_target_minimal(self, bot):
        run(bot.cmd_fingerprint("alice", "#c", "ghost"))
        t = bot.text()
        assert "fingerprint:" in t and "ghost" in t
        assert "unknown - not seen this session" in t
        assert "shadow-banned   no" in t

    def test_full_crossref(self, bot, audit, tmp_path):
        target = "bob"
        bot._nick_hosts["bob"] = "bob@host"
        bot.active_channels = {"#a"}
        bot._store = FakeStore({"#a": {"bob": {"nick": "Bob"}}})
        bot._shadow_bans = {"bob"}
        bot._shadow_ban_reasons = {"bob": "spamming"}

        seen = tmp_path / "seen.json"
        seen.write_text(json.dumps({"bob": {
            "ts": time.time() - 60, "event": "join",
            "channel": "#a", "detail": "hi"}}))
        tells = tmp_path / "tells.json"
        tells.write_text(json.dumps({
            "bob": [{"from": "alice", "msg": "yo"}],
            "carol": [{"from": "bob", "msg": "hey"}]}))
        notes = tmp_path / "notes.json"
        notes.write_text(json.dumps({"bob": ["note1", "note2"]}))
        bot.cfg = {
            "seen": {"file": str(seen)},
            "tell": {"file": str(tells)},
            "notes": {"file": str(notes)},
        }
        audit.record("bob", "bob@host", "auth", None)

        run(bot.cmd_fingerprint("alice", "#c", target))
        t = bot.text()
        assert "bob@host" in t
        assert "in channels     #a" in t
        assert "shadow-banned" in t and "spamming" in t
        assert "last seen" in t and "join" in t
        assert "1 pending to them, 1 sent by them" in t
        assert "2 note(s)" in t
        assert "1 as actor" in t


# ── Shadow-ban ───────────────────────────────────────────────────────────


class TestShadowBan:
    def test_usage(self, bot):
        run(bot.cmd_shadow_ban("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_refuses_self(self, bot):
        run(bot.cmd_shadow_ban("alice", "#c", "alice"))
        assert "refusing to shadow-ban yourself" in bot.text()

    def test_refuses_bot(self, bot):
        run(bot.cmd_shadow_ban("alice", "#c", "TestBot"))
        assert "refusing to shadow-ban the bot itself" in bot.text()

    def test_store_not_initialised(self, bot):
        del bot._shadow_bans
        run(bot.cmd_shadow_ban("alice", "#c", "eve"))
        assert "store not initialised" in bot.text()

    def test_success(self, bot):
        run(bot.cmd_shadow_ban("alice", "#c", "eve trolling"))
        assert "eve" in bot._shadow_bans
        assert bot._shadow_ban_reasons["eve"] == "trolling"
        assert bot.saved_shadow == 1
        assert "shadow-banned" in bot.text()

    def test_already_banned(self, bot):
        bot._shadow_bans = {"eve"}
        run(bot.cmd_shadow_ban("alice", "#c", "eve"))
        assert "already shadow-banned" in bot.text()


class TestShadowUnban:
    def test_usage(self, bot):
        run(bot.cmd_shadow_unban("alice", "#c", None))
        assert "usage:" in bot.text()

    def test_not_banned(self, bot):
        run(bot.cmd_shadow_unban("alice", "#c", "eve"))
        assert "is not shadow-banned" in bot.text()

    def test_success(self, bot):
        bot._shadow_bans = {"eve"}
        bot._shadow_ban_reasons = {"eve": "x"}
        run(bot.cmd_shadow_unban("alice", "#c", "eve"))
        assert "eve" not in bot._shadow_bans
        assert "eve" not in bot._shadow_ban_reasons
        assert bot.saved_shadow == 1
        assert "unbanned" in bot.text()


class TestShadowList:
    def test_empty(self, bot):
        run(bot.cmd_shadow_list("alice", "#c", None))
        assert "no shadow-bans active" in bot.text()

    def test_lists_with_reasons(self, bot):
        bot._shadow_bans = {"eve", "mallory"}
        bot._shadow_ban_reasons = {"eve": "spam"}
        run(bot.cmd_shadow_list("alice", "#c", None))
        t = bot.text()
        assert "shadow-bans" in t and "(2)" in t
        assert "eve" in t and "spam" in t
        assert "mallory" in t


# ── Logging ──────────────────────────────────────────────────────────────


class TestLoglevel:
    def test_listing(self, bot):
        run(bot.cmd_loglevel("alice", "#c", None))
        assert "Log levels:" in bot.text()

    def test_valid_level(self, bot):
        run(bot.cmd_loglevel("alice", "#c", "DEBUG"))
        assert "Base level set to DEBUG" in bot.text()

    def test_invalid_level(self, bot):
        run(bot.cmd_loglevel("alice", "#c", "BOGUS"))
        assert "Invalid level" in bot.text()


class TestDebug:
    def test_on(self, bot):
        run(bot.cmd_debug("alice", "#c", None))
        assert "Debug output ON" in bot.text()

    def test_off(self, bot):
        run(bot.cmd_debug("alice", "#c", "off"))
        assert "Debug output OFF" in bot.text()


# ── Shutdown ─────────────────────────────────────────────────────────────


class TestShutdown:
    def test_default_reason(self, bot):
        run(bot.cmd_shutdown("alice", "#c", None))
        assert "Shutting down: Shutting down" in bot.text()
        assert bot.shutdowns == ["Shutting down"]

    def test_custom_reason(self, bot):
        run(bot.cmd_shutdown("alice", "#c", "maintenance"))
        assert "Shutting down: maintenance" in bot.text()
        assert bot.shutdowns == ["maintenance"]
