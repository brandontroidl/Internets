#!/usr/bin/env bash
#
# Regenerate requirements.lock with hash-pinning.
#
# Run this whenever requirements.txt changes (Dependabot, manual bump,
# or any pip dep edit).  Commit both requirements.txt and
# requirements.lock together - CI installs with --require-hashes
# against the lockfile, so the two must stay in sync.
#
# Why it matters: hash-pinning defends against PyPI account-takeover,
# dependency confusion, and a poisoned wheel being silently installed
# in place of the one you intended.
#
# Usage:
#     scripts/regen-lockfile.sh
#
# Requirements:
#   * Python 3.10 SPECIFICALLY on PATH - the lock MUST be resolved on the
#     lowest supported Python so conditional transitive deps gated
#     `python_version < "3.11"` (e.g. async-timeout) are captured.  A lock
#     generated on 3.14 silently omits them and breaks CI's 3.10 jobs.
#   * Network access to PyPI for the resolve step

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Locate a Python 3.10 interpreter - fail loudly rather than silently
# producing a lock missing the < 3.11 conditional transitives.
PYBIN=""
for cand in python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if [ "$ver" = "3.10" ]; then PYBIN="$cand"; break; fi
    fi
done
if [ -z "$PYBIN" ]; then
    echo "ERROR: Python 3.10 is required to regenerate the lockfile." >&2
    echo "  The lock must be resolved on the lowest supported Python so" >&2
    echo "  conditional transitive deps (async-timeout, etc.) are captured." >&2
    echo "  Install Python 3.10 and re-run." >&2
    exit 1
fi
echo ">> Using $PYBIN ($("$PYBIN" --version 2>&1))"

VENV="$(mktemp -d)/piptools-venv"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

echo ">> Creating ephemeral venv at $VENV"
"$PYBIN" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip pip-tools

echo ">> Generating requirements.lock with hashes"
"$VENV/bin/pip-compile" \
    --generate-hashes \
    --resolver=backtracking \
    --output-file=requirements.lock \
    --strip-extras \
    --no-emit-options \
    requirements.txt

LINES="$(wc -l < requirements.lock)"
echo ">> requirements.lock regenerated ($LINES lines)"
echo ">> Commit BOTH requirements.txt and requirements.lock together."
