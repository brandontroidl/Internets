"""`.seen <nick>` — track when a nick was last seen and what they were doing.

Hooks `on_raw` to passively observe PRIVMSG/JOIN/PART/QUIT/NICK lines and
records the most recent event per nick in an in-memory dict.  The dict is
flushed to disk every 60 seconds (atomic write, 0o600) when dirty.

PII note: the seen.json file contains nicknames, channels, and snippets of
message bodies.  Stored locally only; file mode 0o600.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .base import BotModule

log = logging.getLogger("internets.seen")

_RE_LINE = re.compile(r"^:([^!\s]+)![^\s]+\s+(\S+)\s+(.*)$")

_FLUSH_INTERVAL = 60  # seconds
_DETAIL_MAX = 60


def _timeago(ts: int) -> str:
    s = int(time.time()) - int(ts)
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _parse_trailing(rest: str) -> tuple[list[str], str | None]:
    """Split IRC params into (middle_params, trailing_or_None)."""
    if " :" in rest:
        middle, trailing = rest.split(" :", 1)
        return (middle.split() if middle else [], trailing)
    if rest.startswith(":"):
        return ([], rest[1:])
    return (rest.split() if rest else [], None)


class SeenModule(BotModule):
    """`.seen <nick>` — last seen tracker."""

    COMMANDS: dict[str, str] = {"seen": "cmd_seen"}

    def on_load(self) -> None:
        sect = self.bot.cfg["seen"] if "seen" in self.bot.cfg else {}
        path_str = sect.get("file", "seen.json") if hasattr(sect, "get") else "seen.json"
        self._file = Path(path_str)

        # Retention: drop entries older than this many days (0 disables).
        # "seen" is passively-collected last-seen tracking, so it is pruned
        # like store.py's user-tracking rather than kept forever.
        self._max_age_days = 180
        if hasattr(sect, "get"):
            try:
                self._max_age_days = int(sect.get("max_age_days", 180))
            except (ValueError, TypeError):
                self._max_age_days = 180

        self._lock = threading.Lock()
        self._seen: dict[str, dict[str, Any]] = {}
        self._dirty = False

        # Load existing data — any error → empty dict, don't crash
        try:
            if self._file.exists():
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Light validation: keep only dict-valued entries
                    self._seen = {
                        k: v for k, v in data.items()
                        if isinstance(v, dict) and "ts" in v
                    }
        except Exception as e:
            log.warning(f"seen: failed to load {self._file}: {e!r}")
            self._seen = {}

        # Prune stale entries on startup (single-threaded here).
        self._prune_stale()

        # Schedule the periodic flush on the bot's running event loop
        self._flush_task: asyncio.Task[None] | None = None
        try:
            loop = getattr(self.bot, "_loop", None)
            if loop is not None:
                self._flush_task = loop.create_task(self._periodic_flush())
        except Exception as e:
            log.warning(f"seen: failed to schedule flush task: {e!r}")

    def on_unload(self) -> None:
        # Cancel the periodic flush task
        t = getattr(self, "_flush_task", None)
        if t is not None and not t.done():
            try:
                t.cancel()
            except Exception:
                pass  # nosec B110: best-effort cleanup
        # Final synchronous flush
        try:
            self._flush_sync()
        except Exception as e:
            log.warning(f"seen: final flush failed: {e!r}")

    def forget(self, nick: str) -> int:
        """Erase the .seen record for ``nick`` (privacy right-to-erasure)."""
        with self._lock:
            removed = self._seen.pop(nick.lower(), None)
        if removed is None:
            return 0
        self._flush_sync()
        return 1

    # ----------------------------------------------------------------- helpers
    def _own_nick(self) -> str:
        n = getattr(self.bot, "_nick", None)
        if isinstance(n, str) and n:
            return n
        try:
            return str(self.bot.cfg["irc"]["nickname"])
        except Exception:
            return ""

    def _record(
        self,
        nick: str,
        event: str,
        channel: str | None,
        detail: str | None,
    ) -> None:
        if not nick:
            return
        own = self._own_nick().lower()
        if own and nick.lower() == own:
            return
        entry = {
            "nick": nick,
            "ts": int(time.time()),
            "event": event,
            "channel": channel,
            "detail": detail,
        }
        with self._lock:
            self._seen[nick.lower()] = entry
            self._dirty = True

    # -------------------------------------------------------------- on_raw hook
    def on_raw(self, line: str) -> None:
        try:
            if not line or not line.startswith(":"):
                return
            m = _RE_LINE.match(line)
            if not m:
                return
            prefix_nick, command, rest = m.group(1), m.group(2).upper(), m.group(3)

            if command == "PRIVMSG":
                params, trailing = _parse_trailing(rest)
                if not params:
                    return
                target = params[0]
                # Only record PRIVMSGs sent to a channel; private msgs to the
                # bot don't have a meaningful "channel" and feel surveillance-y.
                if not target.startswith(("#", "&", "+", "!")):
                    return
                msg = (trailing or "")[:_DETAIL_MAX]
                self._record(prefix_nick, "PRIVMSG", target, msg)

            elif command == "JOIN":
                params, trailing = _parse_trailing(rest)
                chan = params[0] if params else (trailing or "")
                if chan:
                    self._record(prefix_nick, "JOIN", chan, None)

            elif command == "PART":
                params, trailing = _parse_trailing(rest)
                chan = params[0] if params else ""
                if not chan:
                    return
                reason = trailing.strip() if trailing else ""
                detail = f"left: {reason[:_DETAIL_MAX]}" if reason else "left"
                self._record(prefix_nick, "PART", chan, detail)

            elif command == "QUIT":
                _params, trailing = _parse_trailing(rest)
                reason = trailing.strip() if trailing else ""
                detail = f"quit: {reason[:_DETAIL_MAX]}" if reason else "quit"
                self._record(prefix_nick, "QUIT", None, detail)

            elif command == "NICK":
                _params, trailing = _parse_trailing(rest)
                # `NICK :newnick` or `NICK newnick`
                newnick = (trailing or "").strip()
                if not newnick:
                    # fall back to first middle param
                    parts = rest.split()
                    newnick = parts[0] if parts else ""
                if not newnick:
                    return
                # Record OLD nick as last-seen NICK → newnick
                self._record(prefix_nick, "NICK", None, f"→ {newnick}")
                # Record NEW nick as last-seen NICK ← oldnick
                self._record(newnick, "NICK", None, f"← {prefix_nick}")
        except Exception as e:
            # on_raw must never throw — it runs in the IRC read path
            log.debug(f"seen: on_raw parse error: {e!r}")

    # ------------------------------------------------------------- persistence
    def _prune_stale(self) -> int:
        """Drop entries older than ``max_age_days``.  Returns count removed.

        Caller must hold ``self._lock`` OR run single-threaded (on_load).
        """
        if self._max_age_days <= 0:
            return 0
        cutoff = int(time.time()) - self._max_age_days * 86400
        stale = [k for k, v in self._seen.items()
                 if int(v.get("ts", 0)) < cutoff]
        for k in stale:
            del self._seen[k]
        if stale:
            self._dirty = True
            log.info(f"seen: pruned {len(stale)} entries older than "
                     f"{self._max_age_days}d")
        return len(stale)

    def _flush_sync(self) -> None:
        """Atomic write of self._seen to disk.  Safe to call from any thread."""
        with self._lock:
            self._prune_stale()
            if not self._dirty:
                return
            snapshot = dict(self._seen)
            self._dirty = False

        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self._file)
            try:
                os.chmod(self._file, 0o600)
            except OSError:
                pass
        except Exception as e:
            log.warning(f"seen: flush failed: {e!r}")
            # Re-mark dirty so we retry next interval
            with self._lock:
                self._dirty = True
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception as e:
                # Cleanup-of-cleanup — the outer flush already failed and
                # we just want to leave no orphan .tmp around.  If even
                # the unlink fails, log and move on; the next flush will
                # overwrite it.
                log.debug("seen: temp cleanup failed: %s", type(e).__name__)

    async def _periodic_flush(self) -> None:
        try:
            while True:
                await asyncio.sleep(_FLUSH_INTERVAL)
                try:
                    await asyncio.to_thread(self._flush_sync)
                except Exception as e:
                    log.warning(f"seen: periodic flush error: {e!r}")
        except asyncio.CancelledError:
            return

    # ----------------------------------------------------------------- command
    def _format_entry(self, entry: dict[str, Any]) -> str:
        nick = entry.get("nick", "?")
        ts = int(entry.get("ts", 0))
        ago = _timeago(ts)
        event = entry.get("event", "?")
        channel = entry.get("channel")
        detail = entry.get("detail") or ""

        bnick = f"\x02{nick}\x02"

        if event == "PRIVMSG":
            return f'{bnick} last seen {ago} ago — PRIVMSG in {channel}: "{detail}"'
        if event == "JOIN":
            return f"{bnick} last seen {ago} ago — joined {channel}"
        if event == "PART":
            # detail starts with "left" or "left: <reason>"
            if channel:
                return f"{bnick} last seen {ago} ago — {detail} {channel}".rstrip()
            return f"{bnick} last seen {ago} ago — {detail}"
        if event == "QUIT":
            return f"{bnick} last seen {ago} ago — {detail}"
        if event == "NICK":
            # detail is "→ newnick" or "← oldnick"
            arrow = detail.strip()
            if arrow.startswith("→"):
                target = arrow[1:].strip()
                return f"{bnick} last seen {ago} ago — changed nick → {target}"
            if arrow.startswith("←"):
                target = arrow[1:].strip()
                return f"{bnick} last seen {ago} ago — changed nick ← {target}"
            return f"{bnick} last seen {ago} ago — nick change"
        return f"{bnick} last seen {ago} ago — {event}"

    async def cmd_seen(self, nick: str, reply_to: str, arg: str | None) -> None:
        """`.seen <nick>` — when was the nick last seen."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return
        target = (arg or "").strip().split()[0] if arg and arg.strip() else ""
        if not target:
            p = self.bot.cfg["bot"]["command_prefix"] if "bot" in self.bot.cfg else "."
            self.bot.privmsg(reply_to, f"{nick}: {p}seen <nick>")
            return

        with self._lock:
            entry = self._seen.get(target.lower())
            entry = dict(entry) if entry else None

        if not entry:
            self.bot.privmsg(reply_to, f"never seen {target}")
            return

        self.bot.privmsg(reply_to, self._format_entry(entry))

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}seen <nick>             When was <nick> last seen"]


def setup(bot: object) -> SeenModule:
    """Module entry point — returns a SeenModule instance."""
    return SeenModule(bot)  # type: ignore[arg-type]
