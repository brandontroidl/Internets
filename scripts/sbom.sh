#!/usr/bin/env bash
#
# sbom.sh - generate a CycloneDX Software Bill of Materials for the
# currently-installed Python environment.
#
# Output: ./sbom.cdx.json (CycloneDX 1.x JSON).
#
# This script intentionally uses *only* the installed environment so the
# SBOM reflects what will actually ship - not what pyproject.toml claims.
# Run it inside the same venv you used for `python -m build`.
#
# Usage:
#   ./scripts/sbom.sh             # writes ./sbom.cdx.json
#   OUT=foo.json ./scripts/sbom.sh
#
set -euo pipefail

OUT="${OUT:-sbom.cdx.json}"

if ! command -v pip-audit >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: pip-audit is not installed.

Install it into the current Python environment, e.g.:

    pip install "pip-audit>=2.7,<3"

or as part of the project's dev extras:

    pip install -e ".[dev]"
EOF
    exit 127
fi

echo "[sbom] generating CycloneDX SBOM -> ${OUT}" >&2
pip-audit \
    --format cyclonedx-json \
    --output "${OUT}" \
    --progress-spinner off

echo "[sbom] wrote ${OUT} ($(wc -c <"${OUT}") bytes)" >&2
