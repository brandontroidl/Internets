from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("internets.store")


def _utcnow() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _before(iso: str, cutoff: datetime) -> bool:
    """True if ISO timestamp ``iso`` is older than ``cutoff``.

    Parses the timestamp rather than comparing strings lexicographically,
    so a stray ``Z`` suffix, a naive value, or a different UTC offset can't
    silently mis-order the comparison.  Missing/malformed values are treated
    as stale (the previous ``"" < cutoff`` behaviour).
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < cutoff


_FLUSH_INTERVAL = 30  # seconds between periodic disk writes
_USER_MAX_AGE_DAYS = 90  # prune user entries older than this

# ── State-file schema versioning ─────────────────────────────────────
# v1 (legacy): file is the bare payload — `{"alice": "ny", ...}` etc.
# v2 (current): file is `{"schema": 2, "checksum": "<sha256>", "data": <payload>}`.
#
# On read, v2 files have their SHA-256 checksum validated.  A mismatch
# means the file is corrupt or has been tampered with — we log a
# warning and fall back to the default (empty) state rather than load
# untrusted data into memory.
#
# v1 files are accepted silently and re-written as v2 on the next flush.
_SCHEMA_VERSION = 2


def _checksum(payload: Any) -> str:
    """Return SHA-256 hex of the canonical-JSON of *payload*.

    Canonicalisation: ``sort_keys=True`` + ``separators=(',', ':')``.
    This guarantees the same data → same hash regardless of dict
    insertion order or Python version.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _wrap_v2(payload: Any) -> dict:
    """Wrap *payload* into the v2 envelope: {schema, checksum, data}."""
    return {
        "schema": _SCHEMA_VERSION,
        "checksum": _checksum(payload),
        "data": payload,
    }


def _unwrap(raw: Any, default: Any) -> Any:
    """Unwrap a possibly-versioned envelope.

    Returns the inner payload on success.  Returns *default* if the
    envelope is v2 but its checksum doesn't match.  If *raw* is a
    legacy v1 payload (no ``schema`` key) it is returned unchanged so
    callers can re-wrap it on the next flush.
    """
    # v2 envelope: top-level dict with a "schema" key.
    if isinstance(raw, dict) and "schema" in raw:
        schema = raw.get("schema")
        if schema != _SCHEMA_VERSION:
            log.warning(
                "Store: unknown schema version %r — using default state.",
                schema,
            )
            return default
        data = raw.get("data")
        stored_sum = raw.get("checksum")
        if not isinstance(stored_sum, str):
            log.warning("Store: v2 envelope missing checksum — rejecting file.")
            return default
        computed = _checksum(data)
        if computed != stored_sum:
            log.warning(
                "Store: checksum mismatch (file=%s computed=%s) — "
                "rejecting file and using default state.",
                stored_sum, computed,
            )
            return default
        return data
    # v1 (legacy): bare payload.
    return raw


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

    _MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB cap on data files

    @staticmethod
    def _read(path: str, default: Any) -> Any:
        try:
            p = Path(path)
            if not p.exists():
                return default
            size = p.stat().st_size
            if size > Store._MAX_FILE_SIZE:
                log.warning(
                    f"Store file {path} exceeds size limit ({size} bytes) — using default"
                )
                return default
            raw = json.loads(p.read_text(encoding="utf-8"))
            # Unwrap a v2 envelope (validates checksum) or accept a
            # legacy v1 bare payload.  A checksum-fail returns *default*
            # so we don't import tampered data into memory; the next
            # flush will overwrite the file with a fresh v2 envelope.
            data = _unwrap(raw, default)
            # BUG-051: Validate loaded data matches expected type.
            # A corrupted file returning a list instead of a dict (or vice versa)
            # would crash on first access.
            if type(data) is not type(default):
                log.warning(
                    f"Store file {path} has type {type(data).__name__}, "
                    f"expected {type(default).__name__} — using default"
                )
                return default
            return data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning(f"Store load {path}: {e!r}")
            return default

    @staticmethod
    def _write(path: str, data: Any) -> bool:
        p = Path(path)
        tmp_path: Path | None = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            tmp_path = Path(tmp)
            try:
                # Wrap in a v2 envelope so future reads can verify
                # integrity.  Pretty-print for human inspection.
                envelope = _wrap_v2(data)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(envelope, f, indent=2)
                # Tighten perms BEFORE the atomic replace so the final
                # file is never world-readable, even momentarily.
                # locations.json holds user-supplied ZIPs, users.json
                # holds nick+hostmask+timestamps (PII).  POSIX only —
                # Windows ACLs are the operator's responsibility.
                if os.name != "nt":
                    try:
                        os.chmod(tmp_path, 0o600)
                    except OSError as e:
                        log.warning(f"Store chmod {tmp_path}: {e!r}")
                os.replace(tmp_path, p)
                tmp_path = None  # successfully renamed — nothing to clean up
                return True
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass  # best effort — may fail on Windows if locked
        except (OSError, TypeError, ValueError) as e:
            log.warning(f"Store save {path}: {e!r}")
            return False

    # ── Flush ────────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        while not self._stop.wait(timeout=_FLUSH_INTERVAL):
            self.flush()

    def flush(self) -> None:
        """Write any dirty datasets to disk.  Safe to call from any thread."""
        with self._loc_lock:
            if self._dirty_locs and self._write(self._lf, self._locs):
                self._dirty_locs = False

        with self._chan_lock:
            if self._dirty_chans and self._write(self._cf, sorted(self._channels)):
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

    def _prune_users(self) -> int:
        """Remove user entries older than _user_max_age.  Called under _user_lock.

        Returns the number of entries removed.
        """
        cutoff = datetime.now(timezone.utc) - self._user_max_age
        pruned = 0
        for ch in list(self._users):
            entries = self._users[ch]
            stale = [
                nick for nick, data in entries.items()
                if _before(data.get("last_seen", ""), cutoff)
            ]
            for nick in stale:
                del entries[nick]
                pruned += 1
            # Remove empty channel dicts.
            if not entries:
                del self._users[ch]
        if pruned:
            log.info(f"Pruned {pruned} stale user entries (>{self._user_max_age.days}d)")
        return pruned

    def prune_users(self) -> int:
        """Public interface: prune stale user entries.  Thread-safe.

        Returns the number of entries removed.
        """
        with self._user_lock:
            return self._prune_users()

    # ── Locations ────────────────────────────────────────────────────

    def loc_get(self, nick: str) -> str | None:
        """Return the saved location string for *nick*, or None."""
        with self._loc_lock:
            return self._locs.get(nick.lower())

    def loc_set(self, nick: str, raw: str) -> None:
        """Save a location string for *nick*."""
        with self._loc_lock:
            self._locs[nick.lower()] = raw
            self._dirty_locs = True

    def loc_del(self, nick: str) -> bool:
        """Delete saved location for *nick*.  Returns False if none existed."""
        key = nick.lower()
        with self._loc_lock:
            if key not in self._locs:
                return False
            del self._locs[key]
            self._dirty_locs = True
            return True

    # ── Channels ─────────────────────────────────────────────────────

    def channels_load(self) -> list[str]:
        """Return the saved channel list."""
        with self._chan_lock:
            return list(self._channels)

    def channels_save(self, channels: set[str] | list[str]) -> None:
        """Replace the saved channel list and mark dirty."""
        with self._chan_lock:
            self._channels = sorted(channels)
            self._dirty_chans = True

    # ── User tracking ────────────────────────────────────────────────

    def user_join(self, channel: str, nick: str, hostmask: str) -> None:
        """Record a user joining or speaking in *channel*."""
        now = _utcnow()
        with self._user_lock:
            ch    = self._users.setdefault(channel.lower(), {})
            entry = ch.setdefault(nick.lower(), {
                "nick": nick, "hostmask": hostmask,
                "first_seen": now, "last_seen": now,
                # opted_out: when True, the privacy module's user-data
                # collection (last-seen / hostmask updates) should be
                # skipped.  Default False keeps existing behaviour.
                "opted_out": False,
            })
            entry.update({"last_seen": now, "hostmask": hostmask, "nick": nick})
            # Defensive: legacy records may not carry the field.
            entry.setdefault("opted_out", False)
            self._dirty_users = True

    def user_part(self, channel: str, nick: str) -> None:
        """Update last-seen timestamp when a user parts *channel*."""
        with self._user_lock:
            entry = self._users.get(channel.lower(), {}).get(nick.lower())
            if entry is not None:
                entry["last_seen"] = _utcnow()
                self._dirty_users = True

    def user_quit(self, nick: str) -> None:
        """Update last-seen for *nick* across all channels."""
        now = _utcnow()
        key = nick.lower()
        with self._user_lock:
            for ch in self._users.values():
                if (entry := ch.get(key)) is not None:
                    entry["last_seen"] = now
                    self._dirty_users = True

    def user_purge(self, nick: str) -> int:
        """Hard-delete every tracked record of *nick* across all channels.

        Returns the number of channel rows removed.  Used by .forgetme
        (modules/privacy.py) to honour user data-deletion requests.
        Unlike user_quit() which only stamps last_seen, this drops the
        entry entirely; it will not reappear until the user re-joins.
        """
        key = nick.lower()
        removed = 0
        with self._user_lock:
            for ch in list(self._users.values()):
                if key in ch:
                    del ch[key]
                    removed += 1
                    self._dirty_users = True
            # Clean up any channels left empty.
            empty = [c for c, members in self._users.items() if not members]
            for c in empty:
                del self._users[c]
        return removed

    def user_rename(self, old: str, new: str, hostmask: str) -> None:
        """Re-key a user entry when they change nicks."""
        now      = _utcnow()
        old_key  = old.lower()
        new_key  = new.lower()
        with self._user_lock:
            for ch in self._users.values():
                if old_key in ch:
                    entry = ch.pop(old_key)
                    entry.update({"nick": new, "hostmask": hostmask, "last_seen": now})
                    ch[new_key] = entry
                    self._dirty_users = True

    def channel_users(self, channel: str) -> dict[str, dict[str, str]]:
        """Return a snapshot of tracked user data for *channel*."""
        with self._user_lock:
            ch = self._users.get(channel.lower(), {})
            return {k: dict(v) for k, v in ch.items()}

    # ── Opt-out flag ─────────────────────────────────────────────────
    # The privacy module exposes a user-facing command that calls these
    # to flip the flag.  We set it on every channel record that tracks
    # the nick so checks anywhere in the bot see a consistent answer.

    def set_opt_out(self, nick: str, value: bool) -> None:
        """Set the opt-out flag for *nick* across all tracked channels.

        Creates a stub entry in a synthetic ``"*"`` channel if the user
        is not currently tracked anywhere, so the preference persists
        even before they next speak.
        """
        key = nick.lower()
        now = _utcnow()
        with self._user_lock:
            seen = False
            for ch in self._users.values():
                if (entry := ch.get(key)) is not None:
                    entry["opted_out"] = bool(value)
                    seen = True
            if not seen:
                # No tracked record — create a sentinel one so the
                # preference survives a restart.
                ch = self._users.setdefault("*", {})
                ch[key] = {
                    "nick": nick, "hostmask": "",
                    "first_seen": now, "last_seen": now,
                    "opted_out": bool(value),
                }
            self._dirty_users = True

    def is_opted_out(self, nick: str) -> bool:
        """Return True if *nick* has opted out of user-data tracking."""
        key = nick.lower()
        with self._user_lock:
            for ch in self._users.values():
                entry = ch.get(key)
                if entry is not None and entry.get("opted_out"):
                    return True
        return False


class RateLimiter:
    """Per-nick flood + API rate limiting, plus per-channel global gate.

    Three independent windows:

    * ``flood_check(nick, is_admin)`` — per-nick, fast (default 3s).
      Admins bypass.  Catches a single user hammering the bot.
    * ``api_check(nick)`` — per-nick, slower (default 10s).  Throttles
      the expensive paths (geocoding + weather APIs).
    * ``channel_check(channel, threshold)`` — per-channel, sliding window
      across ALL users in that channel.  Catches coordinated floods
      where N different nicks each send 1 command/second.  Returns
      True when the channel has exceeded ``threshold`` commands in
      the last ``_CHANNEL_WINDOW`` seconds.

    Periodic ``_cleanup`` evicts stale entries from all three maps.
    """
    _CLEANUP_INTERVAL = 300
    _CHANNEL_WINDOW = 10        # seconds — sliding window for channel rate
    _CHANNEL_DEFAULT_BURST = 20  # commands per window before throttling

    def __init__(self, flood_cd: int, api_cd: int) -> None:
        self._flood_cd = flood_cd
        self._api_cd   = api_cd
        self._lock     = threading.Lock()
        self._flood: dict[str, float] = {}
        self._api:   dict[str, float] = {}
        # Per-channel: list of recent command timestamps within the window.
        self._channel: dict[str, list[float]] = {}
        self._last_cleanup = time.time()

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._flood = {k: v for k, v in self._flood.items() if now - v < self._flood_cd}
        self._api   = {k: v for k, v in self._api.items()   if now - v < self._api_cd}
        # Channel: drop entries whose entire window has elapsed.
        cutoff = now - self._CHANNEL_WINDOW
        self._channel = {
            ch: [t for t in ts if t > cutoff]
            for ch, ts in self._channel.items()
            if any(t > cutoff for t in ts)
        }
        self._last_cleanup = now

    def flood_check(self, nick: str, is_admin: bool = False) -> bool:
        """Return True if *nick* is flooding.  Admins bypass."""
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
        """Return True if *nick* has hit the API cooldown."""
        now = time.time()
        k   = nick.lower()
        with self._lock:
            self._cleanup(now)
            if now - self._api.get(k, 0) < self._api_cd:
                return True
            self._api[k] = now
        return False

    def channel_check(self, channel: str, threshold: int | None = None) -> bool:
        """Return True if *channel* has exceeded the burst threshold.

        Defends against coordinated floods across distinct nicks
        (per-nick flood/api limits don't catch those — N users each
        sending 1 command/second can still saturate the bot).
        Defaults: ``_CHANNEL_DEFAULT_BURST`` commands per
        ``_CHANNEL_WINDOW`` seconds.
        """
        if not channel or not channel.startswith(("#", "&", "+", "!")):
            return False  # not a channel (PM) — only per-nick limits apply
        cap = threshold if threshold is not None else self._CHANNEL_DEFAULT_BURST
        now = time.time()
        k = channel.lower()
        with self._lock:
            self._cleanup(now)
            cutoff = now - self._CHANNEL_WINDOW
            recent = [t for t in self._channel.get(k, []) if t > cutoff]
            if len(recent) >= cap:
                # Channel is over its budget — refuse but do NOT record
                # the new attempt (so attackers can't keep the window
                # full forever by spamming once the limit is hit).
                self._channel[k] = recent
                return True
            recent.append(now)
            self._channel[k] = recent
        return False
