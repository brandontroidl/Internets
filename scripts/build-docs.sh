#!/usr/bin/env bash
# Build the Internets documentation: HTML and/or PDF.
#
#   scripts/build-docs.sh            # build both HTML and PDF
#   scripts/build-docs.sh html       # HTML only
#   scripts/build-docs.sh pdf        # PDF only
#
# Requires: sphinx, myst-parser, sphinx-autoapi, sphinx-rtd-theme,
#           sphinx-copybutton, sphinx-design, graphviz, and a TeX Live with
#           xelatex + makeindex (for PDF).  No latexmk needed: the PDF is
#           produced with explicit xelatex passes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCS="$REPO/docs"
BUILD="$DOCS/_build"
TEXNAME="internets.tex"

target="${1:-all}"

build_html() {
    echo ">>> Building HTML -> $BUILD/html"
    # Not -W: the only remaining warnings are docutils formatting nags on a
    # handful of plain-text-formatted module docstrings in the generated API
    # pages (content renders fine).  Warnings still print for visibility.
    sphinx-build -b html --keep-going "$DOCS" "$BUILD/html"
    echo ">>> HTML index: $BUILD/html/index.html"
}

build_pdf() {
    echo ">>> Building LaTeX -> $BUILD/latex"
    sphinx-build -b latex "$DOCS" "$BUILD/latex"
    echo ">>> Compiling PDF with xelatex (3 passes + makeindex)"
    cd "$BUILD/latex"
    xelatex -interaction=nonstopmode "$TEXNAME" >/dev/null 2>&1 || true
    if [ -f "${TEXNAME%.tex}.idx" ]; then
        makeindex "${TEXNAME%.tex}.idx" >/dev/null 2>&1 || true
    fi
    xelatex -interaction=nonstopmode "$TEXNAME" >/dev/null 2>&1 || true
    xelatex -interaction=nonstopmode "$TEXNAME" >/dev/null 2>&1 || true
    if [ -f "${TEXNAME%.tex}.pdf" ]; then
        echo ">>> PDF: $BUILD/latex/${TEXNAME%.tex}.pdf"
    else
        echo "!!! PDF was not produced; inspect $BUILD/latex/${TEXNAME%.tex}.log" >&2
        exit 1
    fi
}

case "$target" in
    html) build_html ;;
    pdf)  build_pdf ;;
    all)  build_html; build_pdf ;;
    *)    echo "usage: $0 [html|pdf|all]" >&2; exit 2 ;;
esac
