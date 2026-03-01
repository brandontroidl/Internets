#!/usr/bin/env python3
"""
hashpw.py — generate a hashed admin password for Internets's config.ini

Usage:
    python hashpw.py              # interactive, prompts for password
    python hashpw.py --algo bcrypt

Supported algorithms:
    scrypt    — Python stdlib, no extra packages needed  (default)
    bcrypt    — pip install bcrypt
    argon2    — pip install argon2-cffi

The output is a single string to paste as the value of:
    [admin]
    password_hash = <paste here>
"""

import sys
import argparse
import getpass
import base64
import os

# ─── Algorithm implementations ────────────────────────────────────────────────

def _best_scrypt_params() -> tuple:
    """
    Probe for the strongest scrypt params that actually work on this system.

    Memory cost = N * r * 128 bytes.  Platforms vary widely:
      - Linux (glibc/OpenSSL 1.x): usually allows N=131072 (128MB)
      - Arch / Fedora (OpenSSL 3.x): 32MB cap → N=16384 r=8 p=2
      - macOS (LibreSSL):            usually allows N=131072
      - Windows / WSL:               depends on build; WSL2 often fine,
                                     native Windows may cap lower
      - Older Pythons (<3.6):        scrypt may be unavailable entirely

    We catch ValueError, OSError, and MemoryError since different
    OpenSSL/LibreSSL builds raise different exceptions for the same limit.
    Falls back gracefully all the way to N=4096 before giving up.
    """
    import hashlib
    salt = os.urandom(16)
    candidates = [
        (131072, 8, 1),   # 128MB — ideal OWASP
        (65536,  8, 1),   # 64MB
        (32768,  8, 1),   # 32MB
        (16384,  8, 2),   # 32MB via parallelism (Arch/OpenSSL 3.x)
        (16384,  8, 1),   # 16MB
        (8192,   8, 2),   # 16MB via parallelism
        (8192,   8, 1),   # 8MB  — WSL1 / constrained Windows
        (4096,   8, 1),   # 4MB  — last resort, still better than bcrypt cost=10
    ]
    for N, r, p in candidates:
        try:
            hashlib.scrypt(b"probe", salt=salt, n=N, r=r, p=p, dklen=16)
            return N, r, p
        except (ValueError, OSError, MemoryError):
            continue
    raise RuntimeError(
        "scrypt failed on all parameter sets. "
        "Try --algo bcrypt or --algo argon2 instead."
    )


def hash_scrypt(password: str) -> str:
    """
    Format:  scrypt$<N>$<r>$<p>$<b64-salt>$<b64-hash>
    Automatically selects the strongest parameters OpenSSL allows.
    """
    import hashlib
    N, r, p = _best_scrypt_params()
    salt     = os.urandom(32)
    dk       = hashlib.scrypt(password.encode(), salt=salt, n=N, r=r, p=p, dklen=64)
    salt_b64 = base64.b64encode(salt).decode()
    dk_b64   = base64.b64encode(dk).decode()
    return f"scrypt${N}${r}${p}${salt_b64}${dk_b64}"


def hash_bcrypt(password: str) -> str:
    """
    Format:  bcrypt$<bcrypt-encoded-string>
    Uses cost factor 12.
    """
    try:
        import bcrypt
    except ImportError:
        sys.exit("bcrypt is not installed. Run: pip install bcrypt")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))
    return f"bcrypt${hashed.decode()}"


def hash_argon2(password: str) -> str:
    """
    Format:  argon2$<argon2-encoded-string>
    Uses argon2-cffi with OWASP-recommended params.
    """
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError
    except ImportError:
        sys.exit("argon2-cffi is not installed. Run: pip install argon2-cffi")
    ph     = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
    hashed = ph.hash(password)
    return f"argon2${hashed}"

# ─── Verify (used by the bot, importable from here) ───────────────────────────

def verify_password(password: str, stored_hash: str) -> bool:
    """
    Verify a plaintext password against a stored hash string.
    Raises ValueError if the algorithm prefix is unknown or a required
    library is not installed.
    """
    if not stored_hash:
        raise ValueError("No password hash is configured.")

    if stored_hash.startswith("scrypt$"):
        return _verify_scrypt(password, stored_hash)
    elif stored_hash.startswith("bcrypt$"):
        return _verify_bcrypt(password, stored_hash)
    elif stored_hash.startswith("argon2$"):
        return _verify_argon2(password, stored_hash)
    else:
        raise ValueError(
            "Unrecognised hash format. password_hash must start with "
            "'scrypt$', 'bcrypt$', or 'argon2$'."
        )


def _verify_scrypt(password: str, stored: str) -> bool:
    import hashlib
    try:
        _, N, r, p, salt_b64, dk_b64 = stored.split("$", 5)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual   = hashlib.scrypt(
            password.encode(), salt=salt,
            n=int(N), r=int(r), p=int(p), dklen=len(expected)
        )
        # Constant-time comparison
        return _ct_compare(actual, expected)
    except Exception:
        return False


def _verify_bcrypt(password: str, stored: str) -> bool:
    try:
        import bcrypt
    except ImportError:
        raise ValueError("bcrypt is not installed. Run: pip install bcrypt")
    try:
        _, hash_str = stored.split("$", 1)
        return bcrypt.checkpw(password.encode(), hash_str.encode())
    except Exception:
        return False


def _verify_argon2(password: str, stored: str) -> bool:
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    except ImportError:
        raise ValueError("argon2-cffi is not installed. Run: pip install argon2-cffi")
    try:
        _, hash_str = stored.split("$", 1)
        ph = PasswordHasher()
        return ph.verify(hash_str, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        return False


def _ct_compare(a: bytes, b: bytes) -> bool:
    """Constant-time bytes comparison to prevent timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0

# ─── CLI ──────────────────────────────────────────────────────────────────────

ALGOS = {
    "scrypt": hash_scrypt,
    "bcrypt": hash_bcrypt,
    "argon2": hash_argon2,
}

NOTES = {
    "scrypt": "stdlib — no extra packages needed",
    "bcrypt": "requires: pip install bcrypt",
    "argon2": "requires: pip install argon2-cffi  (recommended)",
}


def main():
    parser = argparse.ArgumentParser(
        description="Generate a hashed admin password for Internets."
    )
    parser.add_argument(
        "--algo", choices=ALGOS.keys(), default="scrypt",
        help="Hashing algorithm (default: scrypt)"
    )
    args = parser.parse_args()

    print(f"\nInternets password hasher")
    print(f"Algorithm : {args.algo}  ({NOTES[args.algo]})\n")

    pw  = getpass.getpass("Enter admin password  : ")
    pw2 = getpass.getpass("Confirm password      : ")

    if pw != pw2:
        sys.exit("Passwords do not match.")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters.")

    print("\nHashing... ", end="", flush=True)
    hashed = ALGOS[args.algo](pw)
    # Show which scrypt params were selected
    if args.algo == "scrypt":
        parts = hashed.split("$")
        print(f"done (N={parts[1]}, r={parts[2]}, p={parts[3]}).\n")
    else:
        print("done.\n")

    print("─" * 70)
    print("Paste this into config.ini under [admin]:\n")
    print(f"password_hash = {hashed}")
    print("─" * 70)

    # Quick self-test
    assert verify_password(pw, hashed), "Self-verification failed!"
    assert not verify_password("wrongpassword", hashed), "False positive!"
    print("\nSelf-test passed. ✓")


if __name__ == "__main__":
    main()
