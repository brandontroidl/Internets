"""Reminder module — schedule per-user reminders delivered in-channel.

Commands:
    .remind <when> <message>   schedule a reminder for yourself
    .remind-list               list your pending reminders
    .remind-cancel <N>         cancel reminder #N

Time formats accepted for <when>:
    30s, 5m, 2h, 1d        relative duration
    1h30m, 2d4h, 1d12h30m  combined duration
    tomorrow               24h from now
    tonight                today 20:00 (or tomorrow 20:00 if past)
    HH:MM                  next occurrence (today if future, else tomorrow)
    YYYY-MM-DDTHH:MM       absolute UTC

Min lead time 30s, max 30 days.  Max 10 active reminders per nick,
message max 200 chars.  Persisted to JSON; survives restarts.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import strip_ctrl

from .base import BotModule, help_row

log = logging.getLogger("internets.remind")

MIN_LEAD_SECONDS = 30
MAX_LEAD_SECONDS = 30 * 86400
MAX_PER_NICK = 10
MAX_MSG_LEN = 200
LIST_MAX_LINES = 5

_DURATION_RE = re.compile(r"(?i)^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{1,2}):(\d{2})$")


def _now_ts() -> int:
    return int(time.time())


def _parse_when(token: str, now_ts: int) -> int:
    """Return absolute epoch UTC for the given token.

    Raises ValueError with a human-readable message on failure.
    """
    t = token.strip()
    if not t:
        raise ValueError("missing <when>")

    low = t.lower()

    # tomorrow — exactly 24h from now
    if low == "tomorrow":
        return now_ts + 86400

    # tonight — 20:00 UTC today, or tomorrow if past
    if low == "tonight":
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        target = now_dt.replace(hour=20, minute=0, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)
        return int(target.timestamp())

    # ISO 8601 absolute UTC: YYYY-MM-DDTHH:MM
    m = _ISO_RE.match(t)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError(f"bad ISO date: {e}") from None
        return int(dt.timestamp())

    # HH:MM — next occurrence
    m = _HHMM_RE.match(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("HH:MM out of range")
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        target = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_dt:
            target += timedelta(days=1)
        return int(target.timestamp())

    # Duration: combinations of NdNhNmNs
    m = _DURATION_RE.match(t)
    if m and any(m.groups()):
        days  = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        mins  = int(m.group(3) or 0)
        secs  = int(m.group(4) or 0)
        total = days * 86400 + hours * 3600 + mins * 60 + secs
        if total <= 0:
            raise ValueError("duration is zero")
        return now_ts + total

    raise ValueError(f"can't parse time: {token!r}")


def _fmt_remaining(seconds: int) -> str:
    """Format a positive duration as e.g. '2h 15m' or '1d 4h' or '45s'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _fmt_due_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class RemindModule(BotModule):
    """`.remind` / `.remind-list` / `.remind-cancel` — per-user reminders."""

    COMMANDS: dict[str, str] = {
        "remind": "cmd_remind",
        "remind-list": "cmd_remind_list",
        "remind-cancel": "cmd_remind_cancel",
    }

    def on_load(self) -> None:
        sect = self.bot.cfg["remind"] if "remind" in self.bot.cfg else {}
        self._file = Path(sect.get("file", "reminders.json"))
        self._lock = threading.Lock()
        self._reminders: dict[int, dict[str, Any]] = {}
        self._next_id: int = 1
        self._tasks: dict[int, asyncio.Task] = {}

        self._load()

        # Schedule everything that's still pending.
        loop = getattr(self.bot, "_loop", None)
        if loop is None:
            log.warning("remind: bot has no _loop; reminders will not fire")
            return
        with self._lock:
            ids = list(self._reminders.keys())
        for rid in ids:
            try:
                self._tasks[rid] = loop.create_task(self._fire(rid))
            except Exception as e:
                log.warning(f"remind: failed to schedule #{rid}: {e}")

    def on_unload(self) -> None:
        # Cancel outstanding tasks; persisted JSON re-loads on next on_load.
        for rid, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def is_configured(self) -> bool:
        return True

    def forget(self, nick: str) -> int:
        """Cancel and erase every pending reminder set by ``nick``
        (privacy right-to-erasure)."""
        target = nick.lower()
        with self._lock:
            ids = [rid for rid, r in self._reminders.items()
                   if str(r.get("nick", "")).lower() == target]
            for rid in ids:
                del self._reminders[rid]
            if ids:
                self._save()   # _save documents "caller holds lock"
        # Cancel the timer tasks outside the lock (event-loop thread).
        for rid in ids:
            task = self._tasks.pop(rid, None)
            if task is not None and not task.done():
                task.cancel()
        return len(ids)

    # ---------- persistence ----------

    def _load(self) -> None:
        try:
            if not self._file.exists():
                return
            raw = json.loads(self._file.read_text())
        except Exception as e:
            log.warning(f"remind: failed to read {self._file}: {e}")
            return
        try:
            rems = raw.get("reminders", {}) if isinstance(raw, dict) else {}
            next_id = int(raw.get("next_id", 1)) if isinstance(raw, dict) else 1
            loaded: dict[int, dict[str, Any]] = {}
            for k, v in rems.items():
                try:
                    rid = int(k)
                except (TypeError, ValueError):
                    continue
                if not isinstance(v, dict):
                    continue
                # Minimal shape validation
                if not all(field in v for field in ("id", "nick", "channel", "msg", "due_ts", "created_ts")):
                    continue
                loaded[rid] = {
                    "id": int(v["id"]),
                    "nick": str(v["nick"]),
                    "channel": str(v["channel"]),
                    "msg": str(v["msg"]),
                    "due_ts": int(v["due_ts"]),
                    "created_ts": int(v["created_ts"]),
                }
            self._reminders = loaded
            # Ensure next_id is past anything we loaded
            highest = max(loaded.keys(), default=0)
            self._next_id = max(next_id, highest + 1)
            log.info(f"remind: loaded {len(loaded)} reminder(s) from {self._file}")
        except Exception as e:
            log.warning(f"remind: malformed {self._file}: {e}")
            self._reminders = {}
            self._next_id = 1

    def _save(self) -> None:
        """Atomic write: tempfile + os.replace, mode 0o600.  Caller holds lock."""
        payload = {
            "next_id": self._next_id,
            "reminders": {str(k): v for k, v in self._reminders.items()},
        }
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".remind-",
                suffix=".json.tmp",
                dir=str(self._file.parent) if str(self._file.parent) else ".",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f, indent=2)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self._file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            log.warning(f"remind: failed to save {self._file}: {e}")

    # ---------- helpers ----------

    def _count_for(self, nick_lower: str) -> int:
        return sum(1 for r in self._reminders.values()
                   if r["nick"].lower() == nick_lower)

    def _list_for(self, nick_lower: str) -> list[dict[str, Any]]:
        rems = [r for r in self._reminders.values()
                if r["nick"].lower() == nick_lower]
        rems.sort(key=lambda r: r["due_ts"])
        return rems

    # ---------- delivery ----------

    async def _fire(self, rid: int) -> None:
        """Sleep until due, then deliver and remove."""
        try:
            with self._lock:
                rem = self._reminders.get(rid)
            if rem is None:
                return

            delay = rem["due_ts"] - _now_ts()
            if delay > 0:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return

            # Re-check existence (could have been cancelled while sleeping).
            with self._lock:
                rem = self._reminders.pop(rid, None)
                if rem is not None:
                    self._save()
            if rem is None:
                return

            now = _now_ts()
            late = now - rem["due_ts"]
            if late > 5:
                # Bot was down past the due time.
                tail = f"  (was due {_fmt_remaining(late)} ago)"
            else:
                elapsed = now - rem["created_ts"]
                tail = f"  (set {_fmt_remaining(elapsed)} ago)"

            try:
                self.bot.privmsg(
                    rem["channel"],
                    f"{rem['nick']}: ⏰ {rem['msg']}{tail}",
                )
            except Exception as e:
                log.warning(f"remind: deliver #{rid} failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"remind: _fire #{rid} crashed: {e}")
        finally:
            self._tasks.pop(rid, None)

    # ---------- commands ----------

    async def cmd_remind(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Usage: .remind <when> <message>"""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg or not arg.strip():
            self.bot.privmsg(
                reply_to,
                f"{nick}: usage: {p}remind <when> <message>  "
                f"(e.g. 30s, 5m, 1h30m, tomorrow, tonight, 14:30, 2026-05-20T18:00 — clock times are UTC)",
            )
            return

        parts = arg.strip().split(None, 1)
        if len(parts) < 2:
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}remind <when> <message>")
            return

        when_token, message = parts[0], parts[1].strip()
        if not message:
            self.bot.privmsg(reply_to, f"{nick}: message is empty")
            return
        if len(message) > MAX_MSG_LEN:
            self.bot.privmsg(reply_to, f"{nick}: message too long (max {MAX_MSG_LEN} chars)")
            return
        # Strip IRC control bytes at capture so a stored-then-replayed reminder
        # (possibly across restarts, bot-attributed) can't carry format/colour/
        # BEL/ANSI injection - covers both the immediate ack and delayed delivery.
        message = strip_ctrl(message, MAX_MSG_LEN)
        if not message:
            self.bot.privmsg(reply_to, f"{nick}: message is empty")
            return

        # Only deliver in channels (privmsg replies have reply_to == nick, which
        # is fine — but bare DMs are still allowed: we just echo back to nick).
        channel = reply_to

        now = _now_ts()
        try:
            due_ts = _parse_when(when_token, now)
        except ValueError as e:
            self.bot.privmsg(reply_to, f"{nick}: {e}")
            return

        lead = due_ts - now
        if lead < MIN_LEAD_SECONDS:
            self.bot.privmsg(
                reply_to,
                f"{nick}: too soon — minimum lead time is {MIN_LEAD_SECONDS}s",
            )
            return
        if lead > MAX_LEAD_SECONDS:
            self.bot.privmsg(
                reply_to,
                f"{nick}: too far out — maximum lead time is 30 days",
            )
            return

        nick_lower = nick.lower()
        with self._lock:
            if self._count_for(nick_lower) >= MAX_PER_NICK:
                self.bot.privmsg(
                    reply_to,
                    f"{nick}: you already have {MAX_PER_NICK} pending reminders — "
                    f"cancel one with {p}remind-cancel <N>",
                )
                return
            rid = self._next_id
            self._next_id += 1
            self._reminders[rid] = {
                "id": rid,
                "nick": nick,
                "channel": channel,
                "msg": message,
                "due_ts": due_ts,
                "created_ts": now,
            }
            self._save()

        loop = getattr(self.bot, "_loop", None)
        if loop is not None:
            try:
                self._tasks[rid] = loop.create_task(self._fire(rid))
            except Exception as e:
                log.warning(f"remind: failed to schedule new #{rid}: {e}")

        self.bot.privmsg(
            reply_to,
            f"{nick}: ⏰ reminder #{rid} set for {_fmt_due_utc(due_ts)} — {message}",
        )

    async def cmd_remind_list(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Usage: .remind-list — list YOUR pending reminders."""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        nick_lower = nick.lower()
        with self._lock:
            rems = self._list_for(nick_lower)

        if not rems:
            self.bot.privmsg(reply_to, f"{nick}: no pending reminders")
            return

        now = _now_ts()
        lines: list[str] = []
        for r in rems[:LIST_MAX_LINES]:
            remaining = r["due_ts"] - now
            if remaining < 86400:
                when_str = f"in {_fmt_remaining(remaining)}"
            else:
                when_str = f"at {_fmt_due_utc(r['due_ts'])}"
            snippet = r["msg"] if len(r["msg"]) <= 60 else r["msg"][:57] + "..."
            lines.append(f"#{r['id']} {when_str}: {snippet}")

        out = f"{nick}: your reminders: " + " | ".join(lines)
        extra = len(rems) - LIST_MAX_LINES
        if extra > 0:
            out += f"  (+{extra} more)"
        self.bot.privmsg(reply_to, out)

    async def cmd_remind_cancel(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Usage: .remind-cancel <N>"""
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return

        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg or not arg.strip():
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}remind-cancel <N>")
            return

        token = arg.strip().split()[0].lstrip("#")
        try:
            rid = int(token)
        except ValueError:
            self.bot.privmsg(reply_to, f"{nick}: '{token}' is not a number")
            return

        nick_lower = nick.lower()
        with self._lock:
            rem = self._reminders.get(rid)
            if rem is None or rem["nick"].lower() != nick_lower:
                self.bot.privmsg(reply_to, f"{nick}: not found / not yours")
                return
            del self._reminders[rid]
            self._save()

        task = self._tasks.pop(rid, None)
        if task is not None and not task.done():
            task.cancel()

        self.bot.privmsg(reply_to, f"{nick}: cancelled reminder #{rid}")

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "remind <when> <msg>", "Schedule a reminder (30s, 5m, 1h30m, tonight, 14:30 UTC, ISO)"),
            help_row(prefix, "remind-list", "List your pending reminders"),
            help_row(prefix, "remind-cancel <N>", "Cancel reminder #N"),
        ]


def setup(bot: object) -> RemindModule:
    """Module entry point — returns a RemindModule instance."""
    return RemindModule(bot)  # type: ignore[arg-type]
