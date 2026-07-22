"""Tests for hashpw.py - admin password hashing utility + CLI.

Exercises the real scrypt/bcrypt/argon2 code paths (all three deps are
installed in this env), the env-var parameter tuning + clamping, the
verify dispatch and its fail-closed branches, and the ``main()`` CLI via
monkeypatched ``getpass``.  argon2/bcrypt params are dialled to their
minimums via env so the real hash calls stay fast.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import hashpw


# ── speed knobs: pin cost params to their floors for fast real hashing ──

@pytest.fixture
def fast_costs(monkeypatch):
    monkeypatch.setenv("INTERNETS_ARGON2_MEM_MIB", "19")  # OWASP floor
    monkeypatch.setenv("INTERNETS_ARGON2_TIME", "1")
    monkeypatch.setenv("INTERNETS_BCRYPT_ROUNDS", "10")   # hard floor


# ── _env_int ────────────────────────────────────────────────────────────

class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("INTERNETS_X", raising=False)
        assert hashpw._env_int("INTERNETS_X", 7, 1, 10) == 7

    def test_blank_returns_default(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_X", "   ")
        assert hashpw._env_int("INTERNETS_X", 7, 1, 10) == 7

    def test_valid_in_range(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_X", "5")
        assert hashpw._env_int("INTERNETS_X", 7, 1, 10) == 5

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_X", "notanint")
        assert hashpw._env_int("INTERNETS_X", 7, 1, 10) == 7

    def test_below_range_clamps_to_lo(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_X", "0")
        assert hashpw._env_int("INTERNETS_X", 7, 3, 10) == 3

    def test_above_range_clamps_to_hi(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_X", "999")
        assert hashpw._env_int("INTERNETS_X", 7, 3, 10) == 10


# ── _argon2_params ──────────────────────────────────────────────────────

class TestArgon2Params:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("INTERNETS_ARGON2_MEM_MIB", raising=False)
        monkeypatch.delenv("INTERNETS_ARGON2_TIME", raising=False)
        mem_kib, t, p = hashpw._argon2_params()
        assert mem_kib == hashpw._ARGON2_DEFAULT_MEM_MIB * 1024
        assert t == hashpw._ARGON2_DEFAULT_TIME
        assert p == hashpw._ARGON2_PARALLELISM

    def test_env_override_mem_is_mib_to_kib(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_ARGON2_MEM_MIB", "64")
        monkeypatch.setenv("INTERNETS_ARGON2_TIME", "2")
        mem_kib, t, p = hashpw._argon2_params()
        assert mem_kib == 64 * 1024
        assert t == 2

    def test_mem_below_floor_clamps(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_ARGON2_MEM_MIB", "1")
        mem_kib, _, _ = hashpw._argon2_params()
        assert mem_kib == hashpw._ARGON2_MEM_MIN_MIB * 1024


# ── _bcrypt_rounds ──────────────────────────────────────────────────────

class TestBcryptRounds:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("INTERNETS_BCRYPT_ROUNDS", raising=False)
        assert hashpw._bcrypt_rounds() == hashpw._BCRYPT_DEFAULT_ROUNDS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_BCRYPT_ROUNDS", "11")
        assert hashpw._bcrypt_rounds() == 11

    def test_above_cap_clamps(self, monkeypatch):
        monkeypatch.setenv("INTERNETS_BCRYPT_ROUNDS", "99")
        assert hashpw._bcrypt_rounds() == hashpw._BCRYPT_MAX_ROUNDS


# ── _best_scrypt_params ─────────────────────────────────────────────────

class TestBestScryptParams:
    def test_returns_power_of_two_n(self):
        N, r, p = hashpw._best_scrypt_params()
        assert N & (N - 1) == 0          # power of two
        assert N <= 131072
        assert r == 8
        assert p >= 1

    def test_raises_when_all_param_sets_fail(self, monkeypatch):
        def always_fail(*a, **k):
            raise ValueError("nope")
        monkeypatch.setattr(hashpw.hashlib, "scrypt", always_fail)
        with pytest.raises(RuntimeError):
            hashpw._best_scrypt_params()


# ── _ct_eq ──────────────────────────────────────────────────────────────

class TestCtEq:
    def test_equal(self):
        assert hashpw._ct_eq(b"abc", b"abc") is True

    def test_unequal(self):
        assert hashpw._ct_eq(b"abc", b"abd") is False


# ── scrypt hash + verify roundtrip ──────────────────────────────────────

class TestScrypt:
    def test_hash_format(self):
        h = hashpw.hash_scrypt("hunter2pw")
        assert h.startswith("scrypt$")
        # scrypt$N$r$p$salt$dk  -> 6 fields
        assert len(h.split("$")) == 6

    def test_salt_is_random(self):
        assert hashpw.hash_scrypt("samepw") != hashpw.hash_scrypt("samepw")

    def test_verify_correct(self):
        h = hashpw.hash_scrypt("correct horse")
        assert hashpw.verify_password("correct horse", h) is True

    def test_verify_wrong(self):
        h = hashpw.hash_scrypt("correct horse")
        assert hashpw.verify_password("wrong", h) is False

    def test_verify_malformed_returns_false(self):
        # Too few fields -> split unpack raises -> caught -> False.
        assert hashpw._verify_scrypt("pw", "scrypt$onlyonefield") is False

    def test_verify_bad_base64_returns_false(self):
        bad = "scrypt$131072$8$1$!!!notb64!!!$alsoinvalid"
        assert hashpw._verify_scrypt("pw", bad) is False


# ── bcrypt hash + verify roundtrip ──────────────────────────────────────

class TestBcrypt:
    def test_hash_format(self, fast_costs):
        h = hashpw.hash_bcrypt("hunter2pw")
        assert h.startswith("bcrypt$")

    def test_verify_correct(self, fast_costs):
        h = hashpw.hash_bcrypt("s3cret!!")
        assert hashpw.verify_password("s3cret!!", h) is True

    def test_verify_wrong(self, fast_costs):
        h = hashpw.hash_bcrypt("s3cret!!")
        assert hashpw.verify_password("nope", h) is False

    def test_verify_garbage_returns_false(self):
        # Not a valid bcrypt payload -> ValueError caught -> False.
        assert hashpw._verify_bcrypt("pw", "bcrypt$not-a-real-hash") is False


# ── argon2 hash + verify roundtrip ──────────────────────────────────────

class TestArgon2:
    def test_hash_format(self, fast_costs):
        h = hashpw.hash_argon2("hunter2pw")
        assert h.startswith("argon2$")
        # underlying argon2-cffi encoding is appended.
        assert "$argon2" in h

    def test_verify_correct(self, fast_costs):
        h = hashpw.hash_argon2("p@ssw0rd!")
        assert hashpw.verify_password("p@ssw0rd!", h) is True

    def test_verify_wrong(self, fast_costs):
        h = hashpw.hash_argon2("p@ssw0rd!")
        assert hashpw.verify_password("different", h) is False

    def test_verify_invalid_hash_returns_false(self):
        assert hashpw._verify_argon2("pw", "argon2$garbage") is False


# ── verify_password dispatch + guard branches ───────────────────────────

class TestVerifyDispatch:
    def test_empty_stored_raises(self):
        with pytest.raises(ValueError):
            hashpw.verify_password("pw", "")

    def test_none_stored_raises(self):
        with pytest.raises(ValueError):
            hashpw.verify_password("pw", None)  # type: ignore[arg-type]

    def test_unrecognised_format_raises(self):
        with pytest.raises(ValueError):
            hashpw.verify_password("pw", "md5$deadbeef")

    def test_cross_algo_wrong_password_is_false_not_error(self):
        # A well-formed scrypt hash verified against the wrong pw is False,
        # never an exception.
        h = hashpw.hash_scrypt("realpw123")
        assert hashpw.verify_password("guess", h) is False


# ── registries stay consistent ──────────────────────────────────────────

class TestRegistries:
    def test_algos_and_notes_cover_same_keys(self):
        assert set(hashpw._ALGOS) == set(hashpw._NOTES)
        assert set(hashpw._ALGOS) == {"scrypt", "bcrypt", "argon2"}


# ── main() CLI ──────────────────────────────────────────────────────────

def _patch_getpass(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(hashpw.getpass, "getpass", lambda prompt="": next(it))


class TestMainCLI:
    def test_scrypt_happy_path_self_test_passes(self, monkeypatch, capsys):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "scrypt"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        hashpw.main()  # must not raise
        out = capsys.readouterr().out
        assert "password_hash = scrypt$" in out
        assert "Self-test passed" in out

    def test_argon2_happy_path(self, monkeypatch, capsys, fast_costs):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "argon2"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        hashpw.main()
        out = capsys.readouterr().out
        assert "password_hash = argon2$" in out
        assert "Self-test passed" in out

    def test_bcrypt_happy_path(self, monkeypatch, capsys, fast_costs):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "bcrypt"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        hashpw.main()
        out = capsys.readouterr().out
        assert "password_hash = bcrypt$" in out
        assert "Self-test passed" in out

    def test_non_argon2_prints_recommendation_note(self, monkeypatch, capsys):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "scrypt"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        hashpw.main()
        out = capsys.readouterr().out
        assert "argon2id is the OWASP-recommended choice" in out

    def test_mismatch_exits(self, monkeypatch):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py"])
        _patch_getpass(monkeypatch, "passwordA", "passwordB")
        with pytest.raises(SystemExit) as ei:
            hashpw.main()
        assert "do not match" in str(ei.value)

    def test_too_short_exits(self, monkeypatch):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py"])
        _patch_getpass(monkeypatch, "short", "short")
        with pytest.raises(SystemExit) as ei:
            hashpw.main()
        assert "at least 8" in str(ei.value)

    def test_too_long_exits(self, monkeypatch):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py"])
        huge = "x" * 1025
        _patch_getpass(monkeypatch, huge, huge)
        with pytest.raises(SystemExit) as ei:
            hashpw.main()
        assert "too long" in str(ei.value)

    def test_unknown_algo_rejected_by_argparse(self, monkeypatch):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "md5"])
        with pytest.raises(SystemExit):
            hashpw.main()

    def test_fast_hash_warning(self, monkeypatch, capsys):
        # Force the hash to look instant so the "too weak" warning fires.
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "scrypt"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        times = iter([100.0, 100.0])  # t0, then dt == 0.0 < threshold
        monkeypatch.setattr(hashpw.time, "monotonic", lambda: next(times))
        hashpw.main()
        out = capsys.readouterr().out
        assert "parameters may be too weak" in out

    def test_slow_hash_note(self, monkeypatch, capsys):
        monkeypatch.setattr(hashpw.sys, "argv", ["hashpw.py", "--algo", "scrypt"])
        _patch_getpass(monkeypatch, "goodpassword", "goodpassword")
        times = iter([100.0, 102.0])  # dt == 2.0 > slow threshold
        monkeypatch.setattr(hashpw.time, "monotonic", lambda: next(times))
        hashpw.main()
        out = capsys.readouterr().out
        assert "too slow for login UX" in out


# ── password policy: byte-denominated limits and the bcrypt 72-byte wall ──

class TestPasswordPolicy:
    """bcrypt ignores everything past 72 bytes.

    Installed bcrypt 4.x silently TRUNCATES, so a password sharing the stored
    one's first 72 bytes authenticates - an auth bypass, not a cosmetic limit.
    bcrypt 5.x raises instead, which surfaces as an uncaught traceback out of
    hash_bcrypt. Neither is acceptable, so the limit is enforced here.
    """

    def test_bcrypt_refuses_past_its_72_byte_wall(self, fast_costs):
        with pytest.raises(ValueError, match="72"):
            hashpw.hash_bcrypt("a" * 73)

    def test_bcrypt_accepts_exactly_72_bytes(self, fast_costs):
        # The allow branch: a guard verified only on deny is half-verified.
        h = hashpw.hash_bcrypt("a" * 72)
        assert hashpw.verify_password("a" * 72, h) is True

    def test_over_long_bcrypt_password_can_never_be_hashed(self, fast_costs):
        """The bypass, closed at its only closable point.

        Two passwords differing only after byte 72 hash identically. The guard
        refuses to create such a hash at all, so the operator never ends up
        with an account protected by fewer bytes than they chose.

        DOCUMENTED RESIDUAL: this cannot close the VERIFY side. bcrypt
        truncates the candidate too, so for an already-stored bcrypt hash any
        longer string sharing its first 72 bytes still authenticates. Fixing
        that requires rejecting over-long candidates in verify_password, which
        would lock out an operator whose existing password is over-long - an
        auth-posture change, deliberately not made here.
        """
        base = "a" * 72
        with pytest.raises(ValueError, match="72"):
            hashpw.hash_bcrypt(base + "CORRECT-TAIL")
        with pytest.raises(ValueError, match="72"):
            hashpw.hash_bcrypt(base + "DIFFERENT-TAIL")

    def test_check_password_limits_are_byte_denominated(self):
        # 128 CJK characters is 384 UTF-8 bytes - over the limit even though
        # len() says 128. Every downstream bound (IRC framing, _MAX_ARG_LEN)
        # is in bytes, so the check must be too.
        assert hashpw.check_password("\u6f22" * 128) is not None
        assert hashpw.check_password("a" * hashpw.MAX_PASSWORD_BYTES) is None

    def test_check_password_rejects_untypeable_whitespace(self):
        # internets.py strips the command argument before dispatch, so a
        # password with edge whitespace can never be transmitted over IRC.
        assert hashpw.check_password(" leading-space-pw") is not None
        assert hashpw.check_password("trailing-space-pw ") is not None

    def test_check_password_enforces_the_bcrypt_wall_per_algo(self):
        long_pw = "a" * 100
        assert hashpw.check_password(long_pw, "bcrypt") is not None
        assert hashpw.check_password(long_pw, "argon2") is None

    def test_check_password_rejects_too_short(self):
        assert hashpw.check_password("short") is not None

    def test_cap_cannot_be_shadowed_by_the_dispatch_guard(self):
        """Ordering property, not a tautology.

        internets.py rejects any command argument over _MAX_ARG_LEN before the
        handler runs. If the password cap ever exceeded that, the auth-side
        guard would become unreachable dead code.
        """
        # Read the constant from source rather than importing internets:
        # config.py parses argv at import time and would consume pytest's.
        # This mirrors the repo's existing source-inspection guards.
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "internets.py").read_text()
        m = re.search(r"^\s*_MAX_ARG_LEN\s*=\s*(\d+)", src, re.M)
        assert m, "internets.py no longer defines _MAX_ARG_LEN"
        assert hashpw.MAX_PASSWORD_BYTES <= int(m.group(1))

