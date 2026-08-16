"""The dispatch log line must not record a secret passed as a bare argument.

`IRCBot._handle_privmsg()` writes one INFO line per accepted command carrying
the command name and its whole argument.  `sender.redact_secrets()` masks a
credential that FOLLOWS a credential verb (`IDENTIFY pw`), which is the right
shape for `.raw`, but a command whose entire argument IS the secret has no verb
to match.  `.pwn <password>` is exactly that: it is PM-gated because it takes a
password, and its refusal message tells the user never to type one in a channel.

These tests pin that such a command is masked by NAME, and that the set of such
names is derived from the module that owns the command rather than hard-coded
in the core, so a future module carrying a secret argument declares it once and
cannot be forgotten here.
"""

from __future__ import annotations

import logging
import sys
import threading

import pytest

_SAVED_ARGV = sys.argv
sys.argv = ["internets"]
import internets  # noqa: E402
from modules.base import BotModule  # noqa: E402

sys.argv = _SAVED_ARGV


class _SecretArgModule(BotModule):
    """Stand-in for a module whose argument is the secret (like secinfo.pwn)."""

    COMMANDS = {"probe_secret": "cmd_probe_secret"}
    SECRET_ARGS = frozenset({"probe_secret"})

    async def cmd_probe_secret(self, nick, reply_to, arg):  # pragma: no cover
        pass


class _OrdinaryModule(BotModule):
    COMMANDS = {"probe_plain": "cmd_probe_plain"}

    async def cmd_probe_plain(self, nick, reply_to, arg):  # pragma: no cover
        pass


@pytest.fixture
def bot(monkeypatch):
    b = internets.IRCBot.__new__(internets.IRCBot)
    b._nick = "TestBot"
    b._nick_hosts = {}
    b._auth_lock = threading.Lock()
    b._mod_lock = threading.Lock()
    b._stats_msg_in = 0
    b.active_channels = set()
    b._modules = {
        "secretmod": _SecretArgModule.__new__(_SecretArgModule),
        "plainmod": _OrdinaryModule.__new__(_OrdinaryModule),
    }
    b._commands = {
        "probe_secret": ("secretmod", "cmd_probe_secret"),
        "probe_plain": ("plainmod", "cmd_probe_plain"),
    }
    b.cfg = {"bot": {"command_prefix": "."}}
    monkeypatch.setattr(b, "_dispatch", lambda *a, **k: None)
    return b


def _line(text, target="TestBot"):
    return f":alice!u@h PRIVMSG {target} :{text}"


class TestSecretArgumentLogging:
    def test_declared_secret_argument_is_masked(self, bot, caplog):
        with caplog.at_level(logging.INFO, logger="internets"):
            bot._handle_privmsg(_line(".probe_secret hunter2"))
        assert "hunter2" not in caplog.text, "secret argument reached the log"
        assert "[REDACTED]" in caplog.text

    def test_masking_applies_in_a_channel_too(self, bot, caplog):
        bot.active_channels.add("#chan")
        bot._store = type("S", (), {"user_join": lambda *a: None})()
        with caplog.at_level(logging.INFO, logger="internets"):
            bot._handle_privmsg(_line(".probe_secret hunter2", target="#chan"))
        assert "hunter2" not in caplog.text

    def test_ordinary_command_argument_is_still_logged(self, bot, caplog):
        """Masking everything would destroy the log's diagnostic value."""
        with caplog.at_level(logging.INFO, logger="internets"):
            bot._handle_privmsg(_line(".probe_plain london"))
        assert "london" in caplog.text

    def test_core_auth_remains_masked(self, bot, caplog):
        with caplog.at_level(logging.INFO, logger="internets"):
            bot._handle_privmsg(_line(".auth s3cret"))
        assert "s3cret" not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_verb_keyed_redaction_still_applies(self, bot, caplog):
        """The existing verb path must keep working for .raw-style arguments."""
        with caplog.at_level(logging.INFO, logger="internets"):
            bot._handle_privmsg(_line(".probe_plain identify hunter2"))
        assert "hunter2" not in caplog.text


class TestSecretArgsContract:
    def test_base_module_declares_the_attribute(self):
        """A module author must be able to discover the hook from the base."""
        assert hasattr(BotModule, "SECRET_ARGS")
        assert BotModule.SECRET_ARGS == frozenset()

    def test_secinfo_declares_pwn(self):
        """The command that motivated this must be covered in the real tree."""
        import importlib

        secinfo = importlib.import_module("modules.secinfo")
        cls = next(
            o for _, o in vars(secinfo).items()
            if isinstance(o, type) and issubclass(o, BotModule) and o is not BotModule
        )
        assert "pwn" in getattr(cls, "SECRET_ARGS", frozenset())
