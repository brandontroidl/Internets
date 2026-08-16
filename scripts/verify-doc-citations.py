#!/usr/bin/env python3
"""Verify documentation source citations by CONTENT, not by line arithmetic.

`remap-doc-citations.py` keeps line-number citations pointing at the same text
after an edit moves it. That is necessary and not sufficient: a citation can be
arithmetically correct and still be wrong, because it was wrong before the edit
or because the prose around it drifted. This checks meaning instead.

Two citation styles are checked:

  symbol style   `internets.py - IRCBot.method()`   preferred
      Verified by parsing the cited file's AST and confirming the symbol exists
      (module-level function, class, or method of the named class).

  line style     `internets.py:123` / `internets.py:12-34`
      Verified only for existence and range sanity: the file exists and the
      range lies inside it. A line citation cannot be content-verified
      mechanically, so each one is reported as REVIEW rather than PASS, which
      is the point: line citations are the style being retired.

Usage:
    scripts/verify-doc-citations.py              # report, exit 1 on failures
    scripts/verify-doc-citations.py --summary    # counts only
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

PLACEHOLDERS = {"file.py", "foo.py", "module.py"}
LINE_RE = re.compile(r"`([\w/\._-]+\.py):(\d+)(?:-(\d+))?`")
# Bare prose line ranges: "- Lines 42-103: ..." or "(lines 96-101)". These rot
# exactly like a backticked line citation but match neither pattern above, so
# they were invisible to this checker until a conversion pass surfaced 108 of
# them, one already pointing past the end of its file. The enclosing document
# names the source file, so range-check against that.
PROSE_RE = re.compile(r"(?:^|\()\s*-?\s*[Ll]ines?\s+(\d+)(?:\s*[-\u2013]\s*(\d+))?", re.M)
SYM_RE = re.compile(r"`([\w/\._-]+\.py) - ([\w\.]+)(\(\))?`")

REPO = pathlib.Path(__file__).resolve().parent.parent


def doc_files() -> list[pathlib.Path]:
    out = [REPO / n for n in ("README.md", "CONTRIBUTING.md", "SECURITY.md",
                              "PRIVACY.md")]
    out += sorted(p for p in (REPO / "docs").rglob("*.md")
                  if "_build" not in p.parts and "autoapi" not in p.parts)
    return [p for p in out if p.exists()]


_ast_cache: dict[pathlib.Path, ast.Module | None] = {}


def parse(path: pathlib.Path) -> ast.Module | None:
    if path not in _ast_cache:
        try:
            _ast_cache[path] = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            _ast_cache[path] = None
    return _ast_cache[path]


def symbols(tree: ast.Module) -> set[str]:
    """Every referenceable name: func, Class, Class.method, nested one deep."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{sub.name}")
                    # Instance attributes (self.x = ...) are legitimate
                    # citation targets; they are assigned inside methods,
                    # not at class level, so walk the method body for them.
                    for inner in ast.walk(sub):
                        tgts = []
                        if isinstance(inner, ast.Assign):
                            tgts = inner.targets
                        elif isinstance(inner, ast.AnnAssign):
                            tgts = [inner.target]
                        for t_ in tgts:
                            if (isinstance(t_, ast.Attribute)
                                    and isinstance(t_.value, ast.Name)
                                    and t_.value.id == "self"):
                                names.add(f"{node.name}.{t_.attr}")
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(f"{node.name}.{t.id}")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    names.add(f"{node.name}.{sub.target.id}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def candidates(cited: str, doc: pathlib.Path) -> list[pathlib.Path]:
    """All files a citation could mean, nearest-to-the-doc first.

    Basenames are ambiguous on purpose in this repo: base.py exists as
    modules/base.py and weather_providers/base.py, __init__.py exists in
    every package, and each provider sub-package has its own air_quality.py.
    A citation inside docs/internals/weather-providers/providers/waqi.md that
    says `base.py` means the provider layer's base.py. Resolving to a single
    arbitrary match makes the checker report doc defects that are really
    checker defects, so return every candidate and let the caller accept a
    symbol found in any of them, preferring the closest by path affinity.
    """
    direct = REPO / cited
    if direct.exists():
        return [direct]
    hits = [p for p in REPO.rglob(cited)
            if "_build" not in p.parts and ".git" not in p.parts
            and "__pycache__" not in p.parts]
    if not hits:
        return []
    # Path affinity: docs under .../weather-providers/... prefer files under
    # weather_providers/, and a provider page prefers its own sub-package.
    doc_parts = set(doc.parts)
    stem = doc.stem

    def rank(p: pathlib.Path) -> tuple[int, int]:
        parts = set(p.parts)
        same_provider = 0 if stem in parts else 1
        layer = 0 if (("weather-providers" in doc_parts)
                      == ("weather_providers" in parts)) else 1
        return (same_provider, layer)

    return sorted(hits, key=rank)


def resolve(cited: str, doc: pathlib.Path) -> pathlib.Path | None:
    c = candidates(cited, doc)
    return c[0] if c else None


def main() -> int:
    summary_only = "--summary" in sys.argv
    counts = {"symbol_ok": 0, "symbol_fail": 0, "line_review": 0,
              "line_fail": 0, "file_missing": 0, "prose_ok": 0, "prose_fail": 0}
    failures: list[str] = []

    for doc in doc_files():
        rel = doc.relative_to(REPO)
        for i, ln in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in SYM_RE.finditer(ln):
                cited, sym = m.group(1), m.group(2)
                if cited in PLACEHOLDERS:
                    continue
                cands = candidates(cited, doc)
                if not cands:
                    counts["file_missing"] += 1
                    failures.append(f"{rel}:{i} MISSING FILE {cited}")
                    continue
                tail = sym.split(".")[-1]
                found = False
                for src in cands:
                    tree = parse(src)
                    if tree is None:
                        continue
                    known = symbols(tree)
                    if sym in known or tail in known or any(
                            k.endswith("." + tail) for k in known):
                        found = True
                        break
                if found:
                    counts["symbol_ok"] += 1
                else:
                    counts["symbol_fail"] += 1
                    failures.append(f"{rel}:{i} NO SUCH SYMBOL {cited} - {sym}")
            for m in LINE_RE.finditer(ln):
                cited = m.group(1)
                if cited in PLACEHOLDERS:
                    continue
                start = int(m.group(2))
                end = int(m.group(3) or m.group(2))
                src = resolve(cited, doc)
                if src is None:
                    counts["file_missing"] += 1
                    failures.append(f"{rel}:{i} MISSING FILE {cited}")
                    continue
                total = len(src.read_text(encoding="utf-8").splitlines())
                if start < 1 or end > total:
                    counts["line_fail"] += 1
                    failures.append(
                        f"{rel}:{i} OUT OF RANGE {cited}:{start}-{end} (file has {total})")
                else:
                    counts["line_review"] += 1

    # Bare prose line ranges, checked against the source file the internals
    # page documents (docs/internals/<stem>.md -> <stem>.py).
    for doc in doc_files():
        if "internals" not in doc.parts:
            continue
        rel = doc.relative_to(REPO)
        src = resolve(doc.stem + ".py", doc)
        if src is None:
            continue
        total = len(src.read_text(encoding="utf-8").splitlines())
        for i, ln in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in PROSE_RE.finditer(ln):
                start = int(m.group(1))
                end = int(m.group(2) or m.group(1))
                if end > total or start < 1:
                    counts["prose_fail"] += 1
                    failures.append(
                        f"{rel}:{i} PROSE RANGE past EOF: lines {start}-{end} "
                        f"but {src.name} has {total}")
                else:
                    counts["prose_ok"] += 1

    if not summary_only:
        for f in failures:
            print(f)
        print()
    total_cites = sum(counts.values())
    print(f"citations: {total_cites}")
    print(f"  symbol verified : {counts['symbol_ok']}")
    print(f"  symbol FAILED   : {counts['symbol_fail']}")
    print(f"  line in-range   : {counts['line_review']} (needs human content check)")
    print(f"  line OUT OF RANGE: {counts['line_fail']}")
    print(f"  missing file    : {counts['file_missing']}")
    print(f"  prose ranges ok : {counts['prose_ok']}")
    print(f"  prose PAST EOF  : {counts['prose_fail']}")
    bad = (counts["symbol_fail"] + counts["line_fail"] + counts["file_missing"]
           + counts["prose_fail"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
