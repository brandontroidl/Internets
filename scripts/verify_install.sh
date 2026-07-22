#!/usr/bin/env bash
#
# verify_install.sh - supply-chain install smoke test.
#
# 1. Build sdist + wheel via `python -m build`.
# 2. Create a throw-away venv.
# 3. Install the wheel into it.
# 4. Verify that every file installed by the wheel matches the SHA-256
#    hash recorded in the wheel's RECORD metadata (catches tampering or
#    a broken extractor).
# 5. Smoke-test the package: import + version string + console entry
#    point resolvable.
#
# Usage:
#   ./scripts/verify_install.sh
#
# Exit code 0 == verified, non-zero == something is off.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

PYTHON="${PYTHON:-python3}"

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "error: ${PYTHON} not found on PATH" >&2
    exit 127
fi

echo "[verify] using $(${PYTHON} --version 2>&1) at $(command -v ${PYTHON})"

# ---- 1. Build ----------------------------------------------------------
echo "[verify] cleaning dist/"
rm -rf dist/ build/ ./*.egg-info

echo "[verify] installing 'build' into an isolated env and building"
"${PYTHON}" -m pip install --quiet --upgrade pip build
"${PYTHON}" -m build

WHEEL="$(ls -1 dist/*.whl 2>/dev/null | head -n1 || true)"
SDIST="$(ls -1 dist/*.tar.gz 2>/dev/null | head -n1 || true)"

if [[ -z "${WHEEL}" || -z "${SDIST}" ]]; then
    echo "error: build did not produce both a wheel and an sdist" >&2
    ls -la dist/ >&2 || true
    exit 1
fi
echo "[verify] built ${WHEEL}"
echo "[verify] built ${SDIST}"

# ---- 2. Throw-away venv ------------------------------------------------
TMPDIR_VENV="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_VENV}"' EXIT

"${PYTHON}" -m venv "${TMPDIR_VENV}/venv"
# shellcheck disable=SC1091
source "${TMPDIR_VENV}/venv/bin/activate"

python -m pip install --quiet --upgrade pip

# ---- 3. Install the wheel ---------------------------------------------
echo "[verify] installing wheel into temp venv"
python -m pip install --quiet "${WHEEL}"

# Everything from here on must resolve against the INSTALLED WHEEL, never the
# source tree.  Python puts the current directory on sys.path, so with cwd at
# the repo root `import internets` loads internets.py from source and
# importlib.metadata finds the stale internets_irc.egg-info/ (which carries no
# RECORD) instead of the venv's dist-info.  Both made this gate inspect the
# thing it was supposed to be checking against.  That is how a wheel missing
# audit_log, process_lock and metrics passed and shipped in v3.0.0 and v4.0.0.
cd "${TMPDIR_VENV}"

# ---- 4. Verify installed-file hashes match RECORD ---------------------
echo "[verify] checking installed-file hashes against RECORD"
python - <<'PY'
import base64
import hashlib
import sys
from importlib.metadata import distribution

dist = distribution("internets-irc")
record = dist.read_text("RECORD")
if record is None:
    sys.exit("error: distribution has no RECORD metadata")

base = dist.locate_file("")
errs = 0
checked = 0

for line in record.splitlines():
    if not line.strip():
        continue
    parts = line.rsplit(",", 2)
    if len(parts) != 3:
        continue
    path, hashspec, _size = parts
    if not hashspec:
        # RECORD itself, .pth files, etc. - skip per PEP 376.
        continue
    algo, _, b64 = hashspec.partition("=")
    if algo != "sha256" or not b64:
        continue
    target = base / path
    if not target.is_file():
        print(f"  MISSING  {path}")
        errs += 1
        continue
    h = hashlib.sha256(target.read_bytes()).digest()
    expected = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    if h != expected:
        print(f"  MISMATCH {path}")
        errs += 1
    checked += 1

print(f"[verify] hash-checked {checked} files; {errs} problem(s)")
sys.exit(1 if errs else 0)
PY

# ---- 5. Smoke test ----------------------------------------------------
# config.py reads config.ini from the CWD at import time and exits if it is
# absent, so stage one here the way an operator would.  config.ini.example is
# deliberately not shipped in the wheel - the bot is installed from a checkout,
# which is what `secret_store init`'s own error message tells you.
cp "${ROOT}/config.ini.example" "${TMPDIR_VENV}/config.ini"

echo "[verify] import + version smoke test (from outside the repo)"
python -c "import internets, sys; print('internets', internets.__version__); assert internets.__version__"

echo "[verify] every declared top-level module imports from the wheel"
python - <<'PY'
import importlib
import sys

# Anything imported at module scope by the entry path must be in the wheel.
REQUIRED = [
    "internets", "config", "botlog", "admin_cmds", "console", "sender",
    "store", "protocol", "hashpw", "secret_store",
    "audit_log", "process_lock", "metrics",
]
missing = []
for name in REQUIRED:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as e:
        # Only a missing module ITSELF is a packaging failure; a missing
        # optional third-party dep is a different problem and not this gate's.
        if e.name == name:
            missing.append(name)
    except Exception:
        # Imported fine, then failed on config/runtime state. Packaging is OK.
        pass
if missing:
    print(f"[verify] NOT PACKAGED: {', '.join(missing)}")
    print("[verify] add them to [tool.setuptools] py-modules in pyproject.toml")
    sys.exit(1)
print(f"[verify] all {len(REQUIRED)} top-level modules present in the wheel")
PY

echo "[verify] console entry point resolves"
python -c "import importlib.metadata as md; eps=md.entry_points(group='console_scripts'); names=[e.name for e in eps]; assert 'internets' in names, f'missing entry point; got {names}'"

deactivate

echo "[verify] OK"
