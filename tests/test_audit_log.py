"""Tests for audit_log.py - tamper-evident HMAC-chained audit log.

Covers the pure hashing helpers, the AuditLog record/verify/count chain,
key handling (generation, 0600 perms, fail-closed on unreadable, backup +
regenerate on malformed/short), tampering detection, rotation, legacy
SHA-256 verify fallback, and the module singleton.

Everything runs against tmp_path; no real ./audit.log is touched.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import audit_log
from audit_log import AuditLog


IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "audit.log"


@pytest.fixture
def alog(log_path):
    return AuditLog(log_path)


# ── pure helpers ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_iso_utc_now_has_z_suffix(self):
        s = audit_log._iso_utc_now()
        assert s.endswith("Z")
        # YYYY-mm-ddTHH:MM:SS.ffffff + Z
        assert "T" in s and len(s) == 27

    def test_stable_args_none_is_empty(self):
        assert audit_log._stable_args_str(None) == ""

    def test_stable_args_str_passthrough(self):
        assert audit_log._stable_args_str("hello world") == "hello world"

    def test_stable_args_dict_is_sorted_compact(self):
        s = audit_log._stable_args_str({"b": 1, "a": 2})
        assert s == '{"a":2,"b":1}'

    def test_stable_args_dict_order_independent(self):
        a = audit_log._stable_args_str({"x": 1, "y": 2})
        b = audit_log._stable_args_str({"y": 2, "x": 1})
        assert a == b

    def test_stable_args_unserializable_falls_back_to_repr(self):
        circular: list = []
        circular.append(circular)  # json.dumps raises ValueError on this
        s = audit_log._stable_args_str(circular)
        assert s == repr(circular)

    def test_stable_args_set_uses_default_str(self):
        # A set is not natively JSON-serializable but default=str handles it,
        # so this takes the json.dumps branch (not the repr fallback).
        s = audit_log._stable_args_str({1})
        assert s == '"{1}"'

    def test_canonical_is_nul_separated_utf8(self):
        b = audit_log._canonical("p", "t", "a", "h", "act", "args")
        assert b == b"p\x00t\x00a\x00h\x00act\x00args"

    def test_sha_and_hmac_differ(self):
        sha = audit_log._sha_record("p", "t", "a", "h", "act", "x")
        mac = audit_log._hmac_record(b"k" * 32, "p", "t", "a", "h", "act", "x")
        assert sha != mac
        assert len(sha) == 64 and len(mac) == 64

    def test_hmac_key_dependent(self):
        m1 = audit_log._hmac_record(b"k" * 32, "p", "t", "a", "h", "act", "x")
        m2 = audit_log._hmac_record(b"j" * 32, "p", "t", "a", "h", "act", "x")
        assert m1 != m2

    def test_is_jsonable(self):
        assert audit_log._is_jsonable({"a": 1}) is True
        assert audit_log._is_jsonable([1, 2]) is True
        assert audit_log._is_jsonable({1, 2}) is False  # set


# ── construction ─────────────────────────────────────────────────────────

class TestConstruction:
    def test_paths_resolved(self, tmp_path):
        a = AuditLog(tmp_path / "audit.log")
        assert a.path == (tmp_path / "audit.log").resolve()
        assert a._key_path.name == "audit.log.key"

    def test_default_relative_path(self):
        a = AuditLog()
        assert a.path.name == "audit.log"


# ── record + chain ───────────────────────────────────────────────────────

class TestRecord:
    def test_record_returns_64_hex(self, alog):
        h = alog.record("nick", "n@host", "kick", "reason")
        assert len(h) == 64
        int(h, 16)  # valid hex

    def test_record_creates_file_and_key(self, alog):
        alog.record("nick", "n@host", "op")
        assert alog.path.exists()
        assert alog._key_path.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX perm bits only")
    def test_log_and_key_are_0600(self, alog):
        alog.record("nick", "n@host", "op")
        assert stat.S_IMODE(alog.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(alog._key_path.stat().st_mode) == 0o600

    def test_first_record_links_to_genesis(self, alog):
        alog.record("nick", "n@host", "op")
        line = alog.path.read_text().splitlines()[0]
        obj = json.loads(line)
        assert obj["prev_hash"] == audit_log._GENESIS_HASH
        assert obj["v"] == audit_log._RECORD_VERSION

    def test_chain_links_records(self, alog):
        h1 = alog.record("a", "a@h", "one")
        h2 = alog.record("b", "b@h", "two")
        lines = alog.path.read_text().splitlines()
        o1, o2 = json.loads(lines[0]), json.loads(lines[1])
        assert o1["this_hash"] == h1
        assert o2["prev_hash"] == h1
        assert o2["this_hash"] == h2

    def test_args_dict_preserved_as_object(self, alog):
        alog.record("a", "a@h", "set", {"k": "v", "n": 3})
        obj = json.loads(alog.path.read_text().splitlines()[0])
        assert obj["args"] == {"k": "v", "n": 3}

    def test_args_nonjsonable_stored_as_string(self, alog):
        alog.record("a", "a@h", "set", {1, 2})
        obj = json.loads(alog.path.read_text().splitlines()[0])
        assert isinstance(obj["args"], str)

    def test_actor_coerced_to_str(self, alog):
        alog.record(12345, "h", "op", None)
        obj = json.loads(alog.path.read_text().splitlines()[0])
        assert obj["actor"] == "12345"

    def test_tip_persists_across_instances(self, log_path):
        a = AuditLog(log_path)
        h1 = a.record("a", "a@h", "one")
        # New instance must pick up the existing tip from disk.
        b = AuditLog(log_path)
        b.record("b", "b@h", "two")
        o2 = json.loads(log_path.read_text().splitlines()[1])
        assert o2["prev_hash"] == h1


# ── verify ───────────────────────────────────────────────────────────────

class TestVerify:
    def test_empty_missing_file_verifies(self, alog):
        assert alog.verify() == (True, -1)

    def test_intact_chain_verifies(self, alog):
        for i in range(5):
            alog.record(f"n{i}", "h", "act", {"i": i})
        assert alog.verify() == (True, -1)

    def test_blank_lines_tolerated(self, alog):
        alog.record("a", "h", "one")
        alog.record("b", "h", "two")
        with alog.path.open("a") as f:
            f.write("\n\n")
        assert alog.verify() == (True, -1)

    def test_tampered_field_breaks_verify(self, alog):
        alog.record("a", "h", "one")
        alog.record("b", "h", "two")
        lines = alog.path.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["action"] = "tampered"  # this_hash now stale
        lines[1] = json.dumps(obj, separators=(",", ":"))
        alog.path.write_text("\n".join(lines) + "\n")
        ok, idx = alog.verify()
        assert ok is False
        assert idx == 1

    def test_deleted_record_breaks_chain(self, alog):
        alog.record("a", "h", "one")
        alog.record("b", "h", "two")
        alog.record("c", "h", "three")
        lines = alog.path.read_text().splitlines()
        # Drop the middle record -> record 2's prev_hash no longer matches.
        del lines[1]
        alog.path.write_text("\n".join(lines) + "\n")
        ok, idx = alog.verify()
        assert ok is False
        assert idx == 1

    def test_corrupt_json_line_breaks_verify(self, alog):
        alog.record("a", "h", "one")
        with alog.path.open("a") as f:
            f.write("{not json\n")
        ok, idx = alog.verify()
        assert ok is False
        assert idx == 1

    def test_non_dict_json_breaks_verify(self, alog):
        alog.record("a", "h", "one")
        with alog.path.open("a") as f:
            f.write("[1,2,3]\n")
        ok, idx = alog.verify()
        assert ok is False
        assert idx == 1

    def test_wrong_key_fails_verify(self, log_path):
        a = AuditLog(log_path)
        a.record("a", "h", "one")
        a.record("b", "h", "two")
        # Replace the key with a different valid 32-byte key.
        a._key_path.write_text(("ab" * 32))
        b = AuditLog(log_path)  # fresh instance reads the swapped key
        ok, idx = b.verify()
        assert ok is False
        assert idx == 0

    def test_legacy_sha_record_verifies(self, log_path):
        # Hand-craft a pre-3.0.0 record (no "v" field, plain SHA-256).
        ts = audit_log._iso_utc_now()
        prev = audit_log._GENESIS_HASH
        this = audit_log._sha_record(prev, ts, "old", "h", "act", "x")
        entry = {
            "ts": ts, "actor": "old", "host": "h", "action": "act",
            "args": "x", "prev_hash": prev, "this_hash": this,
        }
        log_path.write_text(json.dumps(entry, separators=(",", ":")) + "\n")
        a = AuditLog(log_path)
        assert a.verify() == (True, -1)

    def test_legacy_then_v2_mixed_chain(self, log_path):
        a = AuditLog(log_path)
        # Genesis-linked legacy record.
        ts = audit_log._iso_utc_now()
        prev = audit_log._GENESIS_HASH
        this = audit_log._sha_record(prev, ts, "old", "h", "act", "x")
        entry = {
            "ts": ts, "actor": "old", "host": "h", "action": "act",
            "args": "x", "prev_hash": prev, "this_hash": this,
        }
        log_path.write_text(json.dumps(entry, separators=(",", ":")) + "\n")
        # Append a v2 record continuing the chain.
        a.record("new", "h", "act2")
        assert a.verify() == (True, -1)


# ── count ────────────────────────────────────────────────────────────────

class TestCount:
    def test_count_empty(self, alog):
        assert alog.count() == 0

    def test_count_records(self, alog):
        alog.record("a", "h", "one")
        alog.record("b", "h", "two")
        assert alog.count() == 2

    def test_count_ignores_blank_lines(self, alog):
        alog.record("a", "h", "one")
        with alog.path.open("a") as f:
            f.write("\n\n")
        assert alog.count() == 1


# ── key handling ─────────────────────────────────────────────────────────

class TestKeyHandling:
    def test_key_generated_once_and_cached(self, alog):
        k1 = alog._load_key()
        k2 = alog._load_key()
        assert k1 is k2
        assert len(k1) == 32

    def test_key_reloaded_from_disk(self, log_path):
        a = AuditLog(log_path)
        k = a._load_key()
        b = AuditLog(log_path)
        assert b._load_key() == k

    def test_short_key_backed_up_and_regenerated(self, log_path):
        a = AuditLog(log_path)
        # Write a too-short (1-byte) key.
        a._key_path.write_text("00")
        key = a._load_key()
        assert len(key) == 32  # regenerated to full length
        # Old key moved aside, not overwritten in place.
        assert a._key_path.with_name(a._key_path.name + ".bad").exists()

    def test_nonhex_key_backed_up_and_regenerated(self, log_path):
        a = AuditLog(log_path)
        a._key_path.write_text("zzzznothex")
        key = a._load_key()
        assert len(key) == 32
        assert a._key_path.with_name(a._key_path.name + ".bad").exists()

    @pytest.mark.skipif(os.name == "nt" or IS_ROOT,
                        reason="needs POSIX perms and non-root")
    def test_unreadable_key_fails_closed(self, log_path):
        a = AuditLog(log_path)
        a._key_path.write_text("ab" * 32)
        os.chmod(a._key_path, 0o000)
        try:
            b = AuditLog(log_path)  # fresh instance: cache empty
            with pytest.raises(RuntimeError, match="refusing to overwrite"):
                b._load_key()
        finally:
            os.chmod(a._key_path, 0o600)

    @pytest.mark.skipif(os.name == "nt" or IS_ROOT,
                        reason="needs POSIX perms and non-root")
    def test_record_raises_on_unreadable_key(self, log_path):
        a = AuditLog(log_path)
        a._key_path.write_text("ab" * 32)
        os.chmod(a._key_path, 0o000)
        try:
            b = AuditLog(log_path)
            with pytest.raises(RuntimeError):
                b.record("a", "h", "act")
        finally:
            os.chmod(a._key_path, 0o600)


# ── _load_tip internals ──────────────────────────────────────────────────

class TestLoadTip:
    def test_tip_genesis_when_missing(self, alog):
        assert alog._load_tip() == audit_log._GENESIS_HASH

    def test_tip_skips_blank_lines(self, log_path):
        a = AuditLog(log_path)
        h = a.record("a", "h", "one")
        with log_path.open("a") as f:
            f.write("\n")
        b = AuditLog(log_path)
        assert b._load_tip() == h

    def test_tip_stops_at_corrupt_line(self, log_path):
        a = AuditLog(log_path)
        h1 = a.record("a", "h", "one")
        with log_path.open("a") as f:
            f.write("{garbage\n")
        # Corruption is a terminating boundary: tip is the last good hash.
        b = AuditLog(log_path)
        assert b._load_tip() == h1

    def test_tip_ignores_record_without_valid_hash(self, log_path):
        a = AuditLog(log_path)
        h1 = a.record("a", "h", "one")
        # A JSON line lacking a 64-char this_hash does not advance the tip.
        with log_path.open("a") as f:
            f.write(json.dumps({"this_hash": "short"}) + "\n")
        b = AuditLog(log_path)
        assert b._load_tip() == h1


# ── rotation ─────────────────────────────────────────────────────────────

class TestRotation:
    def test_rotates_when_oversize(self, log_path, monkeypatch):
        monkeypatch.setattr(audit_log, "_MAX_BYTES", 200)
        a = AuditLog(log_path)
        # First record creates the file; subsequent records eventually exceed
        # the cap and trigger a rotation on a later record() call.
        for i in range(20):
            a.record(f"nick{i}", "host@example", "action", {"i": i})
        rotated = list(log_path.parent.glob("audit.log.2*"))
        assert rotated, "expected at least one rotated segment"

    def test_rotated_segment_independently_verifies(self, log_path, monkeypatch):
        monkeypatch.setattr(audit_log, "_MAX_BYTES", 200)
        a = AuditLog(log_path)
        for i in range(20):
            a.record(f"nick{i}", "host@example", "action", {"i": i})
        # The live log starts a fresh genesis chain post-rotation.
        assert a.verify() == (True, -1)
        # Each rotated segment is its own genesis-rooted chain.
        for seg in log_path.parent.glob("audit.log.2*"):
            seg_log = AuditLog(seg)
            # Reuse the same key file for the rotated segment's verify.
            seg_log._key_path = a._key_path
            assert seg_log.verify() == (True, -1)


# ── singleton ────────────────────────────────────────────────────────────

class TestDefault:
    def test_default_is_singleton(self, monkeypatch):
        monkeypatch.setattr(audit_log, "_default_instance", None)
        a = audit_log.default()
        b = audit_log.default()
        assert a is b
        assert isinstance(a, AuditLog)
