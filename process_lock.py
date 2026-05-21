"""PID-based process lock with stale-detection.

Prevents two instances of the bot from running concurrently against the
same on-disk state.  Two concurrent writers would race on the JSON
state files (locations / channels / users / secrets) and silently
corrupt them — the prior writer's tmp-and-rename can clobber the
later writer's mid-flight changes.

Usage::

    from process_lock import ProcessLock, LockHeld

    try:
        with ProcessLock(Path("./internets.pid")):
            run_bot()
    except LockHeld as e:
        print(f"Another instance is running: {e}")
        sys.exit(1)

Design notes
------------
* The lockfile stores ``pid|start_time|hostname`` so a stale lock can be
  diagnosed (which host, when did it start, what was the PID).
* Stale detection on POSIX uses ``os.kill(pid, 0)`` which returns
  cleanly if the PID is live and raises ``ProcessLookupError`` if
  reaped.  Note this also catches the case where the PID was reused
  by an unrelated process — we then refuse the lock conservatively
  (better safe than corrupt state).
* On Windows we attempt ``psutil`` for liveness; if psutil isn't
  installed we fail open with a warning rather than refuse to start.
* Hostname is recorded so a lockfile left over from a different host
  (e.g. via shared NFS / Docker volume mount) can be diagnosed by the
  operator; we still refuse the lock conservatively in that case.
* The lock path is resolved at ``acquire()`` time (not ``__init__``)
  so callers can pass a relative ``Path`` that's resolved against the
  CWD that's current when the bot actually starts.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("internets.process_lock")


class LockHeld(Exception):
    """Raised when another live process owns the lock."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _pid_is_alive(pid: int) -> Optional[bool]:
    """Return True if *pid* refers to a live process, False if dead,
    None if we can't tell (fail-open path).
    """
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by another user — treat as
            # live; conservative refusal beats clobbering state.
            return True
        except OSError as e:
            if e.errno == errno.ESRCH:
                return False
            log.warning("process_lock: os.kill(%d, 0): %r", pid, e)
            return None
    # Non-POSIX (Windows) — try psutil; fall back to fail-open.
    try:
        import psutil  # type: ignore
    except ImportError:
        log.warning(
            "process_lock: psutil not installed on non-POSIX platform — "
            "cannot verify if PID %d is live; assuming dead (fail-open).",
            pid,
        )
        return None
    try:
        return psutil.pid_exists(pid)
    except Exception as e:
        log.warning("process_lock: psutil.pid_exists(%d) failed: %r", pid, e)
        return None


class ProcessLock:
    """Single-instance PID lockfile with stale detection.

    Use as a context manager::

        with ProcessLock(Path("./internets.pid")):
            ...

    or explicitly::

        lock = ProcessLock(Path("./internets.pid"))
        lock.acquire()
        try:
            ...
        finally:
            lock.release()

    Default path: ``./internets.pid`` (resolved at acquire() time).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        # Store unresolved — we resolve at acquire() so CWD changes
        # between __init__ and acquire are honoured.
        self._path_arg: Optional[Path] = path
        self._path: Optional[Path] = None
        self._owned: bool = False
        self._pid: int = 0
        self._start_time: float = 0.0
        self._hostname: str = ""

    # ── path resolution ────────────────────────────────────────────────

    def _resolved_path(self) -> Path:
        p = self._path_arg if self._path_arg is not None else Path("./internets.pid")
        # Don't call .resolve() — the parent directory may not exist
        # yet for an unusual deployment.  Just convert to an absolute
        # path against the *current* CWD.
        if not p.is_absolute():
            p = Path.cwd() / p
        return p

    # ── core operations ────────────────────────────────────────────────

    def acquire(self) -> None:
        """Acquire the lock.  Raises :class:`LockHeld` if held by another live PID."""
        self._path = self._resolved_path()
        self._pid = os.getpid()
        self._start_time = time.time()
        try:
            self._hostname = socket.gethostname()
        except Exception:
            self._hostname = "unknown"

        # If an existing file is present, decide whether it's stale.
        if self._path.exists():
            existing = self._read_existing()
            if existing is not None:
                other_pid, other_start, other_host = existing
                same_host = (other_host == self._hostname)
                alive: Optional[bool]
                if same_host:
                    alive = _pid_is_alive(other_pid)
                else:
                    # Different host — we cannot probe it.  Refuse
                    # conservatively; operator can delete the lockfile
                    # by hand if they're sure the other host is dead.
                    alive = True
                if alive is True:
                    raise LockHeld(
                        f"lockfile {self._path} held by pid={other_pid} "
                        f"on host={other_host!r} (started ~{other_start})"
                    )
                if alive is False:
                    log.warning(
                        "process_lock: removing stale lockfile %s "
                        "(pid=%d host=%r appears dead)",
                        self._path, other_pid, other_host,
                    )
                    self._safe_unlink(self._path)
                else:
                    # Fail-open: psutil unavailable on Windows.  Log
                    # and take the lock — better to risk a benign
                    # race than refuse-to-start on an admin's box.
                    log.warning(
                        "process_lock: cannot verify pid=%d liveness; "
                        "proceeding (fail-open).", other_pid,
                    )
                    self._safe_unlink(self._path)
            else:
                # Corrupt / unreadable lockfile — remove and continue.
                log.warning(
                    "process_lock: lockfile %s is unreadable — removing.",
                    self._path,
                )
                self._safe_unlink(self._path)

        # Create the lockfile atomically (O_EXCL).
        try:
            fd = os.open(
                str(self._path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError as e:
            # Lost the race with another instance starting at the same time.
            raise LockHeld(f"lockfile {self._path} appeared during acquire") from e
        except OSError as e:
            raise LockHeld(f"could not create lockfile {self._path}: {e!r}") from e

        try:
            payload = f"{self._pid}|{self._start_time:.3f}|{self._hostname}\n"
            os.write(fd, payload.encode("utf-8"))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        self._owned = True
        log.info("process_lock: acquired %s (pid=%d)", self._path, self._pid)

    def release(self) -> None:
        """Remove the lockfile iff we own it.  Idempotent."""
        if not self._owned or self._path is None:
            return
        # Confirm the file still contains our PID before unlinking.
        try:
            contents = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            contents = ""
        try:
            pid_str = contents.split("|", 1)[0] if contents else ""
            file_pid = int(pid_str) if pid_str.isdigit() else -1
        except ValueError:
            file_pid = -1
        if file_pid == self._pid:
            self._safe_unlink(self._path)
            log.info("process_lock: released %s", self._path)
        else:
            log.warning(
                "process_lock: not releasing %s — pid mismatch "
                "(file=%s, ours=%d)", self._path, file_pid, self._pid,
            )
        self._owned = False

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _safe_unlink(p: Path) -> None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("process_lock: could not unlink %s: %r", p, e)

    def _read_existing(self) -> Optional[tuple[int, float, str]]:
        """Return ``(pid, start_time, hostname)`` from the existing
        lockfile, or ``None`` if it's missing / unreadable / malformed.
        """
        # Bandit B101 — replace assert with a real RuntimeError so the
        # invariant survives `python -O`.  Reaching here without a path
        # would indicate a programming error in the caller.
        if self._path is None:
            raise RuntimeError("_read_existing called before path was set")
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text:
            return None
        parts = text.split("|")
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            return None
        try:
            start = float(parts[1]) if len(parts) > 1 else 0.0
        except ValueError:
            start = 0.0
        host = parts[2] if len(parts) > 2 else ""
        return pid, start, host

    # ── context manager ────────────────────────────────────────────────

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
        # Don't swallow exceptions.
        return None

    # ── introspection ──────────────────────────────────────────────────

    @property
    def path(self) -> Optional[Path]:
        """Resolved lockfile path (None if acquire() was never called)."""
        return self._path

    @property
    def owned(self) -> bool:
        """True if this instance currently holds the lock."""
        return self._owned


__all__ = ["ProcessLock", "LockHeld"]
