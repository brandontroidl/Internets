from __future__ import annotations

import json
import threading
import time
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("internets.store")

_utcnow = lambda: datetime.now(timezone.utc).isoformat()

_FLUSH_INTERVAL = 30  # seconds between periodic disk writes
_USER_MAX_AGE_DAYS = 90  # prune user entries older than this


class Store:
    """
    In-memory state with periodic disk flush.

    Data is loaded once at startup and mutated in memory.  A background
    thread writes dirty datasets to disk every _FLUSH_INTERVAL seconds.
    flush() can be called manually (e.g. on shutdown) for immediate write.

    Each dataset (locations, channels, users) has its own lock so a weather
    lookup doesn't block behind a user-tracking write.
    """

    def __init__(self, loc_file: str, channels_file: str, users_file: str,
                 user_max_age_days: int = _USER_MAX_AGE_DAYS) -> None:
        self._lf = loc_file
        self._cf = channels_file
        self._uf = users_file
        self._user_max_age = timedelta(days=user_max_age_days)

        self._loc_lock  = threading.Lock()
        self._chan_lock  = threading.Lock()
        self._user_lock = threading.Lock()

        # Load from disk once.
        self._locs: dict[str, str]                         = self._read(loc_file, {})
        self._channels: list[str]                          = self._read(channels_file, [])
        self._users: dict[str, dict[str, dict[str, str]]]  = self._read(users_file, {})

        self._dirty_locs  = False
        self._dirty_chans = False
        self._dirty_users = False

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="store-flush")
        self._thread.start()

    # ── Disk I/O (private) ───────────────────────────────────────────

    @staticmethod
    def _read(path: str, default: Any) -> Any:
        try:
            p = Path(path)
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:
            log.warning(f"Store load {path}: {e}")
        return default

    @staticmethod
    def _write(path: str, data: Any) -> bool:
        try:
            p = Path(path)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, str(p))
                return True
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:
            log.warning(f"Store save {path}: {e}")
            return False

    # ── Flush ────────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        while not self._stop.wait(timeout=_FLUSH_INTERVAL):
            self.flush()

    def flush(self) -> None:
        """Write any dirty datasets to disk.  Safe to call from any thread."""
        with self._loc_lock:
            if self._dirty_locs:
                if self._write(self._lf, self._locs):
                    self._dirty_locs = False

        with self._chan_lock:
            if self._dirty_chans:
                if self._write(self._cf, sorted(self._channels)):
                    self._dirty_chans = False

        with self._user_lock:
            if self._dirty_users:
                self._prune_users()
                if self._write(self._uf, self._users):
                    self._dirty_users = False

    def stop(self) -> None:
        """Stop the flush timer and write pending data."""
        self._stop.set()
        self.flush()

    # ── User pruning ─────────────────────────────────────────────────

    def _prune_users(self) -> None:
        """Remove user entries older than _user_max_age.  Called under _user_lock."""
        cutoff = (datetime.now(timezone.utc) - self._user_max_age).isoformat()
        pruned = 0
        for ch in list(self._users):
            entries = self._users[ch]
            stale = [
                nick for nick, data in entries.items()
                if data.get("last_seen", "") < cutoff
            ]
            for nick in stale:
                del entries[nick]
                pruned += 1
            # Remove empty channel dicts.
            if not entries:
                del self._users[ch]
        if pruned:
            log.info(f"Pruned {pruned} stale user entries (>{self._user_max_age.days}d)")

    # ── Locations ────────────────────────────────────────────────────

    def loc_get(self, nick: str) -> str | None:
        with self._loc_lock:
            return self._locs.get(nick.lower())

    def loc_set(self, nick: str, raw: str) -> None:
        with self._loc_lock:
            self._locs[nick.lower()] = raw
            self._dirty_locs = True

    def loc_del(self, nick: str) -> bool:
        with self._loc_lock:
            if nick.lower() not in self._locs:
                return False
            del self._locs[nick.lower()]
            self._dirty_locs = True
            return True

    # ── Channels ─────────────────────────────────────────────────────

    def channels_load(self) -> list[str]:
        with self._chan_lock:
            return list(self._channels)

    def channels_save(self, channels: set[str] | list[str]) -> None:
        with self._chan_lock:
            self._channels = sorted(channels)
            self._dirty_chans = True

    # ── User tracking ────────────────────────────────────────────────

    def user_join(self, channel: str, nick: str, hostmask: str) -> None:
        now = _utcnow()
        with self._user_lock:
            ch    = self._users.setdefault(channel.lower(), {})
            entry = ch.setdefault(nick.lower(), {
                "nick": nick, "hostmask": hostmask,
                "first_seen": now, "last_seen": now,
            })
            entry.update({"last_seen": now, "hostmask": hostmask, "nick": nick})
            self._dirty_users = True

    def user_part(self, channel: str, nick: str) -> None:
        with self._user_lock:
            entry = self._users.get(channel.lower(), {}).get(nick.lower())
            if entry:
                entry["last_seen"] = _utcnow()
                self._dirty_users = True

    def user_quit(self, nick: str) -> None:
        now = _utcnow()
        with self._user_lock:
            for ch in self._users.values():
                if nick.lower() in ch:
                    ch[nick.lower()]["last_seen"] = now
                    self._dirty_users = True

    def user_rename(self, old: str, new: str, hostmask: str) -> None:
        now = _utcnow()
        with self._user_lock:
            for ch in self._users.values():
                if old.lower() in ch:
                    entry = ch.pop(old.lower())
                    entry.update({"nick": new, "hostmask": hostmask, "last_seen": now})
                    ch[new.lower()] = entry
                    self._dirty_users = True

    def channel_users(self, channel: str) -> dict[str, dict[str, str]]:
        with self._user_lock:
            ch = self._users.get(channel.lower(), {})
            return {k: dict(v) for k, v in ch.items()}


class RateLimiter:
    _CLEANUP_INTERVAL = 300

    def __init__(self, flood_cd: int, api_cd: int) -> None:
        self._flood_cd = flood_cd
        self._api_cd   = api_cd
        self._lock     = threading.Lock()
        self._flood: dict[str, float] = {}
        self._api:   dict[str, float] = {}
        self._last_cleanup = time.time()

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._flood = {k: v for k, v in self._flood.items() if now - v < self._flood_cd}
        self._api   = {k: v for k, v in self._api.items()   if now - v < self._api_cd}
        self._last_cleanup = now

    def flood_check(self, nick: str, is_admin: bool = False) -> bool:
        if is_admin:
            return False
        now = time.time()
        k   = nick.lower()
        with self._lock:
            self._cleanup(now)
            if now - self._flood.get(k, 0) < self._flood_cd:
                return True
            self._flood[k] = now
        return False

    def api_check(self, nick: str) -> bool:
        now = time.time()
        k   = nick.lower()
        with self._lock:
            self._cleanup(now)
            if now - self._api.get(k, 0) < self._api_cd:
                return True
            self._api[k] = now
        return False
