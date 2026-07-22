#!/usr/bin/env python3
"""Remap ``file.py:LINE`` citations in the docs after a source edit moved lines.

The documentation set cites source by line number throughout. Editing a source
file silently invalidates every citation below the edit, and no cheap check
catches it: the cited line still exists, it just says something else now. A
Sphinx build will not notice, and neither will a range check.

This builds an exact old -> new line map with ``difflib`` by comparing the file
at a git ref against the working tree, then rewrites the citations. Lines that
were deleted outright are reported as UNMAPPABLE and left alone for a human -
those usually mean the surrounding prose needs rewriting anyway, not renumbering.

Usage:
    scripts/remap-doc-citations.py <git-ref> <source-file> [<source-file> ...]
    scripts/remap-doc-citations.py <git-ref> <source-file> --apply

Without ``--apply`` it only reports, which is the intended first run.

Example - after editing internets.py and admin_cmds.py, with the last commit
where the docs and code agreed being HEAD:

    scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py
    scripts/remap-doc-citations.py HEAD internets.py admin_cmds.py --apply

ALWAYS spot-check a few remapped citations by CONTENT afterwards. difflib can
mis-anchor when a block moves and is edited in the same commit, and an
off-by-one still resolves to a real line.
"""
from __future__ import annotations

import difflib
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# Every file that carries citations. Add new prose docs here.
DOC_GLOBS = ("docs/*.md", "README.md", "CONTRIBUTING.md", "SECURITY.md")


def doc_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pattern in DOC_GLOBS:
        out.extend(sorted(REPO.glob(pattern)))
    return [p for p in out if p.is_file()]


def build_line_map(ref: str, src_name: str) -> dict[int, int]:
    """old line number -> new line number, for lines that survived intact."""
    proc = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{ref}:{src_name}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"cannot read {src_name} at {ref}: {proc.stderr.strip()}")
    old = proc.stdout.splitlines()
    new = (REPO / src_name).read_text().splitlines()
    print(f"{src_name}: {len(old)} lines at {ref} -> {len(new)} now")

    line_map: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                line_map[i1 + k + 1] = j1 + k + 1
    return line_map


def remap(ref: str, src_name: str, apply_changes: bool) -> tuple[int, int]:
    line_map = build_line_map(ref, src_name)
    pattern = re.compile(rf"{re.escape(src_name)}:(\d+)(?:-(\d+))?")
    changed_total = unmappable_total = 0

    for doc in doc_files():
        text = doc.read_text()
        changed: list[str] = []
        unmappable: list[str] = []

        def substitute(m: re.Match) -> str:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else None
            new_start = line_map.get(start)
            new_end = line_map.get(end) if end else None
            if new_start is None or (end and new_end is None):
                unmappable.append(m.group(0))
                return m.group(0)
            if new_start == start and (not end or new_end == end):
                return m.group(0)
            replacement = f"{src_name}:{new_start}"
            if end:
                replacement += f"-{new_end}"
            changed.append(f"{m.group(0)} -> {replacement}")
            return replacement

        new_text = pattern.sub(substitute, text)
        if changed or unmappable:
            print(f"\n  {doc.relative_to(REPO)}")
            for line in changed:
                print(f"     {line}")
            for line in unmappable:
                print(f"     UNMAPPABLE (line was edited away): {line}")
        if apply_changes and new_text != text:
            doc.write_text(new_text)
        changed_total += len(changed)
        unmappable_total += len(unmappable)

    return changed_total, unmappable_total


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply_changes = "--apply" in sys.argv
    if len(args) < 2:
        sys.exit(__doc__)

    ref, sources = args[0], args[1:]
    changed = unmappable = 0
    for src in sources:
        if not (REPO / src).is_file():
            sys.exit(f"no such file in the repo: {src}")
        c, u = remap(ref, src, apply_changes)
        changed += c
        unmappable += u

    print(f"\ntotal remapped: {changed}   unmappable: {unmappable}")
    print("(applied)" if apply_changes
          else "(dry run - pass --apply to write)")
    if unmappable:
        print("Unmappable citations point at lines that no longer exist. "
              "Fix those by hand; the prose around them is usually stale too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
