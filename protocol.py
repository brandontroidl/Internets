"""
Pure IRC protocol helpers — no bot state, no I/O.

Extracted from internets.py to keep the main bot class focused on
orchestration and state management.
"""

from __future__ import annotations

import base64
import re
from typing import Optional


def strip_tags(line: str) -> str:
    """Remove IRCv3 message-tags prefix from *line*."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    return line


def parse_isupport_chanmodes(token: str) -> dict[str, str]:
    """Parse a ``CHANMODES=A,B,C,D`` token from 005 into {mode: type}.

    Types:
        A — list mode, always takes a parameter (e.g. b, e, I)
        B — always takes a parameter (e.g. k, L)
        C — parameter only when setting (e.g. l, H)
        D — never takes a parameter (e.g. i, m, n)
    """
    groups = token.split(",")
    types: dict[str, str] = {}
    for idx, label in enumerate(("A", "B", "C", "D")):
        if idx < len(groups):
            for ch in groups[idx]:
                types[ch] = label
    return types


def parse_isupport_prefix(token: str) -> tuple[set[str], dict[str, str]]:
    """Parse a ``PREFIX=(modes)symbols`` token from 005.

    Returns (mode_set, {symbol: mode}).
    Example: ``(qaohv)~&@%+`` → ({q,a,o,h,v}, {'~':'q', '&':'a', ...})
    """
    m = re.match(r"\(([^)]*)\)(.*)", token)
    if not m:
        return set(), {}
    modes, symbols = m.group(1), m.group(2)
    mode_set = set(modes)
    sym_map = {symbols[i]: modes[i] for i in range(min(len(modes), len(symbols)))}
    return mode_set, sym_map


def parse_mode_changes(
    mode_str: str,
    args: list[str],
    prefix_modes: set[str],
    chanmode_types: dict[str, str],
) -> list[tuple[bool, str, Optional[str]]]:
    """Parse a channel MODE string into a list of (adding, mode_char, param).

    *param* is ``None`` for modes that don't take one.
    Correctly consumes parameters based on ISUPPORT types.
    """
    changes: list[tuple[bool, str, Optional[str]]] = []
    adding = True
    arg_idx = 0

    for ch in mode_str:
        if ch == "+":
            adding = True
        elif ch == "-":
            adding = False
        elif ch in prefix_modes:
            param = args[arg_idx] if arg_idx < len(args) else None
            arg_idx += 1
            changes.append((adding, ch, param))
        else:
            mtype = chanmode_types.get(ch)
            if mtype in ("A", "B"):
                param = args[arg_idx] if arg_idx < len(args) else None
                arg_idx += 1
                changes.append((adding, ch, param))
            elif mtype == "C" and adding:
                param = args[arg_idx] if arg_idx < len(args) else None
                arg_idx += 1
                changes.append((adding, ch, param))
            else:
                changes.append((adding, ch, None))

    return changes


def parse_names_entry(entry: str) -> tuple[str, bool]:
    """Parse a single NAMES entry like ``~@nick`` into (nick, is_op).

    Prefixes ~(owner), &(admin), @(op) count as chanop.
    """
    nick = entry.lstrip("~&@%+")
    if not nick:
        return entry, False
    prefix = entry[: len(entry) - len(nick)]
    is_op = bool(set(prefix) & {"~", "&", "@"})
    return nick, is_op


def sasl_plain_payload(nick: str, password: str) -> str:
    """Build base64-encoded SASL PLAIN payload: ``\\0nick\\0password``."""
    raw = f"\0{nick}\0{password}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")
