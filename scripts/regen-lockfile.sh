#!/usr/bin/env bash
#
# Regenerate requirements.lock with hash-pinning.
#
# Run this whenever requirements.txt changes (Dependabot, manual bump,
# or any pip dep edit).  Commit both requirements.txt and
# requirements.lock together — CI installs with --require-hashes
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
#   * Python 3.10+ on PATH (we create an ephemeral venv to avoid
#     touching the operator's system pip)
#   * Network access to PyPI for the resolve step

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$(mktemp -d)/piptools-venv"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

echo ">> Creating ephemeral venv at $VENV"
python3 -m venv "$VENV"
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
