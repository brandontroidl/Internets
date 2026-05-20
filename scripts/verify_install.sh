#!/usr/bin/env bash
#
# verify_install.sh — supply-chain install smoke test.
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
        # RECORD itself, .pth files, etc. — skip per PEP 376.
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
echo "[verify] import + version smoke test"
python -c "import internets, sys; print('internets', internets.__version__); assert internets.__version__"

echo "[verify] console entry point resolves"
python -c "import importlib.metadata as md; eps=md.entry_points(group='console_scripts'); names=[e.name for e in eps]; assert 'internets' in names, f'missing entry point; got {names}'"

deactivate

echo "[verify] OK"
