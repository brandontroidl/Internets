"""Tests for modules/base.py — cred() placeholder filter + is_configured()."""

from __future__ import annotations

from configparser import ConfigParser

import pytest

import secret_store
from modules.base import BotModule, cred, _PLACEHOLDER_MARKERS


# ── _PLACEHOLDER_MARKERS sanity ─────────────────────────────────────────

class TestPlaceholderMarkers:
    def test_marker_list_non_empty(self):
        assert _PLACEHOLDER_MARKERS  # truthy

    def test_markers_lowercase(self):
        for m in _PLACEHOLDER_MARKERS:
            assert m == m.lower(), f"marker {m!r} should be lowercase"


# ── cred(): secret_store path wins ──────────────────────────────────────

class TestCredSecretStoreWins:
    def test_secret_store_value_returned(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get",
                            lambda name, default="": "from_secret_store")
        cfg = ConfigParser()
        cfg.add_section("weather_providers")
        cfg.set("weather_providers", "weatherapi_key", "from_config")
        v = cred(cfg, "weatherapi_key", "weather_providers", "weatherapi_key")
        assert v == "from_secret_store"


# ── cred(): config.ini fallback ────────────────────────────────────────

class TestCredConfigFallback:
    @pytest.fixture(autouse=True)
    def _empty_secret_store(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name, default="": "")

    def test_returns_config_value_when_secret_store_empty(self):
        cfg = ConfigParser()
        cfg.add_section("weather_providers")
        cfg.set("weather_providers", "weatherapi_key", "real-from-config")
        v = cred(cfg, "weatherapi_key", "weather_providers", "weatherapi_key")
        assert v == "real-from-config"

    def test_strips_whitespace(self):
        cfg = ConfigParser()
        cfg.add_section("s")
        cfg.set("s", "k", "  trim_me  ")
        assert cred(cfg, "x", "s", "k") == "trim_me"

    def test_default_returned_for_missing_section(self):
        cfg = ConfigParser()
        assert cred(cfg, "x", "missing", "k", default="fallback") == "fallback"

    def test_default_returned_for_missing_key(self):
        cfg = ConfigParser()
        cfg.add_section("s")
        assert cred(cfg, "x", "s", "missing", default="fb") == "fb"


# ── cred(): placeholder filter ──────────────────────────────────────────

class TestCredPlaceholderFilter:
    @pytest.fixture(autouse=True)
    def _empty_secret_store(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name, default="": "")

    @pytest.mark.parametrize("placeholder", [
        "changeme", "your-key-here", "your-key", "placeholder",
        "set-in-secret-store", "<your-key>", "you@example.com",
        "user@example.com",
        # Mixed-case must still be filtered (substring match is on .lower()).
        "CHANGEME", "Set-In-Secret-Store",
        # Templates often embed the marker inside a longer value.
        "key=changeme-please", "leave-as-placeholder-for-now",
    ])
    def test_placeholder_filtered(self, placeholder):
        cfg = ConfigParser()
        cfg.add_section("s")
        cfg.set("s", "k", placeholder)
        # Result should be the default, NOT the placeholder.
        assert cred(cfg, "x", "s", "k", default="") == ""
        assert cred(cfg, "x", "s", "k", default="fb") == "fb"

    def test_real_value_passes_through(self):
        cfg = ConfigParser()
        cfg.add_section("s")
        cfg.set("s", "k", "ab12cd34ef56-real")
        assert cred(cfg, "x", "s", "k") == "ab12cd34ef56-real"

    def test_user_agent_with_real_email_passes(self):
        cfg = ConfigParser()
        cfg.add_section("weather")
        cfg.set("weather", "user_agent",
                "internets-bot/1.0 (real.address@my-domain.org)")
        # No placeholder markers in the string → passes through unchanged.
        v = cred(cfg, "weather_user_agent", "weather", "user_agent")
        assert "real.address@my-domain.org" in v

    def test_user_agent_with_example_blocked(self):
        cfg = ConfigParser()
        cfg.add_section("weather")
        cfg.set("weather", "user_agent",
                "internets-bot/1.0 (you@example.com)")
        # Contains "you@example" → filtered to default to avoid PII leak.
        v = cred(cfg, "weather_user_agent", "weather", "user_agent", default="")
        assert v == ""


# ── cred(): defensive against broken cfg ────────────────────────────────

class TestCredDefensive:
    def test_attribute_error_returns_default(self, monkeypatch):
        monkeypatch.setattr(secret_store, "get", lambda name, default="": "")
        # Passing None as cfg → cfg.get(...) raises AttributeError; the
        # function must swallow it and return the default.
        assert cred(None, "x", "s", "k", default="fb") == "fb"


# ── BotModule.is_configured() default ───────────────────────────────────

class TestIsConfiguredDefault:
    def test_default_is_true(self):
        # Base class default: assume configured unless overridden.
        class _NoBot:
            pass
        m = BotModule.__new__(BotModule)  # bypass __init__ that needs a bot
        m.bot = _NoBot()
        assert m.is_configured() is True

    def test_override_can_return_false(self):
        class _Disabled(BotModule):
            def is_configured(self) -> bool:
                return False

        m = _Disabled.__new__(_Disabled)
        m.bot = None
        assert m.is_configured() is False


# ── BotModule lifecycle hooks default to no-op ─────────────────────────

class TestBotModuleHooks:
    def test_default_help_lines_empty(self):
        m = BotModule.__new__(BotModule)
        m.bot = None
        assert m.help_lines(".") == []

    def test_default_hooks_are_noops(self):
        m = BotModule.__new__(BotModule)
        m.bot = None
        # Must not raise.
        assert m.on_load() is None
        assert m.on_unload() is None
        assert m.on_raw("anything") is None

    def test_default_forget_returns_zero(self):
        # Modules holding no PII inherit a no-op forget().
        m = BotModule.__new__(BotModule)
        m.bot = None
        assert m.forget("anyone") == 0


# ── BotModule.__init_subclass__ validates the COMMANDS contract ─────────

class TestCommandsContractValidation:
    def test_valid_async_handler_accepted(self):
        class _Good(BotModule):
            COMMANDS = {"foo": "cmd_foo"}

            async def cmd_foo(self, nick, reply_to, arg):
                pass

        assert _Good.COMMANDS["foo"] == "cmd_foo"

    def test_missing_handler_raises_at_class_definition(self):
        with pytest.raises(TypeError, match="no such method"):
            class _Bad(BotModule):
                COMMANDS = {"foo": "cmd_does_not_exist"}

    def test_sync_handler_rejected(self):
        with pytest.raises(TypeError, match="async def"):
            class _Bad(BotModule):
                COMMANDS = {"foo": "cmd_foo"}

                def cmd_foo(self, nick, reply_to, arg):  # sync — invalid
                    pass

    def test_empty_commands_is_fine(self):
        class _NoCommands(BotModule):
            pass

        assert _NoCommands.COMMANDS == {}
