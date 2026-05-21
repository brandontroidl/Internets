"""Tests for secret_store.py — two-tier env/file secret backend.

These tests never touch the real config.ini.  Each test that exercises
the file backend monkey-patches ``secret_store.SECRETS_FILE`` to a temp
path.  OS-keyring support was removed in v2.7.0 (see CHANGELOG); the
backend is now just ``env var → config.ini[secrets]``.
"""

from __future__ import annotations

import os
import stat
import sys
import configparser
from pathlib import Path

import pytest

import secret_store


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def temp_secrets(tmp_path, monkeypatch):
    """Point SECRETS_FILE at a temp location and clear our env namespace."""
    fake = tmp_path / "config.ini"
    monkeypatch.setattr(secret_store, "SECRETS_FILE", fake)
    # Force the env-lookup path to miss for the bot's namespace.
    for k in list(os.environ):
        if k.startswith(secret_store.ENV_PREFIX):
            monkeypatch.delenv(k, raising=False)
    yield fake


# ── perms_ok ─────────────────────────────────────────────────────────────

class TestPermsOk:
    def test_absent_is_ok(self, tmp_path):
        ok, reason = secret_store.perms_ok(tmp_path / "nope")
        assert ok is True
        assert "absent" in reason

    @pytest.mark.skipif(os.name == "nt", reason="POSIX perm bits only")
    def test_world_readable_rejected(self, tmp_path):
        p = tmp_path / "leaky.ini"
        p.write_text("[secrets]\nfoo = bar\n")
        os.chmod(p, 0o644)
        ok, reason = secret_store.perms_ok(p)
        assert ok is False
        assert "0o600" in reason

    @pytest.mark.skipif(os.name == "nt", reason="POSIX perm bits only")
    def test_0o600_accepted(self, tmp_path):
        p = tmp_path / "tight.ini"
        p.write_text("[secrets]\nfoo = bar\n")
        os.chmod(p, 0o600)
        ok, reason = secret_store.perms_ok(p)
        assert ok is True


# ── get / set / delete via file backend ──────────────────────────────────

class TestFileBackend:
    def test_set_then_get(self, temp_secrets):
        backend = secret_store.set_value("weatherapi_key", "abc123")
        assert backend == "file"
        assert temp_secrets.exists()
        # File should be 0o600 on POSIX (created via O_CREAT with mode).
        if os.name != "nt":
            assert stat.S_IMODE(temp_secrets.stat().st_mode) == 0o600
        assert secret_store.get("weatherapi_key") == "abc123"

    def test_get_missing_returns_default(self, temp_secrets):
        assert secret_store.get("nope") == ""
        assert secret_store.get("nope", "fallback") == "fallback"

    def test_delete_removes_value(self, temp_secrets):
        secret_store.set_value("omdb_key", "xxx")
        touched = secret_store.delete("omdb_key")
        assert "file" in touched
        assert secret_store.get("omdb_key") == ""

    def test_delete_missing_returns_empty(self, temp_secrets):
        # Nothing stored — should not raise, returns empty list.
        assert secret_store.delete("nothing") == []

# ── Placeholder filter (the bot must never return a template value) ─────

class TestPlaceholderFilter:
    def test_get_returns_empty_for_placeholder(self, temp_secrets):
        # Write a placeholder value directly into config.ini's [secrets].
        temp_secrets.write_text(
            "[secrets]\nweatherapi_key = changeme\nlastfm_key = your-key-here\n"
        )
        if os.name != "nt":
            os.chmod(temp_secrets, 0o600)
        # All placeholders are treated as "not set".
        for ph in ("weatherapi_key", "lastfm_key"):
            assert secret_store.get(ph) == ""

    def test_get_returns_empty_for_set_via_secret_store_placeholder(
        self, temp_secrets
    ):
        temp_secrets.write_text(
            "[secrets]\nomdb_key = set-via-secret-store\n"
        )
        if os.name != "nt":
            os.chmod(temp_secrets, 0o600)
        assert secret_store.get("omdb_key") == ""

    def test_list_stored_hides_placeholders(self, temp_secrets):
        temp_secrets.write_text(
            "[secrets]\nweatherapi_key = changeme\nomdb_key = real_value_here\n"
        )
        if os.name != "nt":
            os.chmod(temp_secrets, 0o600)
        stored = secret_store.list_stored()
        # Placeholder filtered, real value reported.
        assert stored["weatherapi_key"] == ""
        assert stored["omdb_key"] == "file"


# ── Env var lookup ──────────────────────────────────────────────────────

class TestEnvLookup:
    def test_env_wins_over_file(self, temp_secrets, monkeypatch):
        # Both env + file set.  Env must win.
        temp_secrets.write_text("[secrets]\nweatherapi_key = from_file\n")
        if os.name != "nt":
            os.chmod(temp_secrets, 0o600)
        monkeypatch.setenv(secret_store.ENV_PREFIX + "WEATHERAPI_KEY", "from_env")
        assert secret_store.get("weatherapi_key") == "from_env"

    def test_list_stored_marks_env(self, temp_secrets, monkeypatch):
        monkeypatch.setenv(secret_store.ENV_PREFIX + "OMDB_KEY", "envval")
        stored = secret_store.list_stored()
        assert stored["omdb_key"] == "env"


# ── [secrets] section sitting BETWEEN other sections ────────────────────

class TestSecretsSectionMidFile:
    """The text-based editor must edit a [secrets] section that is not the
    first/only section without corrupting neighbours or duplicating the
    header — config.ini holds [irc], [bot], … then [secrets] at the end."""

    @staticmethod
    def _write(path, text):
        path.write_text(text, encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)

    def test_set_into_midfile_secrets_preserves_neighbours(self, temp_secrets):
        self._write(temp_secrets,
            "[irc]\nserver = irc.example.org\n\n"
            "[secrets]\nomdb_key = existing\n\n"
            "[logging]\nlevel = INFO\n")
        secret_store.set_value("weatherapi_key", "newval")
        text = temp_secrets.read_text(encoding="utf-8")
        assert text.count("[secrets]") == 1          # no duplicate header
        assert "server = irc.example.org" in text    # section before intact
        assert "level = INFO" in text                # section after intact
        assert secret_store.get("omdb_key") == "existing"
        assert secret_store.get("weatherapi_key") == "newval"

    def test_update_existing_key_in_place(self, temp_secrets):
        self._write(temp_secrets,
            "[irc]\nserver = x\n\n[secrets]\nomdb_key = old\n\n"
            "[logging]\nlevel = INFO\n")
        secret_store.set_value("omdb_key", "updated")
        text = temp_secrets.read_text(encoding="utf-8")
        assert secret_store.get("omdb_key") == "updated"
        assert "old" not in text
        assert "level = INFO" in text
        assert text.count("[secrets]") == 1

    def test_delete_from_midfile_secrets(self, temp_secrets):
        self._write(temp_secrets,
            "[irc]\nserver = x\n\n[secrets]\nomdb_key = gone\n"
            "lastfm_key = stay\n\n[logging]\nlevel = INFO\n")
        secret_store.delete("omdb_key")
        text = temp_secrets.read_text(encoding="utf-8")
        assert secret_store.get("omdb_key") == ""
        assert secret_store.get("lastfm_key") == "stay"
        assert "level = INFO" in text
        assert "server = x" in text


# ── set_value rejects newline injection ─────────────────────────────────

class TestNewlineInjection:
    def test_set_value_rejects_newline(self, temp_secrets):
        with pytest.raises(ValueError):
            secret_store.set_value("omdb_key", "abc\n[irc]\nserver = evil")

    def test_set_value_rejects_carriage_return(self, temp_secrets):
        with pytest.raises(ValueError):
            secret_store.set_value("omdb_key", "abc\rdef")


# ── Fail-closed perm check on read ──────────────────────────────────────

class TestFailClosed:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX perm bits only")
    def test_get_refuses_world_readable_file(
        self, temp_secrets, monkeypatch
    ):
        # Plant a value with bad perms — get() must refuse to read it.
        temp_secrets.write_text("[secrets]\nweatherapi_key = leaked\n")
        os.chmod(temp_secrets, 0o644)
        # Should fail closed and return default rather than the leaked value.
        assert secret_store.get("weatherapi_key") == ""
        assert secret_store.get("weatherapi_key", "x") == "x"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX perm bits only")
    def test_set_refuses_to_modify_world_readable_file(
        self, temp_secrets
    ):
        temp_secrets.write_text("[secrets]\nfoo = bar\n")
        os.chmod(temp_secrets, 0o644)
        with pytest.raises(PermissionError):
            secret_store.set_value("baz", "qux")


# ── status() snapshot ───────────────────────────────────────────────────

class TestStatus:
    def test_status_returns_required_keys(self, temp_secrets):
        info = secret_store.status()
        assert info["env_prefix"] == secret_store.ENV_PREFIX
        assert info["secrets_file"] == str(temp_secrets)
        assert info["secrets_file_exists"] is False
        assert info["perms_ok"] is True  # absent file == ok


# ── migrate() ───────────────────────────────────────────────────────────

class TestMigrate:
    def test_migrate_moves_plaintext_into_secrets_section(
        self, temp_secrets, tmp_path, monkeypatch
    ):
        # config.ini IS the destination now — plaintext in non-[secrets]
        # sections moves into [secrets] within the same file, and the
        # original section gets blanked.
        cfg_path = temp_secrets
        cfg_path.write_text(
            "[weather_providers]\n"
            "weatherapi_key = real-secret-1\n"
            "tomorrowio_key = real-secret-2\n"
            "openweathermap_key = changeme\n"   # placeholder → skipped
            "\n"
            "[imdb]\n"
            "omdb_key =\n"                      # empty → skipped
        )
        results = secret_store.migrate(cfg_path, scrub=True)
        assert results["weatherapi_key"] == "stored:file"
        assert results["tomorrowio_key"] == "stored:file"
        assert results["openweathermap_key"] == "skipped:empty"
        assert results["omdb_key"] == "skipped:empty"
        # Values now retrievable via the secret store ([secrets] section).
        assert secret_store.get("weatherapi_key") == "real-secret-1"
        assert secret_store.get("tomorrowio_key") == "real-secret-2"
        # Verify by section: plaintext gone from [weather_providers],
        # present in [secrets].
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        assert parser.get("weather_providers", "weatherapi_key").strip() == ""
        assert parser.get("weather_providers", "tomorrowio_key").strip() == ""
        assert parser.get("secrets", "weatherapi_key") == "real-secret-1"
        assert parser.get("secrets", "tomorrowio_key") == "real-secret-2"
        # Untouched placeholder remains in its original section.
        assert parser.get("weather_providers", "openweathermap_key") == "changeme"
        # File should now be 0o600 (auto-tightened by migrate).
        if os.name != "nt":
            assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    def test_migrate_no_scrub_leaves_other_sections_alone(
        self, temp_secrets
    ):
        # With scrub=False, the original plaintext stays put while the
        # value is *also* added to [secrets].
        cfg_path = temp_secrets
        cfg_path.write_text(
            "[weather_providers]\nweatherapi_key = keepme\n"
        )
        secret_store.migrate(cfg_path, scrub=False)
        text = cfg_path.read_text()
        assert "keepme" in text
        # Value should also be retrievable via the secret store now.
        assert secret_store.get("weatherapi_key") == "keepme"

    def test_migrate_missing_config_raises(self, temp_secrets, tmp_path):
        with pytest.raises(FileNotFoundError):
            secret_store.migrate(tmp_path / "nope.ini")


# ── init via _cmd_init ──────────────────────────────────────────────────

class TestInit:
    def test_init_creates_from_template(
        self, temp_secrets, tmp_path, monkeypatch
    ):
        # Stage a tiny template next to the test workdir.  The CLI resolves
        # config.ini.example from cwd, so chdir into tmp_path first.
        tmpl = tmp_path / "config.ini.example"
        tmpl.write_text("[secrets]\nweatherapi_key =\n")
        monkeypatch.chdir(tmp_path)
        # Re-point SECRETS_FILE under the chdir.
        target = tmp_path / "config.ini"
        monkeypatch.setattr(secret_store, "SECRETS_FILE", target)
        ns = type("NS", (), {"force": False})()
        rc = secret_store._cmd_init(ns)
        assert rc == 0
        assert target.exists()
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert "weatherapi_key" in target.read_text()

    def test_init_refuses_overwrite_without_force(
        self, temp_secrets, tmp_path, monkeypatch
    ):
        tmpl = tmp_path / "config.ini.example"
        tmpl.write_text("[secrets]\nweatherapi_key =\n")
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "config.ini"
        existing.write_text("[secrets]\nweatherapi_key = keepme\n")
        if os.name != "nt":
            os.chmod(existing, 0o600)
        monkeypatch.setattr(secret_store, "SECRETS_FILE", existing)
        ns = type("NS", (), {"force": False})()
        rc = secret_store._cmd_init(ns)
        assert rc == 1
        # Existing value preserved.
        assert "keepme" in existing.read_text()

    def test_init_with_force_overwrites(
        self, temp_secrets, tmp_path, monkeypatch
    ):
        # --force is now a wholesale overwrite (no merge).  After the rewrite,
        # the user's old value is gone and the template's empty key is in
        # its place.
        tmpl = tmp_path / "config.ini.example"
        tmpl.write_text(
            "[secrets]\nweatherapi_key =\ntomorrowio_key =\nbrand_new_key =\n"
        )
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "config.ini"
        existing.write_text(
            "[secrets]\nweatherapi_key = keepme\n"
        )
        if os.name != "nt":
            os.chmod(existing, 0o600)
        monkeypatch.setattr(secret_store, "SECRETS_FILE", existing)
        ns = type("NS", (), {"force": True})()
        rc = secret_store._cmd_init(ns)
        assert rc == 0
        rewritten = existing.read_text()
        # Old value blown away.
        assert "keepme" not in rewritten
        # New keys from template present.
        assert "brand_new_key" in rewritten
        assert "tomorrowio_key" in rewritten
        # File still 0o600 after the atomic rewrite.
        if os.name != "nt":
            assert stat.S_IMODE(existing.stat().st_mode) == 0o600
