#!/usr/bin/env python3
"""
hashpw.py — generate a hashed admin password for config.ini

    python hashpw.py
    python hashpw.py --algo bcrypt

Algorithms:
    scrypt   stdlib, no extra packages  (default)
    bcrypt   pip install bcrypt
    argon2   pip install argon2-cffi
"""

import sys
import os
import base64
import getpass
import argparse


def _best_scrypt_params():
    """
    Find the strongest scrypt params the current OpenSSL build allows.

    Memory cost = N * r * 128 bytes. The ceiling varies widely:
      Linux/glibc OpenSSL 1.x: typically allows N=131072 (128 MB)
      Arch/Fedora OpenSSL 3.x: 32 MB cap → need N=16384 r=8 p=2
      macOS LibreSSL:          usually N=131072
      Windows/WSL:             WSL2 usually fine, native Windows varies

    Different builds raise ValueError, OSError, or MemoryError for the
    same limit, so we catch all three.
    """
    import hashlib
    salt = os.urandom(16)
    for N, r, p in [
        (131072, 8, 1),
        ( 65536, 8, 1),
        ( 32768, 8, 1),
        ( 16384, 8, 2),
        ( 16384, 8, 1),
        (  8192, 8, 2),
        (  8192, 8, 1),
        (  4096, 8, 1),
    ]:
        try:
            hashlib.scrypt(b"probe", salt=salt, n=N, r=r, p=p, dklen=16)
            return N, r, p
        except (ValueError, OSError, MemoryError):
            continue
    raise RuntimeError("scrypt failed on every param set — try --algo bcrypt or --algo argon2")


def hash_scrypt(password: str) -> str:
    import hashlib
    N, r, p  = _best_scrypt_params()
    salt     = os.urandom(32)
    dk       = hashlib.scrypt(password.encode(), salt=salt, n=N, r=r, p=p, dklen=64)
    return f"scrypt${N}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def hash_bcrypt(password: str) -> str:
    try:
        import bcrypt
    except ImportError:
        sys.exit("bcrypt not installed — run: pip install bcrypt")
    return f"bcrypt${bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()}"


def hash_argon2(password: str) -> str:
    try:
        from argon2 import PasswordHasher
    except ImportError:
        sys.exit("argon2-cffi not installed — run: pip install argon2-cffi")
    ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
    return f"argon2${ph.hash(password)}"


def verify_password(password: str, stored: str) -> bool:
    """
    Verify a plaintext password against a stored hash string.
    Raises ValueError for unknown algorithm prefixes or missing libraries.
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
        "Unrecognised hash format — must start with 'scrypt$', 'bcrypt$', or 'argon2$'"
    )


def _verify_scrypt(password: str, stored: str) -> bool:
    import hashlib
    try:
        _, N, r, p, salt_b64, dk_b64 = stored.split("$", 5)
        salt     = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual   = hashlib.scrypt(
            password.encode(), salt=salt,
            n=int(N), r=int(r), p=int(p), dklen=len(expected),
        )
        return _ct_eq(actual, expected)
    except Exception:
        return False


def _verify_bcrypt(password: str, stored: str) -> bool:
    try:
        import bcrypt
    except ImportError:
        raise ValueError("bcrypt not installed — run: pip install bcrypt")
    try:
        return bcrypt.checkpw(password.encode(), stored.split("$", 1)[1].encode())
    except Exception:
        return False


def _verify_argon2(password: str, stored: str) -> bool:
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    except ImportError:
        raise ValueError("argon2-cffi not installed — run: pip install argon2-cffi")
    try:
        return PasswordHasher().verify(stored.split("$", 1)[1], password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        return False


def _ct_eq(a: bytes, b: bytes) -> bool:
    """Constant-time bytes comparison — prevents timing side-channels."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0


_ALGOS = {"scrypt": hash_scrypt, "bcrypt": hash_bcrypt, "argon2": hash_argon2}
_NOTES = {
    "scrypt": "stdlib, no extra packages",
    "bcrypt": "requires: pip install bcrypt",
    "argon2": "requires: pip install argon2-cffi",
}


def main():
    parser = argparse.ArgumentParser(description="Generate an admin password hash for Internets.")
    parser.add_argument("--algo", choices=_ALGOS, default="scrypt",
                        help="Hashing algorithm (default: scrypt)")
    args = parser.parse_args()

    print(f"\nInternets password hasher — {args.algo} ({_NOTES[args.algo]})\n")

    pw  = getpass.getpass("Password  : ")
    pw2 = getpass.getpass("Confirm   : ")
    if pw != pw2:
        sys.exit("Passwords do not match.")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters.")

    print("Hashing ...", end=" ", flush=True)
    hashed = _ALGOS[args.algo](pw)
    if args.algo == "scrypt":
        parts = hashed.split("$")
        print(f"done  (N={parts[1]}, r={parts[2]}, p={parts[3]})\n")
    else:
        print("done\n")

    print("─" * 72)
    print("Add to config.ini under [admin]:\n")
    print(f"    password_hash = {hashed}")
    print("─" * 72)

    assert verify_password(pw, hashed),          "self-test failed: verify returned False"
    assert not verify_password("wrong", hashed), "self-test failed: false positive"
    print("\nSelf-test passed ✓")


if __name__ == "__main__":
    main()
