"""Persistent and in-memory state for modules/claude.py.

Not a loadable module: the leading underscore keeps it out of ``.load``. It
holds no network code and no IRC code, so every rule it enforces (limits,
opt-out, erasure, expiry) is testable without a bot.

Two stores with different lifetimes:

* ``ClaudeState`` is written to disk (``[claude] file``, default
  ``claude.json``) with the module-store pattern from docs/writing-modules.md:
  snapshot under the lock, ``mkstemp`` + ``chmod 0600`` + ``os.replace``
  outside it. It holds saved facts, clock preferences, personas, per-channel
  switches, and the channel backlog.
* ``Conversations`` is memory only. It holds the recent turns of each
  (channel, nick) conversation and is lost on restart by design.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

log = logging.getLogger("internets.claude")

MAX_FACTS = 50
MAX_FACT_LEN = 300
MIN_FACT_LEN = 5
MAX_PERSONA_LEN = 200
BACKLOG_LINES = 100
BACKLOG_MAX_AGE = 48 * 3600
BACKLOG_LINE_LEN = 400

HISTORY_TURNS = 12
HISTORY_IDLE = 30 * 60
HISTORY_KEYS = 500


def _k(s: str) -> str:
    return s.lower()


class ClaudeState:
    """Everything the Claude module keeps across restarts.

    Keys are lower-cased nicks and channels. All mutators take the lock and
    mark the store dirty; ``save()`` writes a snapshot.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._dirty = False
        self.facts: dict[str, list[str]] = {}
        self.prefs: dict[str, dict[str, Any]] = {}
        # channel -> {"channel": str|None, "users": {nick: str}}
        self.personas: dict[str, dict[str, Any]] = {}
        # channel -> {"ai": bool, "talkback": bool}
        self.switches: dict[str, dict[str, bool]] = {}
        # channel -> [[ts, nick, text], ...], oldest first
        self.backlog: dict[str, list[list[Any]]] = {}

    # ── persistence ────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            if not self._path.exists():
                return
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning("claude: state load failed: %s", type(e).__name__)
            return
        if not isinstance(data, dict):
            return
        for name in ("facts", "prefs", "personas", "switches", "backlog"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(self, name, value)
        self.prune()

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = json.loads(json.dumps({
                "facts": self.facts, "prefs": self.prefs,
                "personas": self.personas, "switches": self.switches,
                "backlog": self.backlog,
            }))
            self._dirty = False
        parent = self._path.parent
        fd, tmp = tempfile.mkstemp(dir=str(parent) if str(parent) else ".",
                                   prefix=self._path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log.warning("claude: state save failed: %s", type(e).__name__)
            with self._lock:
                self._dirty = True

    def prune(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - BACKLOG_MAX_AGE
        with self._lock:
            for chan in list(self.backlog):
                kept = [row for row in self.backlog[chan]
                        if isinstance(row, list) and len(row) == 3
                        and isinstance(row[0], (int, float)) and row[0] >= cutoff]
                kept = kept[-BACKLOG_LINES:]
                if len(kept) != len(self.backlog[chan]):
                    self._dirty = True
                if kept:
                    self.backlog[chan] = kept
                else:
                    del self.backlog[chan]

    # ── facts ────────────────────────────────────────────────────────

    def add_fact(self, nick: str, fact: str) -> str:
        """Return "added", "duplicate", "full", or "length"."""
        fact = " ".join(fact.split())
        if not MIN_FACT_LEN <= len(fact) <= MAX_FACT_LEN:
            return "length"
        with self._lock:
            facts = self.facts.setdefault(_k(nick), [])
            if any(f.lower() == fact.lower() for f in facts):
                return "duplicate"
            if len(facts) >= MAX_FACTS:
                return "full"
            facts.append(fact)
            self._dirty = True
        return "added"

    def forget_fact(self, nick: str, query: str) -> str | None:
        """Remove the best fuzzy match for *query*; return it, or None."""
        words = {w for w in query.lower().split() if len(w) > 2} or {query.lower()}
        with self._lock:
            facts = self.facts.get(_k(nick), [])
            best, score = None, 0
            for f in facts:
                fl = f.lower()
                s = sum(1 for w in words if w in fl)
                if query.lower() in fl:
                    s += len(words) + 1
                if s > score:
                    best, score = f, s
            if best is None:
                return None
            facts.remove(best)
            if not facts:
                self.facts.pop(_k(nick), None)
            self._dirty = True
        return best

    def clear_facts(self, nick: str) -> int:
        with self._lock:
            removed = self.facts.pop(_k(nick), [])
            if removed:
                self._dirty = True
        return len(removed)

    def get_facts(self, nick: str) -> list[str]:
        with self._lock:
            return list(self.facts.get(_k(nick), []))

    # ── preferences ───────────────────────────────────────────────────

    def set_pref(self, nick: str, key: str, value: Any) -> None:
        with self._lock:
            self.prefs.setdefault(_k(nick), {})[key] = value
            self._dirty = True

    def get_pref(self, nick: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.prefs.get(_k(nick), {}).get(key, default)

    # ── personas ─────────────────────────────────────────────────────

    def set_persona(self, channel: str, persona: str,
                    nick: str | None = None) -> str:
        persona = " ".join(persona.split())[:MAX_PERSONA_LEN]
        with self._lock:
            entry = self.personas.setdefault(_k(channel),
                                             {"channel": None, "users": {}})
            if nick is None:
                entry["channel"] = persona
            else:
                entry["users"][_k(nick)] = persona
            self._dirty = True
        return persona

    def reset_persona(self, channel: str, nick: str | None = None) -> None:
        """Clear one nick's persona, or with nick=None the whole channel's."""
        with self._lock:
            entry = self.personas.get(_k(channel))
            if entry is None:
                return
            if nick is None:
                self.personas.pop(_k(channel), None)
            else:
                entry["users"].pop(_k(nick), None)
                if not entry["users"] and not entry["channel"]:
                    self.personas.pop(_k(channel), None)
            self._dirty = True

    def persona_for(self, channel: str, nick: str) -> tuple[str | None, str | None]:
        """Return (channel_persona, this_nick's_persona)."""
        with self._lock:
            entry = self.personas.get(_k(channel)) or {}
            return entry.get("channel"), (entry.get("users") or {}).get(_k(nick))

    # ── per-channel switches ─────────────────────────────────────────────

    def set_switch(self, channel: str, name: str, on: bool) -> None:
        with self._lock:
            self.switches.setdefault(_k(channel), {})[name] = on
            self._dirty = True

    def switch(self, channel: str, name: str, default: bool) -> bool:
        with self._lock:
            return bool(self.switches.get(_k(channel), {}).get(name, default))

    # ── channel backlog ──────────────────────────────────────────────

    def log_line(self, channel: str, nick: str, text: str,
                 now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            rows = self.backlog.setdefault(_k(channel), [])
            rows.append([ts, nick, text[:BACKLOG_LINE_LEN]])
            if len(rows) > BACKLOG_LINES:
                del rows[:len(rows) - BACKLOG_LINES]
            self._dirty = True

    def recent(self, channel: str, limit: int = BACKLOG_LINES,
               nick: str | None = None) -> list[tuple[float, str, str]]:
        with self._lock:
            rows = list(self.backlog.get(_k(channel), []))
        if nick is not None:
            rows = [r for r in rows if _k(r[1]) == _k(nick)]
        return [(r[0], r[1], r[2]) for r in rows[-limit:]]

    def clear_backlog(self, channel: str) -> None:
        with self._lock:
            if self.backlog.pop(_k(channel), None) is not None:
                self._dirty = True

    # ── erasure ──────────────────────────────────────────────────────

    def forget(self, nick: str) -> int:
        """Erase every record about *nick*. Returns the number removed."""
        key = _k(nick)
        removed = 0
        with self._lock:
            removed += len(self.facts.pop(key, []))
            if self.prefs.pop(key, None) is not None:
                removed += 1
            for entry in self.personas.values():
                if entry.get("users", {}).pop(key, None) is not None:
                    removed += 1
            for chan, rows in list(self.backlog.items()):
                kept = [r for r in rows if _k(r[1]) != key]
                removed += len(rows) - len(kept)
                if kept:
                    self.backlog[chan] = kept
                else:
                    del self.backlog[chan]
            if removed:
                self._dirty = True
        return removed


class Conversations:
    """Recent turns per (channel, nick), memory only, bounded and expiring.

    Turns are stored as plain text. Assistant thinking blocks are never kept:
    the system prompt changes between requests (facts, persona, clock), and a
    thinking block replayed after its prompt changed is rejected by the API's
    preserved-thinking check, while a text-only history is never checked.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[tuple[str, str], tuple[float, list[dict[str, str]]]] = OrderedDict()

    def get(self, channel: str, nick: str, now: float | None = None) -> list[dict[str, str]]:
        now = now if now is not None else time.monotonic()
        key = (_k(channel), _k(nick))
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return []
            if now - item[0] > HISTORY_IDLE:
                del self._data[key]
                return []
            return [dict(t) for t in item[1]]

    def append(self, channel: str, nick: str, user_text: str, reply: str,
               now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        key = (_k(channel), _k(nick))
        with self._lock:
            _, turns = self._data.pop(key, (now, []))
            turns = turns + [{"role": "user", "content": user_text},
                             {"role": "assistant", "content": reply}]
            # Drop whole user/assistant pairs from the front so the history
            # always starts with a user turn.
            turns = turns[-HISTORY_TURNS:]
            self._data[key] = (now, turns)
            while len(self._data) > HISTORY_KEYS:
                self._data.popitem(last=False)

    def reset(self, channel: str, nick: str | None = None) -> int:
        with self._lock:
            keys = [k for k in self._data
                    if k[0] == _k(channel) and (nick is None or k[1] == _k(nick))]
            for k in keys:
                del self._data[k]
        return len(keys)

    def forget(self, nick: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k[1] == _k(nick)]
            for k in keys:
                del self._data[k]
        return len(keys)
