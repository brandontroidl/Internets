"""Tests for process_lock.py - PID-based single-instance lock.

Exercises real on-disk lockfiles via tmp_path.  The only mocked externals
are the liveness probe itself (os.kill / the _pid_is_alive helper) and, for
the Windows branch, the psutil import - everything else runs the real code.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

import pytest

import process_lock
from process_lock import ProcessLock, LockHeld


# ── helpers ──────────────────────────────────────────────────────────────

def _plant(path: Path, pid, start="1234.500", host=None):
    """Write a lockfile with the given fields (host defaults to current)."""
    if host is None:
        host = socket.gethostname()
    path.write_text(f"{pid}|{start}|{host}\n", encoding="utf-8")


# ── _pid_is_alive ────────────────────────────────────────────────────────

class TestPidIsAlive:
    def test_self_is_alive(self):
        assert process_lock._pid_is_alive(os.getpid()) is True

    def test_zero_pid_is_dead(self):
        assert process_lock._pid_is_alive(0) is False

    def test_negative_pid_is_dead(self):
        assert process_lock._pid_is_alive(-5) is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX os.kill semantics")
    def test_process_lookup_error_is_dead(self, monkeypatch):
        def boom(pid, sig):
            raise ProcessLookupError()
        monkeypatch.setattr(process_lock.os, "kill", boom)
        assert process_lock._pid_is_alive(424242) is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX os.kill semantics")
    def test_permission_error_treated_as_live(self, monkeypatch):
        def boom(pid, sig):
            raise PermissionError()
        monkeypatch.setattr(process_lock.os, "kill", boom)
        # Owned by another user => conservatively live.
        assert process_lock._pid_is_alive(424242) is True

    @pytest.mark.skipif(os.name != "posix", reason="POSIX os.kill semantics")
    def test_oserror_esrch_is_dead(self, monkeypatch):
        def boom(pid, sig):
            raise OSError(errno.ESRCH, "no such process")
        monkeypatch.setattr(process_lock.os, "kill", boom)
        assert process_lock._pid_is_alive(424242) is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX os.kill semantics")
    def test_oserror_other_is_unknown(self, monkeypatch):
        def boom(pid, sig):
            raise OSError(errno.EFAULT, "weird")
        monkeypatch.setattr(process_lock.os, "kill", boom)
        # Can't tell => fail-open None.
        assert process_lock._pid_is_alive(424242) is None

    def test_non_posix_psutil_missing_is_unknown(self, monkeypatch):
        monkeypatch.setattr(process_lock.os, "name", "nt")
        # Force `import psutil` to raise ImportError.
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert process_lock._pid_is_alive(424242) is None

    def test_non_posix_psutil_reports_alive(self, monkeypatch):
        monkeypatch.setattr(process_lock.os, "name", "nt")
        fake = type("P", (), {"pid_exists": staticmethod(lambda pid: True)})()
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert process_lock._pid_is_alive(424242) is True

    def test_non_posix_psutil_reports_dead(self, monkeypatch):
        monkeypatch.setattr(process_lock.os, "name", "nt")
        fake = type("P", (), {"pid_exists": staticmethod(lambda pid: False)})()
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert process_lock._pid_is_alive(424242) is False

    def test_non_posix_psutil_raises_is_unknown(self, monkeypatch):
        monkeypatch.setattr(process_lock.os, "name", "nt")
        def explode(pid):
            raise RuntimeError("boom")
        fake = type("P", (), {"pid_exists": staticmethod(explode)})()
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert process_lock._pid_is_alive(424242) is None


# ── acquire / release happy path ─────────────────────────────────────────

class TestAcquireRelease:
    def test_acquire_creates_lockfile(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        assert p.exists()
        assert lock.owned is True
        assert lock.path == p
        lock.release()

    def test_lockfile_payload_format(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        contents = p.read_text(encoding="utf-8").strip()
        pid_str, start_str, host = contents.split("|")
        assert int(pid_str) == os.getpid()
        assert float(start_str) > 0
        assert host == socket.gethostname()
        lock.release()

    def test_release_removes_lockfile(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        lock.release()
        assert not p.exists()
        assert lock.owned is False

    def test_release_idempotent(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        lock.release()
        # Second release is a no-op, must not raise.
        lock.release()
        assert lock.owned is False

    def test_release_noop_when_never_acquired(self, tmp_path):
        lock = ProcessLock(tmp_path / "internets.pid")
        lock.release()  # _owned False, _path None
        assert lock.owned is False
        assert lock.path is None


# ── contention: a second live holder is refused ──────────────────────────

class TestContention:
    def test_second_acquire_raises_lockheld(self, tmp_path):
        p = tmp_path / "internets.pid"
        first = ProcessLock(p)
        first.acquire()
        try:
            second = ProcessLock(p)
            with pytest.raises(LockHeld) as ei:
                second.acquire()
            assert "held by pid" in str(ei.value)
            assert ei.value.reason  # reason attribute populated
            assert second.owned is False
        finally:
            first.release()

    def test_lockheld_does_not_remove_existing_lock(self, tmp_path):
        p = tmp_path / "internets.pid"
        first = ProcessLock(p)
        first.acquire()
        try:
            with pytest.raises(LockHeld):
                ProcessLock(p).acquire()
            # The original lock must survive a refused contender.
            assert p.exists()
            assert first.owned is True
        finally:
            first.release()


# ── stale-lock reclaim ───────────────────────────────────────────────────

class TestStaleReclaim:
    def test_dead_pid_lock_is_reclaimed(self, tmp_path, monkeypatch):
        p = tmp_path / "internets.pid"
        _plant(p, pid=99999)  # same host, fabricated pid
        monkeypatch.setattr(process_lock, "_pid_is_alive", lambda pid: False)
        lock = ProcessLock(p)
        lock.acquire()  # stale => reclaimed
        assert lock.owned is True
        # Reclaimed file now carries OUR pid.
        assert int(p.read_text().split("|")[0]) == os.getpid()
        lock.release()

    def test_unknown_liveness_fails_open_and_acquires(self, tmp_path, monkeypatch):
        p = tmp_path / "internets.pid"
        _plant(p, pid=99999)
        # None => can't tell => fail-open, take the lock.
        monkeypatch.setattr(process_lock, "_pid_is_alive", lambda pid: None)
        lock = ProcessLock(p)
        lock.acquire()
        assert lock.owned is True
        assert int(p.read_text().split("|")[0]) == os.getpid()
        lock.release()

    def test_live_pid_lock_is_refused(self, tmp_path, monkeypatch):
        p = tmp_path / "internets.pid"
        _plant(p, pid=99999)
        monkeypatch.setattr(process_lock, "_pid_is_alive", lambda pid: True)
        with pytest.raises(LockHeld):
            ProcessLock(p).acquire()
        # Lock left intact.
        assert int(p.read_text().split("|")[0]) == 99999


# ── different host is conservatively refused ─────────────────────────────

class TestDifferentHost:
    def test_other_host_refused_even_if_pid_probed_dead(self, tmp_path, monkeypatch):
        p = tmp_path / "internets.pid"
        _plant(p, pid=99999, host="some-other-host")
        # Even if the probe WOULD say dead, a different host short-circuits
        # to alive=True before probing.
        called = {"n": 0}
        def probe(pid):
            called["n"] += 1
            return False
        monkeypatch.setattr(process_lock, "_pid_is_alive", probe)
        with pytest.raises(LockHeld) as ei:
            ProcessLock(p).acquire()
        assert "some-other-host" in str(ei.value)
        assert called["n"] == 0  # never probed the foreign host


# ── corrupt / unreadable lockfiles are removed and acquired ──────────────

class TestCorruptLockfile:
    def test_empty_lockfile_is_reclaimed(self, tmp_path):
        p = tmp_path / "internets.pid"
        p.write_text("", encoding="utf-8")
        lock = ProcessLock(p)
        lock.acquire()
        assert lock.owned is True
        lock.release()

    def test_garbage_lockfile_is_reclaimed(self, tmp_path):
        p = tmp_path / "internets.pid"
        p.write_text("not-a-pid-at-all\n", encoding="utf-8")
        lock = ProcessLock(p)
        lock.acquire()
        assert lock.owned is True
        assert int(p.read_text().split("|")[0]) == os.getpid()
        lock.release()


# ── release guard: only unlink when the file still holds OUR pid ──────────

class TestReleasePidGuard:
    def test_release_noop_on_pid_mismatch(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        # Someone else overwrote the lockfile with a different pid.
        _plant(p, pid=os.getpid() + 1)
        lock.release()
        # We must NOT have unlinked another owner's lockfile.
        assert p.exists()
        assert lock.owned is False
        assert int(p.read_text().split("|")[0]) == os.getpid() + 1

    def test_release_handles_missing_file(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        p.unlink()  # vanished underneath us
        lock.release()  # read fails -> contents "" -> file_pid -1 -> no-op, no raise
        assert lock.owned is False

    def test_release_handles_corrupt_file(self, tmp_path):
        p = tmp_path / "internets.pid"
        lock = ProcessLock(p)
        lock.acquire()
        p.write_text("garbage-no-pid\n", encoding="utf-8")
        lock.release()  # file_pid -1 != ours -> not released
        assert p.exists()
        assert lock.owned is False


# ── os.open error branches map to LockHeld ───────────────────────────────

class TestOpenErrors:
    def test_missing_parent_dir_raises_lockheld(self, tmp_path):
        # FileNotFoundError (OSError, not FileExistsError) => LockHeld.
        p = tmp_path / "does_not_exist" / "internets.pid"
        with pytest.raises(LockHeld) as ei:
            ProcessLock(p).acquire()
        assert "could not create lockfile" in str(ei.value)

    def test_file_exists_race_raises_lockheld(self, tmp_path, monkeypatch):
        p = tmp_path / "internets.pid"
        def boom(*a, **k):
            raise FileExistsError()
        monkeypatch.setattr(process_lock.os, "open", boom)
        with pytest.raises(LockHeld) as ei:
            ProcessLock(p).acquire()
        assert "appeared during acquire" in str(ei.value)


# ── path resolution at acquire() time ────────────────────────────────────

class TestPathResolution:
    def test_relative_path_resolved_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        lock = ProcessLock(Path("rel.pid"))
        lock.acquire()
        assert lock.path == tmp_path / "rel.pid"
        assert (tmp_path / "rel.pid").exists()
        lock.release()

    def test_default_path_is_internets_pid(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        lock = ProcessLock()  # None -> ./internets.pid
        lock.acquire()
        assert lock.path == tmp_path / "internets.pid"
        lock.release()

    def test_absolute_path_unchanged(self, tmp_path):
        p = tmp_path / "abs.pid"
        lock = ProcessLock(p)
        lock.acquire()
        assert lock.path == p
        lock.release()

    def test_path_none_before_acquire(self, tmp_path):
        assert ProcessLock(tmp_path / "x.pid").path is None


# ── _read_existing field parsing ─────────────────────────────────────────

class TestReadExisting:
    def _lock_for(self, path):
        lock = ProcessLock(path)
        lock._path = path
        return lock

    def test_full_record(self, tmp_path):
        p = tmp_path / "x.pid"
        p.write_text("4321|1700.250|myhost\n")
        lock = self._lock_for(p)
        assert lock._read_existing() == (4321, 1700.250, "myhost")

    def test_pid_only(self, tmp_path):
        p = tmp_path / "x.pid"
        p.write_text("4321\n")
        lock = self._lock_for(p)
        assert lock._read_existing() == (4321, 0.0, "")

    def test_bad_start_defaults_zero(self, tmp_path):
        p = tmp_path / "x.pid"
        p.write_text("4321|notafloat|host\n")
        lock = self._lock_for(p)
        assert lock._read_existing() == (4321, 0.0, "host")

    def test_non_int_pid_returns_none(self, tmp_path):
        p = tmp_path / "x.pid"
        p.write_text("xyz|1.0|host\n")
        lock = self._lock_for(p)
        assert lock._read_existing() is None

    def test_empty_returns_none(self, tmp_path):
        p = tmp_path / "x.pid"
        p.write_text("   \n")
        lock = self._lock_for(p)
        assert lock._read_existing() is None

    def test_missing_file_returns_none(self, tmp_path):
        p = tmp_path / "absent.pid"
        lock = self._lock_for(p)
        assert lock._read_existing() is None

    def test_read_existing_without_path_raises(self):
        lock = ProcessLock(Path("x"))
        # _path stays None until acquire().
        with pytest.raises(RuntimeError):
            lock._read_existing()


# ── context manager ──────────────────────────────────────────────────────

class TestContextManager:
    def test_enter_acquires_exit_releases(self, tmp_path):
        p = tmp_path / "internets.pid"
        with ProcessLock(p) as lock:
            assert lock.owned is True
            assert p.exists()
        assert not p.exists()
        assert lock.owned is False

    def test_exit_does_not_swallow_exception(self, tmp_path):
        p = tmp_path / "internets.pid"
        with pytest.raises(ValueError):
            with ProcessLock(p):
                raise ValueError("boom")
        # Lock still released on the way out.
        assert not p.exists()

    def test_enter_under_contention_raises(self, tmp_path):
        p = tmp_path / "internets.pid"
        holder = ProcessLock(p)
        holder.acquire()
        try:
            with pytest.raises(LockHeld):
                with ProcessLock(p):
                    pass
        finally:
            holder.release()


# ── _safe_unlink ─────────────────────────────────────────────────────────

class TestSafeUnlink:
    def test_unlink_missing_is_silent(self, tmp_path):
        ProcessLock._safe_unlink(tmp_path / "nope")  # no raise

    def test_unlink_existing(self, tmp_path):
        p = tmp_path / "f"
        p.write_text("x")
        ProcessLock._safe_unlink(p)
        assert not p.exists()

    def test_unlink_oserror_swallowed(self, tmp_path, monkeypatch):
        p = tmp_path / "f"
        p.write_text("x")
        def boom(self):
            raise OSError(errno.EACCES, "denied")
        monkeypatch.setattr(Path, "unlink", boom)
        ProcessLock._safe_unlink(p)  # logged, not raised
