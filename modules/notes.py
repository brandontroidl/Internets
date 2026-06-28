"""Personal sticky-notes — per-nick note storage.

`.notes` subcommands:
    list            list your notes, numbered
    add <text>      add a new note
    del <N>         delete note #N (1-based)
    show <N>        show note #N in full
    clear           delete all your notes (two-step confirm within 60s)

Notes are stored in a JSON file alongside the bot, keyed by lowercased
nick.  No expiration.  Limits: 20 notes per nick, 200 chars per note.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import BotModule, help_row, strip_ctrl

log = logging.getLogger("internets.notes")

_MAX_NOTES = 20
_MAX_LEN = 200
_CLEAR_WINDOW = 60.0  # seconds


def _strip_ctrl(s: str, max_len: int = _MAX_LEN) -> str:
    return strip_ctrl(s, max_len)


def _timeago(ts: int) -> str:
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    s = int(delta.total_seconds())
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


class NotesModule(BotModule):
    """Per-nick sticky-notes store."""

    COMMANDS: dict[str, str] = {"notes": "cmd_notes"}

    def on_load(self) -> None:
        sect = self.bot.cfg["notes"] if "notes" in self.bot.cfg else {}
        self._notes_file = Path(sect.get("file", "notes.json"))
        self._lock = threading.Lock()
        self._clear_pending: dict[str, float] = {}
        try:
            if self._notes_file.exists():
                raw = json.loads(self._notes_file.read_text())
                if isinstance(raw, dict):
                    self._notes: dict[str, list[dict]] = {
                        str(k).lower(): [
                            {"text": str(n.get("text", "")), "ts": int(n.get("ts", 0))}
                            for n in v if isinstance(n, dict) and n.get("text")
                        ]
                        for k, v in raw.items()
                        if isinstance(v, list)
                    }
                else:
                    self._notes = {}
            else:
                self._notes = {}
        except Exception as e:
            log.warning(f"notes: failed to load {self._notes_file}: {e}")
            self._notes = {}

    def is_configured(self) -> bool:
        return True

    def forget(self, nick: str) -> int:
        """Erase all notes owned by ``nick`` (privacy right-to-erasure)."""
        with self._lock:
            removed = self._notes.pop(nick.lower(), None)
        if removed is None:
            return 0
        self._save_notes()   # re-acquires self._lock — must not hold it here
        return len(removed)

    def _save_notes(self) -> None:
        """Atomic write: tempfile + os.replace, mode 0o600."""
        with self._lock:
            try:
                parent = self._notes_file.parent if self._notes_file.parent.as_posix() else Path(".")
                parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    prefix=".notes.", suffix=".json.tmp", dir=str(parent)
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(self._notes, f, indent=2)
                    os.chmod(tmp_path, 0o600)
                    os.replace(tmp_path, self._notes_file)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                log.warning(f"notes: failed to save: {e}")

    async def cmd_notes(self, nick: str, reply_to: str, arg: str | None) -> None:
        """`.notes [list|add|del|show|clear] [args]`"""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        prefix = self.bot.cfg["bot"]["command_prefix"]
        text = (arg or "").strip()
        if not text:
            self.bot.privmsg(
                reply_to,
                f"{nick}: usage: {prefix}notes <list|add|del|show|clear> [args]",
            )
            return

        parts = text.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        key = nick.lower()
        if sub == "list":
            self._do_list(nick, reply_to, key)
        elif sub == "add":
            self._do_add(nick, reply_to, key, rest)
            await asyncio.to_thread(self._save_notes)
        elif sub == "del" or sub == "delete" or sub == "rm":
            changed = self._do_del(nick, reply_to, key, rest)
            if changed:
                await asyncio.to_thread(self._save_notes)
        elif sub == "show":
            self._do_show(nick, reply_to, key, rest)
        elif sub == "clear":
            changed = self._do_clear(nick, reply_to, key)
            if changed:
                await asyncio.to_thread(self._save_notes)
        else:
            self.bot.privmsg(
                reply_to,
                f"{nick}: unknown subcommand '{sub}' — "
                f"try {prefix}notes <list|add|del|show|clear>",
            )

    # --- subcommand handlers (sync; mutate self._notes) -----------------

    def _do_list(self, nick: str, reply_to: str, key: str) -> None:
        notes = self._notes.get(key, [])
        if not notes:
            self.bot.privmsg(reply_to, f"{nick}: you have no notes")
            return
        self.bot.privmsg(reply_to, f"{nick}: your notes ({len(notes)}):")
        for i, n in enumerate(notes, start=1):
            self.bot.privmsg(
                reply_to,
                f"  #{i} ({_timeago(int(n.get('ts', 0)))}) {n.get('text', '')}",
            )

    def _do_add(self, nick: str, reply_to: str, key: str, rest: str) -> None:
        if not rest:
            prefix = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {prefix}notes add <text>")
            return
        notes = self._notes.setdefault(key, [])
        if len(notes) >= _MAX_NOTES:
            self.bot.privmsg(
                reply_to,
                f"{nick}: you have {_MAX_NOTES} notes already — delete some first",
            )
            return
        clean = _strip_ctrl(rest, _MAX_LEN)
        if not clean:
            self.bot.privmsg(reply_to, f"{nick}: note text is empty")
            return
        truncated = len(rest) > _MAX_LEN
        notes.append({"text": clean, "ts": int(time.time())})
        n_num = len(notes)
        suffix = f" (truncated to {_MAX_LEN} chars)" if truncated else ""
        self.bot.privmsg(reply_to, f"{nick}: added note #{n_num}{suffix}")

    def _do_del(self, nick: str, reply_to: str, key: str, rest: str) -> bool:
        if not rest:
            prefix = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {prefix}notes del <N>")
            return False
        try:
            n = int(rest.split()[0])
        except ValueError:
            self.bot.privmsg(reply_to, f"{nick}: '{rest}' is not a number")
            return False
        notes = self._notes.get(key, [])
        if not notes:
            self.bot.privmsg(reply_to, f"{nick}: you have no notes")
            return False
        if n < 1 or n > len(notes):
            self.bot.privmsg(
                reply_to,
                f"{nick}: no note #{n} (you have {len(notes)})",
            )
            return False
        removed = notes.pop(n - 1)
        if not notes:
            # keep the dict tidy
            self._notes.pop(key, None)
        self.bot.privmsg(
            reply_to,
            f'{nick}: deleted note #{n}: "{removed.get("text", "")}"',
        )
        return True

    def _do_show(self, nick: str, reply_to: str, key: str, rest: str) -> None:
        if not rest:
            prefix = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {prefix}notes show <N>")
            return
        try:
            n = int(rest.split()[0])
        except ValueError:
            self.bot.privmsg(reply_to, f"{nick}: '{rest}' is not a number")
            return
        notes = self._notes.get(key, [])
        if not notes:
            self.bot.privmsg(reply_to, f"{nick}: you have no notes")
            return
        if n < 1 or n > len(notes):
            self.bot.privmsg(
                reply_to,
                f"{nick}: no note #{n} (you have {len(notes)})",
            )
            return
        note = notes[n - 1]
        self.bot.privmsg(
            reply_to,
            f"{nick}: #{n} ({_timeago(int(note.get('ts', 0)))}) {note.get('text', '')}",
        )

    def _do_clear(self, nick: str, reply_to: str, key: str) -> bool:
        notes = self._notes.get(key, [])
        if not notes:
            self.bot.privmsg(reply_to, f"{nick}: you have no notes")
            self._clear_pending.pop(key, None)
            return False
        count = len(notes)
        now = time.time()
        pending = self._clear_pending.get(key)
        prefix = self.bot.cfg["bot"]["command_prefix"]
        if pending and (now - pending) <= _CLEAR_WINDOW:
            self._notes.pop(key, None)
            self._clear_pending.pop(key, None)
            self.bot.privmsg(reply_to, f"{nick}: cleared {count} notes")
            return True
        self._clear_pending[key] = now
        self.bot.privmsg(
            reply_to,
            f"{nick}: confirm: run `{prefix}notes clear` again within "
            f"{int(_CLEAR_WINDOW)}s to delete all {count} notes",
        )
        return False

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "notes <sub> [args]", "Personal sticky notes"),
            f"      subcommands: list | add <text> | del <N> | show <N> | clear",
        ]


def setup(bot: object) -> NotesModule:
    """Module entry point — returns a NotesModule instance."""
    return NotesModule(bot)  # type: ignore[arg-type]
