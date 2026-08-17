#!/usr/bin/env python3
"""Find documentation OMISSIONS: source surfaces that no document explains.

This is the source-to-docs half of the documentation gates.
`verify-doc-citations.py` runs the other way, resolving every citation in the
corpus to a real symbol, and it catches STALE references. It cannot catch a
MISSING one, because a citation that was never written has nothing to resolve.

That asymmetry shipped a real regression. `modules/base.py` gained five
additions to the module authoring contract - `SECRET_ARGS`, `on_connect`,
`on_disconnect`, `spawn`, `scrub_secrets` - and the page that *is* that contract
documented none of them. Every gate stayed green, because every existing
citation still resolved. The same shape shipped `modules/twitch.py` with no
sanitizer while a "canonical sanitizer" test passed, since that test checked a
hard-coded list of six other modules.

Four checks, all driven by docs/coverage-manifest.json:

  symbols   public API of a tracked source must be mentioned in its mapped docs
  trees     every source file in a tracked directory must have a page
  counts    a hard-coded population count in prose must match reality
  toctree   every maintained document must be reachable from the Sphinx root

Usage:
    scripts/verify-doc-coverage.py            # report, exit 1 on any gap
    scripts/verify-doc-coverage.py --summary  # counts only
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = REPO / "docs" / "coverage-manifest.json"

# Public methods every module inherits or overrides from the base contract, and
# dunders. Mentioning each one in prose would be noise, not coverage.
UNIVERSAL_SKIP = {
    "setup", "help_lines", "is_configured", "on_load", "on_unload", "on_raw",
    "COMMANDS",
}


def public_symbols(path: pathlib.Path, ignore: set[str]) -> set[str]:
    """Public module-level names, plus public attributes and methods of public classes.

    Private names (leading underscore) are excluded: they are implementation,
    and requiring prose for each would make the gate unusable. The surfaces in
    the manifest are chosen so their public names ARE the contract other code
    and other documents depend on.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    out: set[str] = set()

    def public(name: str) -> bool:
        return (not name.startswith("_")
                and name not in ignore
                and name not in UNIVERSAL_SKIP)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if public(node.name):
                out.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if public(node.name):
                out.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if public(sub.name):
                        out.add(sub.name)
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    if public(sub.target.id) and sub.target.id.isupper():
                        out.add(sub.target.id)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name) and public(t.id) and t.id.isupper():
                            out.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if public(node.target.id) and node.target.id.isupper():
                out.add(node.target.id)
    return out


def mentioned(symbol: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(symbol)}\b", text) is not None


def check_symbols(man: dict, failures: list[str]) -> tuple[int, int]:
    checked = missing = 0
    for surf in man["surfaces"]:
        src = REPO / surf["source"]
        if not src.exists():
            failures.append(f"MANIFEST: {surf['source']} does not exist")
            continue
        docs_text = ""
        for d in surf["docs"]:
            p = REPO / d
            if not p.exists():
                failures.append(f"MANIFEST: {d} does not exist")
                continue
            docs_text += p.read_text(encoding="utf-8") + "\n"
        for sym in sorted(public_symbols(src, set(surf.get("ignore", [])))):
            checked += 1
            if not mentioned(sym, docs_text):
                missing += 1
                failures.append(
                    f"UNDOCUMENTED  {surf['source']} - {sym}  "
                    f"(expected in {', '.join(surf['docs'])})")
    return checked, missing


def check_trees(man: dict, failures: list[str]) -> tuple[int, int]:
    checked = missing = 0
    for tree in man["trees"]:
        d = REPO / tree["dir"]
        docs_dir = REPO / tree["docs_dir"]
        skip = set(tree.get("skip", []))
        if tree.get("packages_only"):
            items = [p.name for p in d.iterdir()
                     if p.is_dir() and not p.name.startswith(("_", "."))]
        else:
            items = [p.stem for p in d.glob("*.py")]
        for item in sorted(items):
            if item in skip or item.startswith("_"):
                continue
            checked += 1
            if not (docs_dir / f"{item}.md").exists():
                missing += 1
                failures.append(
                    f"NO PAGE       {tree['dir']}/{item}  "
                    f"(expected {tree['docs_dir']}/{item}.md)")
    return checked, missing


def check_counts(man: dict, failures: list[str]) -> tuple[int, int]:
    checked = wrong = 0
    for c in man.get("counts", []):
        actual = len(list(REPO.glob(c["glob"])))
        for d in c["docs"]:
            p = REPO / d
            if not p.exists():
                continue
            m = re.search(c["pattern"], p.read_text(encoding="utf-8"))
            if not m:
                continue
            checked += 1
            if int(m.group(1)) != actual:
                wrong += 1
                failures.append(
                    f"STALE COUNT   {d}: says {m.group(1)} {c['what']}, "
                    f"actual {actual}")
    return checked, wrong


def check_toctree(man: dict, failures: list[str]) -> tuple[int, int]:
    idx = (REPO / "docs" / "index.md").read_text(encoding="utf-8")
    named: set[str] = set()
    for block in re.findall(r"```\{toctree\}(.*?)```", idx, re.S):
        for ln in block.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith(":"):
                named.add(ln)
    globbed = any(":glob:" in b for b in re.findall(r"```\{toctree\}(.*?)```", idx, re.S))
    skip_dirs = tuple(man.get("unrendered_ok", []))
    checked = orphan = 0
    for p in sorted((REPO / "docs").rglob("*.md")):
        rel = p.relative_to(REPO)
        if any(str(rel).startswith(s) for s in skip_dirs):
            continue
        if p.name == "index.md":
            continue
        stem = str(p.relative_to(REPO / "docs")).removesuffix(".md")
        checked += 1
        # A page is reachable if named directly, named by its own index, or
        # picked up by a glob toctree in its directory.
        if stem in named or f"{p.parent.relative_to(REPO / 'docs')}/index" in named:
            continue
        sibling_index = p.parent / "index.md"
        if sibling_index.exists() and ":glob:" in sibling_index.read_text(encoding="utf-8"):
            continue
        if globbed and stem.split("/")[0] in {n.split("/")[0] for n in named}:
            continue
        orphan += 1
        failures.append(f"ORPHAN        {rel} is in no toctree; it reaches neither HTML nor PDF")
    return checked, orphan


def main() -> int:
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures: list[str] = []
    sym_n, sym_bad = check_symbols(man, failures)
    tree_n, tree_bad = check_trees(man, failures)
    cnt_n, cnt_bad = check_counts(man, failures)
    toc_n, toc_bad = check_toctree(man, failures)

    if "--summary" not in sys.argv:
        for f in failures:
            print(f)
        if failures:
            print()
    print(f"symbols  : {sym_n} checked, {sym_bad} undocumented")
    print(f"files    : {tree_n} checked, {tree_bad} without a page")
    print(f"counts   : {cnt_n} checked, {cnt_bad} stale")
    print(f"toctree  : {toc_n} checked, {toc_bad} orphaned")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
