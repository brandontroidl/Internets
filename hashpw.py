#!/usr/bin/env python3
"""
hashpw.py - generate a hashed admin password for config.ini

    python hashpw.py
    python hashpw.py --algo bcrypt
    python hashpw.py --algo argon2

Algorithms (preference order, strongest first):

    argon2id   pip install argon2-cffi    RECOMMENDED - memory-hard, side-channel
                                          resistant, OWASP 2024 first choice.
    scrypt     stdlib, no extra packages  Strong; memory-hard but older design.
    bcrypt     pip install bcrypt         OK; CPU-bound only - weaker vs GPU/ASIC
                                          than the two above.

The default CLI algorithm remains ``scrypt`` for backwards-compatibility with
existing deployments (changing it would not invalidate old hashes - the
stored ``algo$rest`` format carries its own algorithm tag - but operators
expect the default to be stable).  New deployments SHOULD pass ``--algo argon2``.

Parameter tuning env vars:
    INTERNETS_ARGON2_MEM_MIB    memory cost in MiB        (default: 128)
    INTERNETS_ARGON2_TIME       time-cost iterations      (default: 3)
    INTERNETS_BCRYPT_ROUNDS     bcrypt log2(rounds) cost  (default: 13)

References:
    OWASP Password Storage Cheat Sheet (2024 revision):
      Argon2id  ≥ 19 MiB / t=2 / p=1 minimum; 64 MiB+ recommended for sensitive
                services.  We default to 128 MiB / t=3 / p=4 for desktop-class
                resistance vs commodity 2026 GPUs / ASICs.
      scrypt    N ≥ 2**17 (131 072) recommended; we probe down only if OpenSSL
                refuses the cost (memory cap or FIPS).
      bcrypt    cost ≥ 12; we default to 13 (~10× slower than 12 on 2026 CPUs).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import logging
import os
import sys
import time
from typing import Callable

log = logging.getLogger("internets.hashpw")


# ── Argon2id parameter selection ──────────────────────────────────────
#
# OWASP 2024 password storage cheat sheet recommends Argon2id with a
# minimum of 19 MiB / t=2 / p=1, but explicitly notes higher memory is
# preferable on servers that can spare it.  Commodity 2026 GPUs (e.g.
# RTX 50-series) can churn ~10 KH/s against argon2id at 64 MiB / t=3;
# doubling memory to 128 MiB roughly halves that throughput because the
# GPU's on-chip memory bandwidth becomes the bottleneck.  Time-cost is a
# secondary multiplier and stacks linearly.
#
# Tunable via env so operators on small VMs can dial back without
# editing source.  Caps prevent footgun values (terabyte allocations
# that OOM the bot).

_ARGON2_DEFAULT_MEM_MIB = 128          # 128 MiB
_ARGON2_DEFAULT_TIME    = 3
_ARGON2_PARALLELISM     = 4            # typical desktop core count
_ARGON2_HASH_LEN        = 32
_ARGON2_SALT_LEN        = 16

_ARGON2_MEM_MIN_MIB = 19               # OWASP 2024 floor
_ARGON2_MEM_MAX_MIB = 4096             # 4 GiB hard cap - anything more is
                                       # almost certainly a misconfiguration
_ARGON2_TIME_MIN    = 1
_ARGON2_TIME_MAX    = 20


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Read an int env var clamped to ``[lo, hi]``; fall back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        log.warning("hashpw: %s=%r is not an integer; using default %d",
                    name, raw, default)
        return default
    if v < lo or v > hi:
        log.warning("hashpw: %s=%d outside allowed range [%d, %d]; clamping",
                    name, v, lo, hi)
        v = max(lo, min(hi, v))
    return v


def _argon2_params() -> tuple[int, int, int]:
    """Resolve (memory_cost_kib, time_cost, parallelism) from env+defaults.

    ``memory_cost`` for argon2-cffi is in KiB.  We expose the env var in
    MiB because that's the unit operators reason about.
    """
    mem_mib = _env_int("INTERNETS_ARGON2_MEM_MIB",
                       _ARGON2_DEFAULT_MEM_MIB,
                       _ARGON2_MEM_MIN_MIB, _ARGON2_MEM_MAX_MIB)
    t_cost  = _env_int("INTERNETS_ARGON2_TIME",
                       _ARGON2_DEFAULT_TIME,
                       _ARGON2_TIME_MIN, _ARGON2_TIME_MAX)
    return mem_mib * 1024, t_cost, _ARGON2_PARALLELISM


# ── Password policy (shared by the CLI and the live auth path) ────────
#
# Denominated in UTF-8 BYTES, not characters.  Every downstream bound is a
# byte bound - the 512-byte IRC frame (sender.py), the bot's _MAX_ARG_LEN
# command-argument cap, and the .encode() every hash function performs - so a
# len() check in code points silently admits a 128-character non-ASCII
# passphrase that is 384 bytes on the wire and cannot be transmitted.

MIN_PASSWORD_LEN = 8

# Must stay <= internets.IRCBot._MAX_ARG_LEN, which the dispatcher enforces
# before the auth handler ever runs; a larger value here would make the
# auth-side guard unreachable dead code.  Pinned by a test.
MAX_PASSWORD_BYTES = 128

# bcrypt ignores every byte past 72.  This is not a tunable cost parameter,
# it is the algorithm's hard input limit, and both ways of hitting it are
# unacceptable:
#   * bcrypt < 5.0 silently TRUNCATES, so any password sharing the stored
#     one's first 72 bytes authenticates.  That is an auth bypass, and it is
#     silent - the operator believes a 100-character passphrase is in force
#     when only its first 72 bytes are.
#   * bcrypt >= 5.0 raises ValueError, which without this guard escapes
#     hash_bcrypt as an uncaught traceback.
# We refuse at hash time instead, so an over-long password can never be
# turned into a hash that under-protects the account.
BCRYPT_MAX_PASSWORD_BYTES = 72


def check_password(pw: str, algo: str = "") -> str | None:
    """Validate a candidate password.  Returns an error string, or None if OK.

    One implementation shared by ``main`` (hash time) and the bot's auth
    handler (verify time) so the two can never drift apart - they previously
    disagreed by 8x, which let an operator create a password that hashed
    successfully and could then never authenticate.

    *algo* enables the algorithm-specific limit; pass the algorithm name when
    it is known, omit it for a generic check.
    """
    if len(pw) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    nbytes = len(pw.encode("utf-8"))
    if nbytes > MAX_PASSWORD_BYTES:
        return (f"Password too long ({nbytes} bytes, max "
                f"{MAX_PASSWORD_BYTES}).")
    if algo == "bcrypt" and nbytes > BCRYPT_MAX_PASSWORD_BYTES:
        return (f"Password too long for bcrypt ({nbytes} bytes, max "
                f"{BCRYPT_MAX_PASSWORD_BYTES} - bcrypt ignores the rest). "
                f"Use --algo argon2 for a longer passphrase.")
    if pw != pw.strip():
        # The bot strips a command argument before dispatch, so a password
        # with leading or trailing whitespace hashes fine here and then can
        # never be sent over IRC.  Reject it at creation rather than let the
        # operator discover it while locked out.
        return "Password must not start or end with whitespace."
    return None


# ── scrypt ────────────────────────────────────────────────────────────

# OWASP 2024: scrypt N ≥ 2**17 (131 072), r=8, p=1.  We probe downward
# only if the host's OpenSSL build refuses the cost (it enforces a
# per-process memory cap via ``maxmem``; the stdlib wrapper inherits it).
# Don't touch the order of this list without re-reading the comment -
# the degradation chain is a deliberate cliff from "OWASP-strong" through
# "RFC-default" down to "still better than plaintext".

def _best_scrypt_params() -> tuple[int, int, int]:
    """Probe for the strongest scrypt (N, r, p) the current OpenSSL allows.

    N must be a power of two and ≤ 2**20 per RFC 7914.  We start at 2**17
    (OWASP 2024 recommended) and walk down only if the kernel/OpenSSL
    refuses (typically because of the per-process memory cap).
    """
    salt = os.urandom(16)
    for N, r, p in [
        (131072, 8, 1),   # 2**17 - OWASP 2024 recommended
        ( 65536, 8, 1),   # 2**16 - historical "strong"
        ( 32768, 8, 1),   # 2**15 - RFC 7914 default
        ( 16384, 8, 2),
        ( 16384, 8, 1),
        (  8192, 8, 2),
        (  8192, 8, 1),
        (  4096, 8, 1),   # weakest acceptable fallback
    ]:
        try:
            hashlib.scrypt(b"probe", salt=salt, n=N, r=r, p=p, dklen=16)
            return N, r, p
        except (ValueError, OSError, MemoryError):
            continue
    raise RuntimeError("scrypt failed on every param set - try --algo bcrypt or --algo argon2")


def hash_scrypt(password: str) -> str:
    """Hash *password* with scrypt (stdlib, no extra packages).

    Prefer ``hash_argon2`` for new deployments - argon2id resists GPU/ASIC
    attacks better.  scrypt remains the default only for compatibility.
    """
    N, r, p = _best_scrypt_params()
    salt    = os.urandom(32)
    dk      = hashlib.scrypt(password.encode(), salt=salt, n=N, r=r, p=p, dklen=64)
    return f"scrypt${N}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


# ── bcrypt ────────────────────────────────────────────────────────────

_BCRYPT_DEFAULT_ROUNDS = 13            # OWASP 2024: ≥12; we default to 13.
_BCRYPT_MIN_ROUNDS     = 10            # hard floor
_BCRYPT_MAX_ROUNDS     = 16            # >16 takes several seconds per call


def _bcrypt_rounds() -> int:
    return _env_int("INTERNETS_BCRYPT_ROUNDS",
                    _BCRYPT_DEFAULT_ROUNDS,
                    _BCRYPT_MIN_ROUNDS, _BCRYPT_MAX_ROUNDS)


def hash_bcrypt(password: str) -> str:
    """Hash *password* with bcrypt (requires ``pip install bcrypt``).

    Cost is configurable via ``INTERNETS_BCRYPT_ROUNDS`` (default: 13).
    Prefer argon2id or scrypt - bcrypt is CPU-bound only, so it gains
    nothing against an attacker with FPGA/ASIC hardware.
    """
    try:
        import bcrypt
    except ImportError:
        sys.exit("bcrypt not installed - run: pip install bcrypt")
    raw = password.encode()
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        # Defence in depth: main() screens this via check_password, but
        # hash_bcrypt is also called directly (tests, any future caller) and
        # must never silently produce a truncated hash.  See the constant.
        raise ValueError(
            f"bcrypt ignores input past {BCRYPT_MAX_PASSWORD_BYTES} bytes; "
            f"got {len(raw)}. Hashing this would silently protect only the "
            f"first {BCRYPT_MAX_PASSWORD_BYTES} bytes.")
    rounds = _bcrypt_rounds()
    return f"bcrypt${bcrypt.hashpw(raw, bcrypt.gensalt(rounds=rounds)).decode()}"


# ── argon2id ──────────────────────────────────────────────────────────

def hash_argon2(password: str) -> str:
    """Hash *password* with argon2id (requires ``pip install argon2-cffi``).

    RECOMMENDED for new deployments.  Parameters follow OWASP 2024:
    memory-hard (128 MiB default), time_cost=3, parallelism=4.  Tune via
    ``INTERNETS_ARGON2_MEM_MIB`` and ``INTERNETS_ARGON2_TIME``.
    """
    try:
        from argon2 import PasswordHasher
    except ImportError:
        sys.exit("argon2-cffi not installed - run: pip install argon2-cffi")
    mem_kib, t_cost, p = _argon2_params()
    ph = PasswordHasher(
        time_cost=t_cost, memory_cost=mem_kib, parallelism=p,
        hash_len=_ARGON2_HASH_LEN, salt_len=_ARGON2_SALT_LEN,
    )
    return f"argon2${ph.hash(password)}"


# ── Verification (dispatch on prefix) ─────────────────────────────────

def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against a stored hash.  Supports scrypt, bcrypt, argon2.

    The stored hash carries its own params (cost, salt, etc.) so bumping
    the defaults above never invalidates existing hashes - new hashes get
    the new params on next ``set``, old hashes continue to verify with
    their embedded params until the user re-sets.  See KEY_ROTATION.md.
    """
    if not stored:
        raise ValueError("No password hash configured.")
    if stored.startswith("scrypt$"):
        return _verify_scrypt(password, stored)
    if stored.startswith("bcrypt$"):
        return _verify_bcrypt(password, stored)
    if stored.startswith("argon2$"):
        return _verify_argon2(password, stored)
    raise ValueError(
        "Unrecognised hash format - must start with 'scrypt$', 'bcrypt$', or 'argon2$'"
    )


def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        _, N, r, p, salt_b64, dk_b64 = stored.split("$", 5)
        salt     = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual   = hashlib.scrypt(
            password.encode(), salt=salt,
            n=int(N), r=int(r), p=int(p), dklen=len(expected),
        )
        return _ct_eq(actual, expected)
    except (ValueError, OSError, MemoryError):
        return False


def _verify_bcrypt(password: str, stored: str) -> bool:
    try:
        import bcrypt
    except ImportError as e:
        raise ValueError(f"bcrypt not installed - run: pip install bcrypt ({e})")
    try:
        return bcrypt.checkpw(password.encode(), stored.split("$", 1)[1].encode())
    except (ValueError, TypeError, IndexError):
        return False


def _verify_argon2(password: str, stored: str) -> bool:
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    except ImportError as e:
        raise ValueError(f"argon2-cffi not installed - run: pip install argon2-cffi ({e})")
    try:
        return PasswordHasher().verify(stored.split("$", 1)[1], password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, IndexError):
        return False


def _ct_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


_ALGOS: dict[str, Callable[[str], str]] = {
    "scrypt": hash_scrypt,
    "bcrypt": hash_bcrypt,
    "argon2": hash_argon2,
}
_NOTES: dict[str, str] = {
    "scrypt": "stdlib, no extra packages",
    "bcrypt": "requires: pip install bcrypt",
    "argon2": "requires: pip install argon2-cffi (RECOMMENDED)",
}


# ── Self-test / benchmark ─────────────────────────────────────────────

# A single hash that takes <50 ms is essentially free for an attacker on
# a 2026 GPU farm - flag it.  Anything >1 s blocks login latency and we
# back off automatically (drop memory by 25%, then time_cost by 1).

_FAST_HASH_THRESHOLD_S = 0.050
_SLOW_HASH_THRESHOLD_S = 1.000


def main() -> None:
    """CLI entry point - prompt for password, hash it, and print config.ini snippet."""
    parser = argparse.ArgumentParser(description="Generate an admin password hash for Internets.")
    parser.add_argument("--algo", choices=_ALGOS, default="scrypt",
                        help="Hashing algorithm (default: scrypt - but argon2 is RECOMMENDED)")
    args = parser.parse_args()

    if args.algo != "argon2":
        print("\nNOTE: argon2id is the OWASP-recommended choice for new deployments.")
        print(f"      You picked '{args.algo}'; consider --algo argon2 next time.\n")

    print(f"\nInternets password hasher - {args.algo} ({_NOTES[args.algo]})\n")

    pw  = getpass.getpass("Password  : ")
    pw2 = getpass.getpass("Confirm   : ")
    if pw != pw2:
        sys.exit("Passwords do not match.")
    if err := check_password(pw, args.algo):
        sys.exit(err)

    print("Hashing ...", end=" ", flush=True)
    t0 = time.monotonic()
    hashed = _ALGOS[args.algo](pw)
    dt = time.monotonic() - t0
    if args.algo == "scrypt":
        parts = hashed.split("$")
        print(f"done in {dt:.2f}s (N={parts[1]}, r={parts[2]}, p={parts[3]})\n")
    elif args.algo == "argon2":
        mem_kib, t_cost, p = _argon2_params()
        print(f"done in {dt:.2f}s (mem={mem_kib // 1024} MiB, t={t_cost}, p={p})\n")
    elif args.algo == "bcrypt":
        print(f"done in {dt:.2f}s (rounds={_bcrypt_rounds()})\n")
    else:
        print(f"done in {dt:.2f}s\n")

    # Surface latency anomalies regardless of algo.
    if dt < _FAST_HASH_THRESHOLD_S:
        print(f"WARNING: hash took only {dt:.3f}s - parameters may be too weak "
              "for 2026 GPU/ASIC attackers.")
    elif dt > _SLOW_HASH_THRESHOLD_S:
        print(f"NOTE: hash took {dt:.2f}s - if this is too slow for login UX, "
              "lower the cost via env vars (see module docstring).")

    print("─" * 72)
    print("Add to config.ini under [admin]:\n")
    print(f"    password_hash = {hashed}")
    print("─" * 72)

    if not verify_password(pw, hashed):
        sys.exit("Self-test FAILED: verify returned False")
    if verify_password("wrong", hashed):
        sys.exit("Self-test FAILED: false positive")
    print("\nSelf-test passed ✓")


if __name__ == "__main__":
    main()
