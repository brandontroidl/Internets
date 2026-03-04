"""Tests for store.py — Store (with pruning) and RateLimiter."""

import os
import json
import time
import tempfile
import pytest
from datetime import datetime, timezone, timedelta

from store import Store, RateLimiter


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def store(tmp_dir):
    s = Store(
        str(tmp_dir / "loc.json"),
        str(tmp_dir / "chan.json"),
        str(tmp_dir / "users.json"),
        user_max_age_days=30,
    )
    yield s
    s.stop()


class TestLocations:
    def test_set_get(self, store):
        store.loc_set("Alice", "New York")
        assert store.loc_get("alice") == "New York"

    def test_case_insensitive(self, store):
        store.loc_set("BOB", "LA")
        assert store.loc_get("bob") == "LA"

    def test_get_missing(self, store):
        assert store.loc_get("nobody") is None

    def test_delete(self, store):
        store.loc_set("Carol", "Miami")
        assert store.loc_del("carol") is True
        assert store.loc_get("carol") is None

    def test_delete_missing(self, store):
        assert store.loc_del("nobody") is False


class TestChannels:
    def test_save_load(self, store):
        store.channels_save({"#foo", "#bar"})
        loaded = store.channels_load()
        assert loaded == ["#bar", "#foo"]  # sorted

    def test_empty(self, store):
        assert store.channels_load() == []


class TestUserTracking:
    def test_join_creates_entry(self, store):
        store.user_join("#test", "Alice", "alice@host")
        users = store.channel_users("#test")
        assert "alice" in users
        assert users["alice"]["nick"] == "Alice"

    def test_part_updates_last_seen(self, store):
        store.user_join("#test", "Bob", "bob@host")
        time.sleep(0.01)
        store.user_part("#test", "Bob")
        users = store.channel_users("#test")
        assert "bob" in users

    def test_quit_updates_all_channels(self, store):
        store.user_join("#a", "Carol", "carol@host")
        store.user_join("#b", "Carol", "carol@host")
        store.user_quit("Carol")
        assert "carol" in store.channel_users("#a")
        assert "carol" in store.channel_users("#b")

    def test_rename(self, store):
        store.user_join("#test", "OldNick", "user@host")
        store.user_rename("OldNick", "NewNick", "user@newhost")
        users = store.channel_users("#test")
        assert "oldnick" not in users
        assert "newnick" in users
        assert users["newnick"]["nick"] == "NewNick"


class TestUserPruning:
    def test_prune_removes_stale_entries(self, store):
        # Inject a stale entry directly.
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with store._user_lock:
            store._users["#test"] = {
                "old_user": {
                    "nick": "OldUser", "hostmask": "old@host",
                    "first_seen": old_time, "last_seen": old_time,
                },
                "new_user": {
                    "nick": "NewUser", "hostmask": "new@host",
                    "first_seen": old_time,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                },
            }
        removed = store.prune_users()
        assert removed == 1
        users = store.channel_users("#test")
        assert "old_user" not in users
        assert "new_user" in users

    def test_prune_removes_empty_channels(self, store):
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with store._user_lock:
            store._users["#dead"] = {
                "gone": {"nick": "Gone", "hostmask": "x@y",
                         "first_seen": old_time, "last_seen": old_time},
            }
        store.prune_users()
        with store._user_lock:
            assert "#dead" not in store._users

    def test_prune_nothing_to_prune(self, store):
        store.user_join("#test", "Fresh", "fresh@host")
        removed = store.prune_users()
        assert removed == 0


class TestFlush:
    def test_flush_writes_dirty(self, store, tmp_dir):
        store.loc_set("test", "value")
        store.flush()
        data = json.loads((tmp_dir / "loc.json").read_text())
        assert data["test"] == "value"

    def test_atomic_write(self, store, tmp_dir):
        """After flush, no .tmp files should remain."""
        store.loc_set("test", "value")
        store.flush()
        tmps = list(tmp_dir.glob("*.tmp"))
        assert tmps == []


class TestRateLimiter:
    def test_flood_first_not_limited(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        assert rl.flood_check("alice") is False

    def test_flood_second_limited(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        rl.flood_check("alice")
        assert rl.flood_check("alice") is True

    def test_flood_admin_exempt(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        rl.flood_check("admin", is_admin=True)
        assert rl.flood_check("admin", is_admin=True) is False

    def test_api_first_not_limited(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        assert rl.api_check("alice") is False

    def test_api_second_limited(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        rl.api_check("alice")
        assert rl.api_check("alice") is True

    def test_different_nicks_independent(self):
        rl = RateLimiter(flood_cd=3, api_cd=10)
        rl.flood_check("alice")
        assert rl.flood_check("bob") is False
