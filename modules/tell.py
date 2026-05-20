"""Offline ``.tell`` — leave messages for users delivered on their next PRIVMSG.

Commands:
    .tell <nick> <message>   Leave a message for <nick>.
    .tell-cancel             Cancel all of YOUR pending tells.
    .tell-list               List YOUR pending tells.

Delivery happens via the synchronous ``on_raw`` hook: every incoming PRIVMSG
is inspected; if the sender has queued tells, they are flushed to the channel
(or back to the sender if the message was a PM to the bot).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import BotModule

log = logging.getLogger("internets.tell")

_PRIVMSG_RE = re.compile(r"^:([^!\s]+)![^\s]+\s+PRIVMSG\s+(\S+)\s+:(.*)$")

_MAX_TELLS_PER_RECIPIENT = 10
_MAX_TELLS_PER_SENDER    = 5
_MAX_MSG_LEN             = 350
_MAX_LIST_LINES          = 5
_TTL_SECONDS             = 30 * 24 * 60 * 60  # 30 days


def _timeago(ts: int) -> str:
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class TellModule(BotModule):
    """Offline message delivery — `.tell`, `.tell-cancel`, `.tell-list`."""

    COMMANDS: dict[str, str] = {
        "tell":        "cmd_tell",
        "tell-cancel": "cmd_tell_cancel",
        "tell-list":   "cmd_tell_list",
    }

    def on_load(self) -> None:
        sect = self.bot.cfg["tell"] if "tell" in self.bot.cfg else {}
        self._file = Path(sect.get("file", "tells.json"))
        self._lock = threading.Lock()
        self._tells: dict[str, list[dict]] = {}
        try:
            if self._file.exists():
                raw = json.loads(self._file.read_text())
                if isinstance(raw, dict):
                    # Coerce to expected shape and drop any malformed entries.
                    for k, v in raw.items():
                        if isinstance(v, list):
                            clean = [e for e in v if isinstance(e, dict)
                                     and "from" in e and "msg" in e and "ts" in e]
                            if clean:
                                self._tells[k.lower()] = clean
        except Exception as e:
            log.warning(f"tell: failed to load {self._file}: {e}")
            self._tells = {}
        self._expire_locked_unsafe()  # caller holds no lock; safe at startup

    # ---- persistence -----------------------------------------------------

    def _expire_locked_unsafe(self) -> None:
        """Drop expired entries. Caller must hold ``self._lock`` OR be running
        before ``on_raw`` is hooked up (startup)."""
        cutoff = int(time.time()) - _TTL_SECONDS
        dead_keys = []
        for k, entries in self._tells.items():
            fresh = [e for e in entries if int(e.get("ts", 0)) >= cutoff]
            if fresh:
                self._tells[k] = fresh
            else:
                dead_keys.append(k)
        for k in dead_keys:
            del self._tells[k]

    def _save_sync(self) -> None:
        """Atomic write of the tell store.  Called via asyncio.to_thread."""
        with self._lock:
            self._expire_locked_unsafe()
            snapshot = {k: list(v) for k, v in self._tells.items()}
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=self._file.name + ".",
                suffix=".tmp",
                dir=str(self._file.parent) if str(self._file.parent) else ".",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(snapshot, f, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._file)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception as e:
            log.warning(f"tell: failed to save {self._file}: {e}")

    # ---- raw IRC delivery hook ------------------------------------------

    def on_raw(self, line: str) -> None:
        m = _PRIVMSG_RE.match(line)
        if not m:
            return
        sender, target, _message = m.group(1), m.group(2), m.group(3)
        key = sender.lower()

        # Cheap pre-check to avoid taking the lock for every line.
        if key not in self._tells:
            return

        with self._lock:
            entries = self._tells.get(key)
            if not entries:
                return
            # Expire on lookup.
            cutoff = int(time.time()) - _TTL_SECONDS
            entries = [e for e in entries if int(e.get("ts", 0)) >= cutoff]
            if not entries:
                self._tells.pop(key, None)
                changed = True
                to_deliver: list[dict] = []
            else:
                to_deliver = entries
                self._tells.pop(key, None)
                changed = True

        if not to_deliver:
            if changed:
                self._schedule_save()
            return

        # If the inbound message was a PM to the bot, target == bot's nick;
        # deliver back to the sender directly instead of into a "channel".
        bot_nick = getattr(self.bot, "_nick", "") or ""
        if target.startswith("#") or target.startswith("&"):
            reply_target = target
        elif target.lower() == bot_nick.lower():
            reply_target = sender
        else:
            # Some other non-channel target (shouldn't normally happen) —
            # fall back to messaging the sender.
            reply_target = sender

        for e in to_deliver:
            orig_sender = e.get("from", "?")
            msg         = e.get("msg", "")
            ts          = int(e.get("ts", 0))
            display_to  = e.get("to", sender)
            try:
                self.bot.privmsg(
                    reply_target,
                    f"{display_to}: {orig_sender} said at {_fmt_ts(ts)}: {msg}",
                )
            except Exception as ex:
                log.warning(f"tell: delivery failed for {sender}: {ex}")

        self._schedule_save()

    def _schedule_save(self) -> None:
        """Best-effort async flush from the event-loop thread."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            try:
                loop.create_task(asyncio.to_thread(self._save_sync))
                return
            except Exception:
                pass
        # Fallback: synchronous write (rare — only at startup/shutdown).
        try:
            self._save_sync()
        except Exception as e:
            log.debug(f"tell: sync save fallback failed: {e}")

    # ---- commands --------------------------------------------------------

    async def cmd_tell(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Leave a message for someone.  Usage: .tell <nick> <message>"""
        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg or not arg.strip():
            self.bot.privmsg(reply_to, f"{nick}: {p}tell <nick> <message>")
            return
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        parts = arg.strip().split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self.bot.privmsg(reply_to, f"{nick}: {p}tell <nick> <message>")
            return
        target_nick = parts[0].strip()
        message     = parts[1].strip()

        if len(message) > _MAX_MSG_LEN:
            self.bot.privmsg(
                reply_to,
                f"{nick}: message too long (max {_MAX_MSG_LEN} chars)",
            )
            return

        if target_nick.lower() == nick.lower():
            self.bot.privmsg(reply_to, f"{nick}: just write yourself a note")
            return
        bot_nick = getattr(self.bot, "_nick", "") or ""
        if bot_nick and target_nick.lower() == bot_nick.lower():
            self.bot.privmsg(reply_to, f"{nick}: I'm right here.")
            return

        key = target_nick.lower()
        now = int(time.time())

        with self._lock:
            self._expire_locked_unsafe()
            queue = self._tells.get(key, [])
            if len(queue) >= _MAX_TELLS_PER_RECIPIENT:
                self.bot.privmsg(
                    reply_to,
                    f"{nick}: queue full — {target_nick} has "
                    f"{_MAX_TELLS_PER_RECIPIENT} messages waiting",
                )
                return
            # Sender-spam cap: count all tells this sender has outstanding,
            # across every recipient.
            sender_count = 0
            for entries in self._tells.values():
                for e in entries:
                    if str(e.get("from", "")).lower() == nick.lower():
                        sender_count += 1
            if sender_count >= _MAX_TELLS_PER_SENDER:
                self.bot.privmsg(
                    reply_to,
                    f"{nick}: you already have {_MAX_TELLS_PER_SENDER} pending "
                    f"tells — use {p}tell-cancel or {p}tell-list",
                )
                return

            queue.append({
                "from": nick,
                "msg":  message,
                "ts":   now,
                "to":   target_nick,
            })
            self._tells[key] = queue

        await asyncio.to_thread(self._save_sync)
        self.bot.notice(nick, f"will tell {target_nick} when they next speak")

    async def cmd_tell_cancel(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Cancel all pending tells YOU've sent."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        sender_lc = nick.lower()
        removed = 0
        with self._lock:
            self._expire_locked_unsafe()
            empty_keys = []
            for k, entries in self._tells.items():
                kept = []
                for e in entries:
                    if str(e.get("from", "")).lower() == sender_lc:
                        removed += 1
                    else:
                        kept.append(e)
                if kept:
                    self._tells[k] = kept
                else:
                    empty_keys.append(k)
            for k in empty_keys:
                del self._tells[k]

        if removed:
            await asyncio.to_thread(self._save_sync)
        self.bot.notice(nick, f"cancelled {removed} pending tell(s)")

    async def cmd_tell_list(self, nick: str, reply_to: str, arg: str | None) -> None:
        """List your pending tells (delivered via NOTICE)."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        sender_lc = nick.lower()
        mine: list[dict] = []
        with self._lock:
            self._expire_locked_unsafe()
            for entries in self._tells.values():
                for e in entries:
                    if str(e.get("from", "")).lower() == sender_lc:
                        mine.append(e)

        if not mine:
            self.bot.notice(nick, "no pending tells from you")
            return

        mine.sort(key=lambda e: int(e.get("ts", 0)))
        for e in mine[:_MAX_LIST_LINES]:
            to_disp = e.get("to", "?")
            msg     = str(e.get("msg", ""))
            preview = msg if len(msg) <= 80 else msg[:77] + "..."
            ts      = int(e.get("ts", 0))
            self.bot.notice(
                nick,
                f"to {to_disp}: '{preview}' ({_timeago(ts)})",
            )
        if len(mine) > _MAX_LIST_LINES:
            self.bot.notice(
                nick,
                f"... and {len(mine) - _MAX_LIST_LINES} more",
            )

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}tell <nick> <msg>      Leave a message for <nick>",
            f"  {prefix}tell-cancel            Cancel all your pending tells",
            f"  {prefix}tell-list              List your pending tells",
        ]


def setup(bot: object) -> TellModule:
    """Module entry point — returns a TellModule instance."""
    return TellModule(bot)  # type: ignore[arg-type]
