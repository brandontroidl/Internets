import json
import threading
import time
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("internets")

_utcnow = lambda: datetime.now(timezone.utc).isoformat()

_FLUSH_INTERVAL = 30  # seconds between periodic disk writes


class Store:
    """
    In-memory state with periodic disk flush.

    Data is loaded once at startup and mutated in memory.  A background
    thread writes dirty datasets to disk every _FLUSH_INTERVAL seconds.
    flush() can be called manually (e.g. on shutdown) for immediate write.

    Each dataset (locations, channels, users) has its own lock so a weather
    lookup doesn't block behind a user-tracking write.
    """

    def __init__(self, loc_file, channels_file, users_file):
        self._lf = loc_file
        self._cf = channels_file
        self._uf = users_file

        self._loc_lock  = threading.Lock()
        self._chan_lock  = threading.Lock()
        self._user_lock = threading.Lock()

        # Load from disk once.
        self._locs     = self._read(loc_file, {})
        self._channels = self._read(channels_file, [])
        self._users    = self._read(users_file, {})

        self._dirty_locs  = False
        self._dirty_chans = False
        self._dirty_users = False

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="store-flush")
        self._thread.start()

    # ── Disk I/O (private) ───────────────────────────────────────────

    @staticmethod
    def _read(path, default):
        try:
            p = Path(path)
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:
            log.warning(f"Store load {path}: {e}")
        return default

    @staticmethod
    def _write(path, data):
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

    def _flush_loop(self):
        while not self._stop.wait(timeout=_FLUSH_INTERVAL):
            self.flush()

    def flush(self):
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
                if self._write(self._uf, self._users):
                    self._dirty_users = False

    def stop(self):
        """Stop the flush timer and write pending data."""
        self._stop.set()
        self.flush()

    # ── Locations ────────────────────────────────────────────────────

    def loc_get(self, nick):
        with self._loc_lock:
            return self._locs.get(nick.lower())

    def loc_set(self, nick, raw):
        with self._loc_lock:
            self._locs[nick.lower()] = raw
            self._dirty_locs = True

    def loc_del(self, nick):
        with self._loc_lock:
            if nick.lower() not in self._locs:
                return False
            del self._locs[nick.lower()]
            self._dirty_locs = True
            return True

    # ── Channels ─────────────────────────────────────────────────────

    def channels_load(self):
        with self._chan_lock:
            return list(self._channels)

    def channels_save(self, channels):
        with self._chan_lock:
            self._channels = sorted(channels)
            self._dirty_chans = True

    # ── User tracking ────────────────────────────────────────────────

    def user_join(self, channel, nick, hostmask):
        now = _utcnow()
        with self._user_lock:
            ch    = self._users.setdefault(channel.lower(), {})
            entry = ch.setdefault(nick.lower(), {
                "nick": nick, "hostmask": hostmask,
                "first_seen": now, "last_seen": now,
            })
            entry.update({"last_seen": now, "hostmask": hostmask, "nick": nick})
            self._dirty_users = True

    def user_part(self, channel, nick):
        with self._user_lock:
            entry = self._users.get(channel.lower(), {}).get(nick.lower())
            if entry:
                entry["last_seen"] = _utcnow()
                self._dirty_users = True

    def user_quit(self, nick):
        now = _utcnow()
        with self._user_lock:
            for ch in self._users.values():
                if nick.lower() in ch:
                    ch[nick.lower()]["last_seen"] = now
                    self._dirty_users = True

    def user_rename(self, old, new, hostmask):
        now = _utcnow()
        with self._user_lock:
            for ch in self._users.values():
                if old.lower() in ch:
                    entry = ch.pop(old.lower())
                    entry.update({"nick": new, "hostmask": hostmask, "last_seen": now})
                    ch[new.lower()] = entry
                    self._dirty_users = True

    def channel_users(self, channel):
        with self._user_lock:
            ch = self._users.get(channel.lower(), {})
            return {k: dict(v) for k, v in ch.items()}


class RateLimiter:
    _CLEANUP_INTERVAL = 300

    def __init__(self, flood_cd, api_cd):
        self._flood_cd = flood_cd
        self._api_cd   = api_cd
        self._lock     = threading.Lock()
        self._flood:   dict = {}
        self._api:     dict = {}
        self._last_cleanup = time.time()

    def _cleanup(self, now):
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._flood = {k: v for k, v in self._flood.items() if now - v < self._flood_cd}
        self._api   = {k: v for k, v in self._api.items()   if now - v < self._api_cd}
        self._last_cleanup = now

    def flood_check(self, nick, is_admin=False):
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

    def api_check(self, nick):
        now = time.time()
        k   = nick.lower()
        with self._lock:
            self._cleanup(now)
            if now - self._api.get(k, 0) < self._api_cd:
                return True
            self._api[k] = now
        return False
