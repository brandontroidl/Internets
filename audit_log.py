"""Append-only, HMAC-chained audit log for privileged bot actions.

This is intentionally separate from the main ``botlog`` stream.  Goals:

  * **Tamper-evident**: each record carries an HMAC-SHA-256 over the
    previous record's hash plus the current record's fields, forming a
    chain.  Editing or removing any record breaks ``verify()`` from that
    point on.  The HMAC key lives in a 0600 sidecar (``audit.key``), so
    an attacker who obtains only a *copy* of ``audit.log`` (a backup, an
    accidental commit) cannot recompute the chain to forge entries —
    plain SHA-256 (the pre-3.0.0 scheme) could be recomputed by anyone
    who knew the algorithm, which is in this file.
  * **Append-only on disk**: every ``record()`` opens the file in
    append-binary mode, writes one JSON line, then ``chmod 0o600`` (the
    log may include hostmasks, which are PII).
  * **Bounded**: the log rotates to ``audit.log.<timestamp>`` once it
    exceeds ``_MAX_BYTES``; each rotated segment is independently
    verifiable.  Rotation starts a fresh chain (new genesis).
  * **Cheap**: stdlib only, one ``threading.Lock`` for in-process
    serialization.  Not designed for cross-process concurrent writers.

Honest limitation: pure *tail* truncation by an attacker with write
access to both ``audit.log`` and ``audit.key`` cannot be detected from
the file alone — that needs an external append-only sink (remote
syslog), which is out of scope for a single-host bot.  Editing,
reordering, or deleting any non-tail record IS caught: it breaks the
``prev_hash`` link and the HMAC.

Backward compatibility: records written before 3.0.0 have no ``v`` field
and were hashed with plain SHA-256.  ``verify()`` still accepts them
(legacy mode); every new record is HMAC-chained (``v: 2``).

Wire-up:
    Already integrated.  Every privileged handler in ``admin_cmds.py``
    calls ``audit_log.default().record(nick, host, action, args)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("internets.audit")

_GENESIS_HASH = "0" * 64
_RECORD_VERSION = 2                  # 2 = HMAC-SHA-256; absent/1 = legacy SHA-256
_MAX_BYTES = 5 * 1024 * 1024         # rotate the log once it exceeds this


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


def _canonical(prev_hash: str, ts: str, actor: str, host: str,
               action: str, args_str: str) -> bytes:
    """NUL-separated canonical byte form of a record's hashed fields.

    NUL separators mean a value containing the literal delimiter can't
    collide with a different field layout.
    """
    return b"\x00".join(s.encode("utf-8") for s in
                        (prev_hash, ts, actor, host, action, args_str))


def _sha_record(prev_hash: str, ts: str, actor: str, host: str,
                action: str, args_str: str) -> str:
    """Legacy (pre-3.0.0) unkeyed SHA-256 digest — verify-only."""
    return hashlib.sha256(
        _canonical(prev_hash, ts, actor, host, action, args_str)).hexdigest()


def _hmac_record(key: bytes, prev_hash: str, ts: str, actor: str, host: str,
                 action: str, args_str: str) -> str:
    """HMAC-SHA-256 digest of a record under the audit key."""
    return hmac.new(
        key, _canonical(prev_hash, ts, actor, host, action, args_str),
        hashlib.sha256).hexdigest()


class AuditLog:
    """Append-only, HMAC-chained audit log.

    Constructor:
        path: filesystem path (str or Path).  Resolved at instantiation.
              Default ``./audit.log``.  The HMAC key sidecar is
              ``<path>.key`` (e.g. ``audit.log`` → ``audit.log.key``).

    Threading:
        All public methods are guarded by ``self._lock``.  Safe for
        multi-thread use within one process.  Not safe for concurrent
        writers across processes (no fcntl flock).
    """

    def __init__(self, path: str | Path = "./audit.log") -> None:
        self.path: Path = Path(path).resolve()
        self._key_path: Path = self.path.with_name(self.path.name + ".key")
        self._lock = threading.Lock()
        # Cached tail hash + HMAC key — both initialised lazily.
        self._tip: str | None = None
        self._key: bytes | None = None

    # ── internal helpers ────────────────────────────────────────────

    def _load_key(self) -> bytes:
        """Return the HMAC key, generating a 0600 sidecar on first use."""
        if self._key is not None:
            return self._key
        if self._key_path.exists():
            try:
                raw = self._key_path.read_text(encoding="ascii").strip()
            except OSError as e:
                # An EXISTING key that is merely unreadable (transient FS error,
                # a perms hiccup) must NOT be regenerated: the O_TRUNC below
                # would silently void every prior record's HMAC.  Fail closed -
                # the audit caller catches this, the operator fixes the file.
                raise RuntimeError(
                    f"audit_log: existing key unreadable ({type(e).__name__}); "
                    "refusing to overwrite tamper-evidence") from e
            try:
                self._key = bytes.fromhex(raw)
            except ValueError:
                self._key = b""
            if len(self._key) >= 32:
                return self._key
            # The existing key is genuinely malformed/short.  Move it aside
            # (not O_TRUNC over it) so the old chain stays recoverable, then
            # write a fresh key into the now-vacant path.
            log.warning("audit_log: key file invalid/short - backing up and regenerating")
            try:
                self._key_path.replace(
                    self._key_path.with_name(self._key_path.name + ".bad"))
            except OSError as e:
                log.error("audit_log: could not back up bad key (%s)", type(e).__name__)
        # Generate a fresh 32-byte key, written 0600 from creation.
        key = secrets.token_bytes(32)
        try:
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._key_path),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key.hex())
            if os.name != "nt":
                os.chmod(self._key_path, 0o600)
        except OSError as e:
            log.error("audit_log: could not persist key (%s) — audit chain "
                      "will not survive restart", type(e).__name__)
        self._key = key
        return key

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

    def _rotate_if_oversize(self) -> None:
        """Rotate the log aside if it exceeds ``_MAX_BYTES``.

        Caller must hold ``self._lock``.  The rotated segment keeps its
        own chain; the new ``audit.log`` starts fresh from genesis.
        """
        try:
            if not self.path.exists() or self.path.stat().st_size <= _MAX_BYTES:
                return
        except OSError:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.path.with_name(f"{self.path.name}.{stamp}")
        try:
            self.path.rename(dest)
            log.info("audit_log: rotated to %s", dest.name)
        except OSError as e:
            log.warning("audit_log: rotation failed: %s", type(e).__name__)
            return
        # Fresh chain for the new file.
        self._tip = _GENESIS_HASH

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
            key = self._load_key()
            self._rotate_if_oversize()
            if self._tip is None:
                self._tip = self._load_tip()
            prev = self._tip
            this_hash = _hmac_record(key, prev, ts, actor_s, host_s,
                                     action_s, args_str)
            entry = {
                "v":          _RECORD_VERSION,
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
            try:
                from metrics import registry as _mreg  # noqa: PLC0415
                _mreg.audit_records_total.inc()
            except Exception:  # noqa: BLE001
                pass  # nosec B110: best-effort cleanup
            return this_hash

    def verify(self) -> tuple[bool, int]:
        """Re-walk the chain.

        v2 records are verified with HMAC under the audit key; legacy
        records (no ``v`` field) fall back to plain SHA-256 so a log that
        predates 3.0.0 still verifies.

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
            key = self._load_key()
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
                        if obj.get("v") == _RECORD_VERSION:
                            expected = _hmac_record(key, prev, ts, actor,
                                                    host, action, args_str)
                        else:  # legacy pre-3.0.0 record
                            expected = _sha_record(prev, ts, actor, host,
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
