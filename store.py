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


class Store:
    def __init__(self, loc_file, channels_file, users_file):
        self._lf   = loc_file
        self._cf   = channels_file
        self._uf   = users_file
        self._lock = threading.Lock()

    def _load(self, path, default):
        try:
            p = Path(path)
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:
            log.warning(f"Load {path}: {e}")
        return default

    def _save(self, path, data):
        """Atomic write: temp file + rename prevents corruption on crash."""
        try:
            p = Path(path)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, str(p))
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:
            log.warning(f"Save {path}: {e}")

    def loc_get(self, nick):
        with self._lock:
            return self._load(self._lf, {}).get(nick.lower())

    def loc_set(self, nick, raw):
        with self._lock:
            d = self._load(self._lf, {})
            d[nick.lower()] = raw
            self._save(self._lf, d)

    def loc_del(self, nick):
        with self._lock:
            d = self._load(self._lf, {})
            if nick.lower() not in d:
                return False
            del d[nick.lower()]
            self._save(self._lf, d)
            return True

    def channels_load(self):
        with self._lock:
            return self._load(self._cf, [])

    def channels_save(self, channels):
        with self._lock:
            self._save(self._cf, sorted(channels))

    def user_join(self, channel, nick, hostmask):
        now = _utcnow()
        with self._lock:
            data  = self._load(self._uf, {})
            ch    = data.setdefault(channel.lower(), {})
            entry = ch.setdefault(nick.lower(), {
                "nick": nick, "hostmask": hostmask,
                "first_seen": now, "last_seen": now,
            })
            entry.update({"last_seen": now, "hostmask": hostmask, "nick": nick})
            self._save(self._uf, data)

    def user_part(self, channel, nick):
        with self._lock:
            data  = self._load(self._uf, {})
            entry = data.get(channel.lower(), {}).get(nick.lower())
            if entry:
                entry["last_seen"] = _utcnow()
                self._save(self._uf, data)

    def user_quit(self, nick):
        now = _utcnow()
        with self._lock:
            data    = self._load(self._uf, {})
            touched = [ch for ch in data.values() if nick.lower() in ch]
            for ch in touched:
                ch[nick.lower()]["last_seen"] = now
            if touched:
                self._save(self._uf, data)

    def user_rename(self, old, new, hostmask):
        now = _utcnow()
        with self._lock:
            data    = self._load(self._uf, {})
            touched = False
            for ch in data.values():
                if old.lower() in ch:
                    entry = ch.pop(old.lower())
                    entry.update({"nick": new, "hostmask": hostmask, "last_seen": now})
                    ch[new.lower()] = entry
                    touched = True
            if touched:
                self._save(self._uf, data)

    def channel_users(self, channel):
        with self._lock:
            return self._load(self._uf, {}).get(channel.lower(), {})


class RateLimiter:
    _CLEANUP_INTERVAL = 300  # Purge stale entries every 5 minutes

    def __init__(self, flood_cd, api_cd):
        self._flood_cd = flood_cd
        self._api_cd   = api_cd
        self._lock     = threading.Lock()
        self._flood:   dict = {}
        self._api:     dict = {}
        self._last_cleanup = time.time()

    def _cleanup(self, now):
        """Remove entries older than their cooldown. Called under lock."""
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
