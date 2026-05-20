"""Append-only, hash-chained audit log for privileged bot actions.

This is intentionally separate from the main ``botlog`` stream.  Goals:

  * **Tamper-evident**: each record carries a SHA-256 over the previous
    record's hash plus the current record's fields, forming a chain.
    Removing or editing any line breaks ``verify()`` from that point on.
  * **Append-only on disk**: every ``record()`` opens the file in
    append-binary mode, writes one JSON line, then ``chmod 0o600`` to
    mirror ``secret_store._write_file_atomic`` semantics (the audit log
    may include hostmasks, which are PII).
  * **Cheap**: stdlib only, one ``threading.Lock`` for in-process
    serialization.  Not designed for cross-process concurrent writers.

Wire-up:
    The audit log is created here but other modules must *call* it.
    See the TODO marker below for the integration list in
    ``admin_cmds.py``.

# TODO(admin_cmds.py): call `audit_log.default().record(nick, host, "auth"/"load"/"unload"/"shutdown"/...)` from each privileged handler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("internets.audit")

_GENESIS_HASH = "0" * 64


def _iso_utc_now() -> str:
    """Return current UTC time as a strict ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _stable_args_str(args: Any) -> str:
    """Render ``args`` to a deterministic string for hashing.

    Strings pass through verbatim; everything else is rendered with
    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` so dict
    ordering doesn't break chain verification.
    """
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _hash_record(prev_hash: str, ts: str, actor: str, host: str,
                 action: str, args_str: str) -> str:
    """SHA-256 over the canonical concatenation of the record's fields."""
    h = hashlib.sha256()
    # NUL separators so a value containing the literal delimiter can't
    # collide with a different field layout.
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\x00")
    h.update(ts.encode("utf-8"))
    h.update(b"\x00")
    h.update(actor.encode("utf-8"))
    h.update(b"\x00")
    h.update(host.encode("utf-8"))
    h.update(b"\x00")
    h.update(action.encode("utf-8"))
    h.update(b"\x00")
    h.update(args_str.encode("utf-8"))
    return h.hexdigest()


class AuditLog:
    """Append-only, hash-chained audit log.

    Constructor:
        path: filesystem path (str or Path).  Resolved at instantiation.
              Default ``./audit.log``.

    Threading:
        All public methods are guarded by ``self._lock``.  Safe for
        multi-thread use within one process.  Not safe for concurrent
        writers across processes (no fcntl flock).
    """

    def __init__(self, path: str | Path = "./audit.log") -> None:
        self.path: Path = Path(path).resolve()
        self._lock = threading.Lock()
        # Cached tail hash so we don't re-read the file on every record().
        # Initialised lazily on first record() or verify() call.
        self._tip: str | None = None

    # ── internal helpers ────────────────────────────────────────────

    def _load_tip(self) -> str:
        """Return the last record's ``this_hash``, or the genesis hash."""
        if not self.path.exists():
            return _GENESIS_HASH
        last = _GENESIS_HASH
        try:
            with self.path.open("rb") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # Corrupt line — verify() will flag the index;
                        # for tip purposes treat as terminating boundary.
                        return last
                    h = obj.get("this_hash")
                    if isinstance(h, str) and len(h) == 64:
                        last = h
        except OSError as e:
            log.warning("audit_log: tip load failed: %s", type(e).__name__)
            return _GENESIS_HASH
        return last

    def _enforce_perms(self) -> None:
        """Best-effort chmod 0o600 (POSIX only)."""
        if os.name == "nt":
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError as e:
            log.warning("audit_log: chmod 0o600 failed on %s: %s",
                        self.path, type(e).__name__)

    # ── public API ──────────────────────────────────────────────────

    def record(self, actor: str, host: str, action: str,
               args: object = None) -> str:
        """Append a new audit entry; return the new ``this_hash``.

        Hashing inputs are stringified deterministically so re-walking the
        chain in ``verify()`` reproduces the same values.
        """
        ts = _iso_utc_now()
        actor_s  = str(actor)
        host_s   = str(host)
        action_s = str(action)
        args_str = _stable_args_str(args)

        with self._lock:
            if self._tip is None:
                self._tip = self._load_tip()
            prev = self._tip
            this_hash = _hash_record(prev, ts, actor_s, host_s,
                                     action_s, args_str)
            entry = {
                "ts":         ts,
                "actor":      actor_s,
                "host":       host_s,
                "action":     action_s,
                # Preserve the original args shape (dict/list/scalar) when
                # JSON-serialisable so post-hoc analysis isn't degraded
                # to opaque strings.  Fall back to the stable string form
                # otherwise.
                "args":       args if _is_jsonable(args) else args_str,
                "prev_hash":  prev,
                "this_hash":  this_hash,
            }
            line = json.dumps(entry, ensure_ascii=False,
                              separators=(",", ":")) + "\n"
            try:
                # Mirror secret_store's approach: create with 0o600 if new.
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if not self.path.exists():
                    # Create with restrictive perms from the start.
                    fd = os.open(str(self.path),
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as f:
                        f.write(line)
                else:
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(line)
                self._enforce_perms()
            except OSError as e:
                log.error("audit_log: write failed: %s", type(e).__name__)
                raise
            self._tip = this_hash
            return this_hash

    def verify(self) -> tuple[bool, int]:
        """Re-walk the chain.

        Returns:
            ``(True, -1)`` if the chain is intact (including the empty
            case where the file does not exist).
            ``(False, idx)`` where ``idx`` is the zero-based line index
            of the first broken record (bad JSON, prev_hash mismatch, or
            this_hash mismatch).
        """
        with self._lock:
            if not self.path.exists():
                return True, -1
            prev = _GENESIS_HASH
            idx = 0
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line:
                            # Blank lines are tolerated (don't advance idx).
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            return False, idx
                        if not isinstance(obj, dict):
                            return False, idx
                        if obj.get("prev_hash") != prev:
                            return False, idx
                        ts       = obj.get("ts", "")
                        actor    = obj.get("actor", "")
                        host     = obj.get("host", "")
                        action   = obj.get("action", "")
                        args_str = _stable_args_str(obj.get("args"))
                        expected = _hash_record(prev, ts, actor, host,
                                                action, args_str)
                        if obj.get("this_hash") != expected:
                            return False, idx
                        prev = expected
                        idx += 1
            except OSError as e:
                log.warning("audit_log: verify read failed: %s",
                            type(e).__name__)
                return False, idx
            return True, -1

    def count(self) -> int:
        """Return number of records currently on disk (cheap line count)."""
        if not self.path.exists():
            return 0
        n = 0
        try:
            with self.path.open("rb") as f:
                for raw in f:
                    if raw.strip():
                        n += 1
        except OSError:
            return 0
        return n


def _is_jsonable(x: Any) -> bool:
    """True if ``x`` round-trips through json.dumps without ``default=``."""
    try:
        json.dumps(x, ensure_ascii=False)
        return True
    except (TypeError, ValueError):
        return False


# ── Module-level singleton ──────────────────────────────────────────

_default_lock = threading.Lock()
_default_instance: AuditLog | None = None


def default() -> AuditLog:
    """Return a process-wide ``AuditLog`` singleton at ``./audit.log``."""
    global _default_instance
    if _default_instance is not None:
        return _default_instance
    with _default_lock:
        if _default_instance is None:
            _default_instance = AuditLog("./audit.log")
    return _default_instance
