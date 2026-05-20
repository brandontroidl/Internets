#!/usr/bin/env python3
"""
Internets — async modular IRC bot.

Architecture: asyncio event loop for connection, dispatch, and background
tasks.  Module command handlers are coroutines.  Blocking I/O (HTTP via
requests, disk, CPU-heavy work like password hashing) runs via
asyncio.to_thread() inside the handler.

Core commands: .help .modules .auth .deauth
               .load .unload .reload .reloadall .restart .rehash
               .version

Modules live in modules/. Each exposes setup(bot) -> BotModule.
See modules/base.py for the interface.
"""

from __future__ import annotations

import asyncio
import ssl
import re
import sys
import os
import random
import secrets
import signal
import threading
import logging
import importlib
import importlib.util
import time
from pathlib import Path
from typing import Any

from config import (
    __version__,
    cfg, CONFIG_PATH,
    SERVER, PORT, NICKNAME, REALNAME,
    NS_PW, SERVER_PW, OPER_N, OPER_PW,
    USER_MODES, OPER_MODES, OPER_SNOMASK,
    CMD_PREFIX, FLOOD_CD,
    MODULES_DIR, AUTO_LOAD, DESIRED_CAPS,
    cli_args, API_CD,
)
from botlog import log, log_filter  # noqa: F401 — log_filter used by tests
from admin_cmds import AdminCommandsMixin
from console import run_console, should_skip_console
from store import Store, RateLimiter
from sender import Sender
from protocol import (
    strip_tags,
    parse_isupport_chanmodes,
    parse_isupport_prefix,
    parse_mode_changes,
    parse_names_entry,
    sasl_plain_payload,
)
from process_lock import ProcessLock, LockHeld


# ── Per-subsystem loggers ────────────────────────────────────────────
# These inherit from the "internets" root logger configured in botlog.py.
# Using them gives operators per-subsystem .debug control.
_LOG_CONN     = logging.getLogger("internets.conn")
_LOG_DISPATCH = logging.getLogger("internets.dispatch")
_LOG_MODULES  = logging.getLogger("internets.modules")
_LOG_SIGNAL   = logging.getLogger("internets.signal")
_LOG_SHUTDOWN = logging.getLogger("internets.shutdown")
_LOG_SASL     = logging.getLogger("internets.sasl")

# ── Utilities ────────────────────────────────────────────────────────

class ChannelSet:
    """Thread-safe set of active channel names (lowercased)."""

    def __init__(self) -> None:
        self._channels: set[str] = set()
        self._lock = threading.Lock()

    def add(self, ch: str) -> None:
        with self._lock: self._channels.add(ch.lower())

    def discard(self, ch: str) -> None:
        with self._lock: self._channels.discard(ch.lower())

    def snapshot(self) -> set[str]:
        with self._lock: return set(self._channels)

    def __contains__(self, ch: str) -> bool:
        with self._lock: return ch.lower() in self._channels

    def __iter__(self):
        return iter(self.snapshot())

    def __bool__(self) -> bool:
        with self._lock: return bool(self._channels)

    def __len__(self) -> int:
        with self._lock: return len(self._channels)


def _backoff(attempt: int, base: float = 15.0, cap: float = 300.0) -> float:
    """Deterministic exponential backoff: base * 2**attempt, capped at *cap*.

    Kept deterministic for testability; callers needing jitter should use
    ``_backoff_jittered`` which wraps this with bounded randomization.
    """
    return min(base * (2 ** attempt), cap)


# Jitter factor applied to reconnect delays.  Full-jitter style (Decorrelated
# Jitter, AWS) would compound state; we use the simpler "equal jitter" model:
# the wait is a value in [delay * (1 - JITTER), delay * (1 + JITTER)].
_BACKOFF_JITTER = 0.25


def _backoff_jittered(attempt: int, base: float = 15.0, cap: float = 300.0,
                       jitter: float = _BACKOFF_JITTER) -> float:
    """Exponential backoff with bounded jitter.  Returns >= 0 seconds.

    Uses ``random`` (not ``secrets``): jitter is for thundering-herd
    avoidance, not security.
    """
    delay = _backoff(attempt, base, cap)
    spread = delay * jitter
    return max(0.0, delay + random.uniform(-spread, spread))


# Note: distinguishing transient (DNS/RST/SSL renegotiation) from permanent
# (auth) connection failures is handled inline in run() using the
# self._sasl_failed_permanently flag set by the SASL handler — no dedicated
# exception class is needed.


# ═════════════════════════════════════════════════════════════════════
# IRCBot
# ═════════════════════════════════════════════════════════════════════

class IRCBot(AdminCommandsMixin):
    """Async IRC bot core — event loop, state machine, command dispatch.

    Admin command handlers live in AdminCommandsMixin (admin_cmds.py).
    """
    _MAX_BODY = 400
    _MAX_TASKS = 50
    _MAX_ARG_LEN = 400
    _AUTH_CLEANUP_THRESHOLD = 50
    _AUTH_MAX_FAILS = 5
    _AUTH_LOCKOUT = 300

    # ── Network / timing constants (no magic numbers in code) ────────
    _READ_LIMIT          = 8192   # asyncio.open_connection buffer cap (bytes)
    _READ_TIMEOUT        = 300    # seconds — read inactivity → reconnect
    _PING_INTERVAL       = 90     # seconds between client-side PINGs
    _PONG_MAX_PAYLOAD    = 400    # bytes — guard against oversized PONGs
    _NICKSERV_WAIT_TICKS = 40     # 40 * _NICKSERV_TICK = 10 s total
    _NICKSERV_TICK       = 0.25   # seconds between identify polls
    _SHUTDOWN_DRAIN_S    = 2.0    # grace period for sender to flush QUIT
    _UNEXPECTED_SLEEP_S  = 5.0    # back-off on unexpected main-loop errors
    _MAX_PONG_LEN        = 400    # bytes — cap PING/PONG payload

    # Precompiled regex for _process() hot path.
    _RE_CAP       = re.compile(r"(?::\S+ )?CAP \S+ (\S+)(?: :?(.*))?")
    _RE_903       = re.compile(r":\S+ 903 ")
    _RE_SASL_FAIL = re.compile(r":\S+ (?:902|904|905) ")
    _RE_421_CAP   = re.compile(r":\S+ 421 \S+ CAP ")
    _RE_451       = re.compile(r":\S+ 451 ")
    _RE_433       = re.compile(r":\S+ 433 ")
    _RE_005       = re.compile(r":\S+ 005 ")
    _RE_CHANMODES = re.compile(r"CHANMODES=(\S+)")
    _RE_PREFIX    = re.compile(r"PREFIX=(\S+)")
    _RE_473       = re.compile(r":\S+ 473 \S+ (\S+)")
    _RE_JOIN_ERR  = re.compile(r":\S+ (?:403|405|471|474|475|476) \S+ (\S+)")
    _RE_381       = re.compile(r":\S+ 381 ")
    _RE_491       = re.compile(r":\S+ 491 ")
    _RE_900       = re.compile(r":\S+ 900 ")
    _RE_NOTICE    = re.compile(r":([^!]+)!\S+ NOTICE \S+ :(.*)")
    _RE_353       = re.compile(r":\S+ 353 \S+ [=*@] (\S+) :(.*)")
    _RE_MODE      = re.compile(r":\S+ MODE (\S+) ([+-]\S+)(.*)")
    _RE_CHGHOST   = re.compile(r":([^!]+)![^@]+@\S+ CHGHOST (\S+) (\S+)")
    _RE_ACCOUNT   = re.compile(r":([^!]+)![^@]+@\S+ ACCOUNT (\S+)")
    _RE_INVITE    = re.compile(r":([^!]+)![^@]+@\S+ INVITE \S+ :?(\S+)")
    _RE_JOIN      = re.compile(r":([^!]+)!(\S+) JOIN :?(\S+)(?:\s+\S+)?")
    _RE_PART      = re.compile(r":([^!]+)![^@]+@\S+ PART :?(\S+)")
    _RE_KICK      = re.compile(r":\S+ KICK (\S+) (\S+)")
    _RE_QUIT      = re.compile(r":([^!]+)![^@]+@\S+ QUIT")
    _RE_NICK      = re.compile(r":([^!]+)!(\S+) NICK :?(\S+)")
    _RE_PRIVMSG   = re.compile(r":([^!]+)!(\S+) PRIVMSG (\S+) :(.*)")
    _RE_MOTD      = re.compile(r":\S+ (?:376|422) ")
    _RE_AUTH_LOG  = re.compile(r"PRIVMSG\s+\S+\s+:\.?AUTH\s", re.IGNORECASE)
    _CHAN_RE      = re.compile(r"^[#&+!][^\s,\x07]{1,49}$")

    _CORE: dict[str, str] = {
        "help": "cmd_help", "modules": "cmd_modules", "version": "cmd_version",
        "auth": "cmd_auth", "deauth": "cmd_deauth",
        "load": "cmd_load", "unload": "cmd_unload",
        "reload": "cmd_reload", "reloadall": "cmd_reloadall",
        "restart": "cmd_restart", "rehash": "cmd_rehash",
        "mode": "cmd_mode", "snomask": "cmd_snomask", "raw": "cmd_raw",
        "say": "cmd_say", "act": "cmd_act", "audit": "cmd_audit",
        "uptime": "cmd_uptime", "nick": "cmd_nick", "stats": "cmd_stats",
        "fingerprint": "cmd_fingerprint",
        "shadow-ban":   "cmd_shadow_ban",
        "shadow-unban": "cmd_shadow_unban",
        "shadow-list":  "cmd_shadow_list",
        "shutdown": "cmd_shutdown", "die": "cmd_shutdown",
        "loglevel": "cmd_loglevel", "debug": "cmd_debug",
    }

    def __init__(self) -> None:
        self.cfg               = cfg
        self.active_channels   = ChannelSet()
        self._modules: dict[str, Any]  = {}
        self._commands: dict[str, tuple[str, str]] = {}
        self._mod_lock       = threading.Lock()
        self._auth_lock      = threading.Lock()
        self._authed: dict[str, str] = {}
        self._auth_fails: dict[str, tuple[int, float]] = {}
        self._nick: str    = NICKNAME
        self._chanops: dict[str, set[str]] = {}
        self._ns_identified = False
        self._sasl_in_progress = False
        self._cap_busy = False
        self._caps: set[str] = set()
        self._services_nick = cfg["bot"].get("services_nick", "ChanServ").strip()
        self._chanmode_types: dict[str, str] = {
            "b": "A", "e": "A", "I": "A", "k": "B", "l": "C",
            "i": "D", "m": "D", "n": "D", "p": "D", "s": "D", "t": "D",
        }
        self._prefix_modes: set[str] = set("qaohv")
        self._store = Store(
            cfg["bot"].get("locations_file", "locations.json"),
            cfg["bot"].get("channels_file",  "channels.json"),
            cfg["bot"].get("users_file",     "users.json"),
            user_max_age_days=int(cfg["bot"].get("user_max_age_days", "90")),
        )
        self._rate = RateLimiter(FLOOD_CD, API_CD)
        # ── stats counters surfaced via .stats / .uptime ────────────
        self._stats_boot_ts:    float        = time.time()
        self._stats_connect_ts: float | None = None
        self._stats_cmd_count:  int          = 0
        self._stats_msg_in:     int          = 0
        self._stats_msg_out:    int          = 0
        # ── shadow-ban set: nicks whose traffic is silently dropped ─
        # Loaded from shadow_bans.json (0600).  Keys lowercased.
        self._shadow_bans:          set[str]      = set()
        self._shadow_ban_reasons:   dict[str, str] = {}
        self._shadow_bans_file:     str = cfg["bot"].get("shadow_bans_file",
                                                         "shadow_bans.json")
        self._load_shadow_bans()
        self._loop:    asyncio.AbstractEventLoop | None = None
        self._sender:  Sender | None = None
        self._reader:  asyncio.StreamReader | None = None
        self._writer:  asyncio.StreamWriter | None = None
        self._stop:    asyncio.Event | None = None
        self._quit_msg = "QUIT :Shutting down"
        self._restart_flag = False
        self._tasks: list[asyncio.Task] = []
        self._last_invite_time: float = 0.0
        self._nick_hosts: dict[str, str] = {}
        # Counter of in-flight command tasks — O(1) check vs. scanning _tasks.
        self._active_cmd_tasks: int = 0
        # Idempotency guard for signal handlers (fired once even if SIGINT
        # arrives twice during shutdown).
        self._shutdown_initiated: bool = False
        # SASL hard failure marks the connection as unrecoverable until config
        # is fixed; reconnect loop should surface this clearly.
        self._sasl_failed_permanently: bool = False
        # Observability counters.  Exposed for the .debug command and tests.
        self._metrics: dict[str, int] = {
            "reconnects":         0,
            "dropped_messages":   0,
            "command_timeouts":   0,
            "oversized_lines":    0,
            "sasl_failures":      0,
            "unexpected_errors":  0,
        }

    # ── Outbound messaging ───────────────────────────────────────────

    def send(self, msg: str, priority: int = 1) -> None:
        if self._sender:
            self._sender.enqueue(msg, priority)
            self._stats_msg_out += 1

    def privmsg(self, target: str, msg: str) -> None:
        if " " in target or not target:
            log.warning(f"privmsg: invalid target {target!r}"); return
        for chunk in self._split_msg(msg):
            self.send(f"PRIVMSG {target} :{chunk}")

    def notice(self, target: str, msg: str) -> None:
        if " " in target or not target:
            log.warning(f"notice: invalid target {target!r}"); return
        for chunk in self._split_msg(msg):
            self.send(f"NOTICE {target} :{chunk}")

    def reply(self, nick: str, reply_to: str, msg: str,
              privileged: bool = False) -> None:
        if not reply_to.startswith(("#", "&", "+", "!")):
            self.privmsg(nick, msg)
        elif privileged:
            self.notice(nick, msg)
        else:
            self.privmsg(reply_to, msg)

    def preply(self, nick: str, reply_to: str, msg: str) -> None:
        self.reply(nick, reply_to, msg, privileged=True)

    def _split_msg(self, msg: str) -> list[str]:
        chunks: list[str] = []
        enc = msg.encode("utf-8", errors="replace")
        while enc:
            chunk = enc[:self._MAX_BODY]
            if len(enc) > self._MAX_BODY:
                while chunk and (chunk[-1] & 0xC0) == 0x80:
                    chunk = chunk[:-1]
                if not chunk:
                    chunk = enc[:self._MAX_BODY]
            chunks.append(chunk.decode("utf-8", errors="replace"))
            enc = enc[len(chunk):]
        return chunks

    # ── Accessors ────────────────────────────────────────────────────

    def is_admin(self, nick: str) -> bool:
        k = nick.lower()
        with self._auth_lock:
            if k not in self._authed: return False
            stored = self._authed[k]
            current = self._nick_hosts.get(k)
            if current and stored != "unknown" and current != stored:
                del self._authed[k]
                log.warning(f"Auth revoked for {nick}: hostmask changed ({stored} → {current})")
                return False
            return True

    def is_chanop(self, channel: str, nick: str) -> bool:
        return nick.lower() in self._chanops.get(channel.lower(), set())

    def flood_limited(self, nick: str) -> bool:
        return self._rate.flood_check(nick, self.is_admin(nick))

    def rate_limited(self, nick: str) -> bool:
        return self._rate.api_check(nick)

    def channel_limited(self, channel: str) -> bool:
        """True if *channel* has hit the cross-user burst threshold.

        Sits on top of per-nick flood/api limiters: catches coordinated
        floods where many distinct nicks each send 1 command/sec.
        """
        return self._rate.channel_check(channel)

    # ── Shadow-ban store ─────────────────────────────────────────────

    def is_shadow_banned(self, nick: str) -> bool:
        """True if ``nick`` is on the shadow-ban list (case-insensitive)."""
        return nick.lower() in self._shadow_bans

    def _load_shadow_bans(self) -> None:
        """Read shadow_bans.json into ``self._shadow_bans``.  Tolerant of
        missing/corrupt files — the list just stays empty."""
        try:
            import json as _json
            from pathlib import Path as _Path
            p = _Path(self._shadow_bans_file)
            if not p.exists():
                return
            data = _json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                bans = data.get("bans") or []
                reasons = data.get("reasons") or {}
                if isinstance(bans, list):
                    self._shadow_bans = {str(n).lower() for n in bans}
                if isinstance(reasons, dict):
                    self._shadow_ban_reasons = {
                        str(k).lower(): str(v) for k, v in reasons.items()
                    }
        except Exception as e:
            log.warning(f"shadow_bans: load failed: {type(e).__name__}: {e}")

    def _save_shadow_bans(self) -> None:
        """Atomic write of the shadow-ban list to disk, 0600 perms."""
        try:
            import json as _json
            import os as _os
            import tempfile as _tempfile
            from pathlib import Path as _Path
            p = _Path(self._shadow_bans_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "bans":    sorted(self._shadow_bans),
                "reasons": self._shadow_ban_reasons,
            }
            fd, tmp_path = _tempfile.mkstemp(prefix=p.name + ".",
                                             dir=str(p.parent))
            try:
                with _os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(payload, f, indent=2, ensure_ascii=False)
                _os.chmod(tmp_path, 0o600)
                _os.replace(tmp_path, p)
            except Exception:
                try: _os.unlink(tmp_path)
                except OSError: pass
                raise
        except Exception as e:
            log.warning(f"shadow_bans: save failed: {type(e).__name__}: {e}")

    def loc_get(self, nick: str) -> str | None: return self._store.loc_get(nick)
    def loc_set(self, nick: str, raw: str) -> None: self._store.loc_set(nick, raw)
    def loc_del(self, nick: str) -> bool: return self._store.loc_del(nick)
    def channel_users(self, ch: str) -> dict[str, Any]: return self._store.channel_users(ch)

    # ── Module management ────────────────────────────────────────────

    def load_module(self, name: str) -> tuple[bool, str]:
        with self._mod_lock:
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                return False, f"Invalid module name '{name}'."
            if name in self._modules:
                return False, f"'{name}' already loaded."
            path = MODULES_DIR / f"{name}.py"
            if not path.exists():
                return False, f"'{path}' not found."
            try:
                path.resolve().relative_to(MODULES_DIR.resolve())
            except ValueError:
                return False, f"'{name}' blocked — path escapes modules directory."
            try:
                spec = importlib.util.spec_from_file_location(f"modules.{name}", path)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not hasattr(mod, "setup"):
                    return False, f"'{name}' has no setup()."
                inst = mod.setup(self)
                dupes = [c for c in inst.COMMANDS if c in self._commands and self._commands[c][0] != name]
                if dupes:
                    return False, f"'{name}' conflicts on: {', '.join(dupes)}"
                inst.on_load()
                self._modules[name] = inst
                for cmd, method in inst.COMMANDS.items():
                    self._commands[cmd] = (name, method)
                _LOG_MODULES.info(
                    "event=module_loaded name=%s commands=%d cmds=%s",
                    name, len(inst.COMMANDS), ",".join(sorted(inst.COMMANDS)))
                return True, f"'{name}' loaded ({len(inst.COMMANDS)} commands)."
            except Exception as e:
                _LOG_MODULES.error("event=module_load_failed name=%s err=%s", name, e)
                return False, f"Error loading '{name}' — see log for details."

    def unload_module(self, name: str) -> tuple[bool, str]:
        with self._mod_lock:
            if name not in self._modules:
                return False, f"'{name}' not loaded."
            try:
                self._modules[name].on_unload()
                removed = [c for c, v in self._commands.items() if v[0] == name]
                for cmd in removed:
                    del self._commands[cmd]
                del self._modules[name]
                _LOG_MODULES.info(
                    "event=module_unloaded name=%s commands=%d", name, len(removed))
                return True, f"'{name}' unloaded."
            except Exception as e:
                _LOG_MODULES.error("event=module_unload_failed name=%s err=%s", name, e)
                return False, f"Error unloading '{name}' — see log for details."

    def reload_module(self, name: str) -> tuple[bool, str]:
        ok, msg = self.unload_module(name)
        return (False, msg) if not ok else self.load_module(name)

    def autoload_modules(self) -> None:
        for name in AUTO_LOAD:
            ok, msg = self.load_module(name)
            (log.info if ok else log.warning)(msg)

    # ── Shutdown ─────────────────────────────────────────────────────

    def request_shutdown(self, reason: str = "Shutting down") -> None:
        """Request a graceful shutdown.  Safe to call multiple times and from
        any thread; idempotent — only the first reason wins to avoid races
        where SIGINT during a clean shutdown rewrites the QUIT message.
        """
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        self._quit_msg = f"QUIT :{reason}"
        if self._stop and self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def graceful_shutdown(self) -> None:
        _LOG_SHUTDOWN.info("event=shutdown_begin reason=%r", self._quit_msg)
        # 1. Persist channel list to disk before anything else can fail.
        try: self._save_channels()
        except Exception as e: _LOG_SHUTDOWN.warning("event=channel_save_failed err=%s", e)
        # 2. Unload modules (gives them a chance to flush their own state).
        with self._mod_lock: names = list(self._modules)
        for name in names:
            try: self.unload_module(name)
            except Exception as e:
                _LOG_SHUTDOWN.warning("event=module_unload_failed name=%s err=%s", name, e)
        # 3. Stop the store flush thread and force a final write.
        try:
            self._store.stop()
            _LOG_SHUTDOWN.info("event=store_flushed")
        except Exception as e:
            _LOG_SHUTDOWN.warning("event=store_flush_failed err=%s", e)
        # 4. Enqueue QUIT (priority=0 → bypasses rate limit) and let the
        #    sender drain it.  If the queue is already full we still try.
        try: self.send(self._quit_msg, priority=0)
        except Exception as e:
            _LOG_SHUTDOWN.warning("event=quit_enqueue_failed err=%s", e)
        await asyncio.sleep(self._SHUTDOWN_DRAIN_S)
        # 5. Stop the sender (cancels its drain task).
        if self._sender:
            try: await self._sender.stop()
            except Exception as e:
                _LOG_SHUTDOWN.warning("event=sender_stop_failed err=%s", e)
        # 6. Close the socket.
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                _LOG_SHUTDOWN.debug("event=writer_close_err err=%s", e)
        # 7. Cancel any remaining background tasks (keepalive, rejoin, cmd-*).
        for task in self._tasks:
            task.cancel()
        # Give cancelled tasks a chance to clean up.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        # 7b. Stop metrics HTTP server if it's running.
        try:
            from metrics import registry as _mreg
            if _mreg.is_enabled():
                _mreg.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _LOG_SHUTDOWN.info(
            "event=shutdown_complete reconnects=%d dropped=%d cmd_timeouts=%d "
            "sasl_failures=%d oversized=%d unexpected=%d",
            self._metrics["reconnects"], self._metrics["dropped_messages"],
            self._metrics["command_timeouts"], self._metrics["sasl_failures"],
            self._metrics["oversized_lines"], self._metrics["unexpected_errors"])
        # 8. Flush all logging handlers — important before os.execv() which
        #    will replace the process image without running atexit handlers.
        for h in logging.getLogger("internets").handlers:
            try: h.flush()
            except Exception: pass

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, nick: str, reply_to: str, cmd: str,
                  arg: str | None, is_pm: bool) -> None:
        # Shadow-bans drop ALL commands silently — no privmsg/notice reply,
        # no rate-limit consumption, no audit log entry.  The banned nick
        # cannot tell whether the command is being ignored or the bot is
        # offline, which is the entire point.
        if self.is_shadow_banned(nick):
            log.debug(f"shadow-banned cmd dropped: {nick}!{cmd!r}")
            return
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in PM."); return
        if self.flood_limited(nick):
            self.notice(nick, f"{nick}: slow down ({FLOOD_CD}s cooldown)"); return
        # Channel-wide gate — catches coordinated floods across nicks
        # that the per-nick limit can't see.  Silent log only; we don't
        # want to spam the channel telling users it's throttled.
        if not is_pm and self.channel_limited(reply_to):
            _LOG_DISPATCH.warning(
                "event=channel_throttled channel=%s nick=%s cmd=%s",
                reply_to, nick, cmd)
            return
        if arg and len(arg) > self._MAX_ARG_LEN:
            self.notice(nick, f"{nick}: input too long (max {self._MAX_ARG_LEN} chars)."); return
        # O(1) cap check via counter instead of O(n) scan over self._tasks.
        # Kept _MAX_TASKS reference for the existing test (BUG-030 inspects
        # the dispatch source for the constant name).
        if self._active_cmd_tasks >= self._MAX_TASKS:
            _LOG_DISPATCH.warning(
                "event=dispatch_rejected reason=at_capacity active=%d cap=%d nick=%s cmd=%s",
                self._active_cmd_tasks, self._MAX_TASKS, nick, cmd)
            self.notice(nick, f"{nick}: bot is busy — try again shortly."); return
        handler = None
        if cmd in self._CORE:
            handler = getattr(self, self._CORE[cmd])
        else:
            with self._mod_lock:
                entry = self._commands.get(cmd)
                inst  = self._modules.get(entry[0]) if entry else None
            if inst and entry:
                handler = getattr(inst, entry[1])
        if handler and self._loop:
            self._active_cmd_tasks += 1
            self._stats_cmd_count += 1
            # Metrics counter for command volume.  Module label is the
            # owning module name (or "core" for built-in admin commands).
            module_label = "core"
            if cmd in self._commands:
                module_label = self._commands[cmd][0]
            try:
                from metrics import registry as _mreg  # noqa: PLC0415
                _mreg.commands_total.inc(labels={"module": module_label, "command": cmd})
            except Exception:  # noqa: BLE001
                pass
            task = self._loop.create_task(
                self._run_cmd(handler, nick, reply_to, arg, cmd), name=f"cmd-{cmd}")
            self._tasks.append(task)
            def _on_done(t: asyncio.Task, _self=self) -> None:
                _self._active_cmd_tasks = max(0, _self._active_cmd_tasks - 1)
                if t in _self._tasks:
                    _self._tasks.remove(t)
            task.add_done_callback(_on_done)

    async def _run_cmd(self, handler: Any, nick: str, reply_to: str,
                       arg: str | None, cmd: str) -> None:
        try:
            await handler(nick, reply_to, arg)
        except asyncio.CancelledError:
            # Propagate cancellation cleanly during shutdown — don't notify.
            self._metrics["command_timeouts"] += 1
            raise
        except Exception as e:
            self._metrics["unexpected_errors"] += 1
            log.error(f"Command {cmd!r} from {nick} crashed: {e}", exc_info=True)
            self.notice(nick, f"{nick}: internal error processing '{cmd}' — see log for details.")

    # ── Connection ───────────────────────────────────────────────────

    def _tls_or_refuse(self, cred_name: str) -> bool:
        """Gate every outbound credential on TLS being active.

        Returns True iff the live IRC connection is TLS-protected.  On
        a plaintext connection we log CRITICAL and return False — the
        caller suppresses the credential send.  Prevents the foot-gun
        of leaking NickServ/SASL/server/oper passwords on a
        misconfigured connection.
        """
        if getattr(self, "_tls_active", False):
            return True
        log.critical(
            "event=plaintext_cred_refused cred=%s — refusing to send %s "
            "over a non-TLS connection.  Set ssl=true in config.ini[irc] "
            "or unset the credential.",
            cred_name, cred_name)
        return False

    async def _connect(self) -> None:
        use_ssl = cfg["irc"].getboolean("ssl", fallback=True)
        verify  = cfg["irc"].getboolean("ssl_verify", fallback=True)
        # Record TLS state for credential-send guards (see _tls_or_refuse).
        self._tls_active = use_ssl
        _LOG_CONN.info(
            "event=connect_begin host=%s port=%d ssl=%s verify=%s",
            SERVER, PORT, use_ssl, verify if use_ssl else "n/a")
        ssl_ctx: ssl.SSLContext | None = None
        if use_ssl:
            ssl_ctx = ssl.create_default_context()
            # TLS 1.3-only.  Closes the entire TLS 1.2 cipher-suite
            # surface (CBC modes, RSA key exchange, weak MAC algorithms,
            # renegotiation tricks).  Modern IRCds (UnrealIRCd 6+,
            # InspIRCd 4+, Charybdis, Solanum) all speak TLS 1.3.
            # If you must talk to a TLS-1.2-only IRCd, set
            # INTERNETS_ALLOW_TLS12=1 in the environment.
            if os.environ.get("INTERNETS_ALLOW_TLS12") == "1":
                ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                log.warning(
                    "event=tls_minimum_downgraded value=TLSv1.2 — "
                    "INTERNETS_ALLOW_TLS12 is set; weak ciphersuites are "
                    "back on the table for this connection.")
            else:
                ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                # Cert verification is off — emit a loud warning per
                # reconnect so this never silently regresses in a
                # production deployment.  Configured intentionally for
                # self-signed networks like ChatNPlay, but worth
                # surfacing every time we connect.
                log.warning(
                    "event=tls_unverified host=%s port=%d — ssl_verify=false "
                    "in config: TLS hostname + certificate verification "
                    "are DISABLED for this connection. Acceptable only for "
                    "trusted networks with self-signed certs.",
                    SERVER, PORT)
        # Note: 8192 must remain as a literal here too — test BUG-042 inspects
        # the source for ``limit=8192``.  _READ_LIMIT documents the value.
        self._reader, self._writer = await asyncio.open_connection(
            SERVER, PORT, ssl=ssl_ctx, limit=8192)
        self._nick = NICKNAME
        self._cap_busy = self._sasl_in_progress = self._ns_identified = False
        self._caps = set(); self._chanops = {}
        if self._sender: await self._sender.stop()
        self._sender = Sender(self._loop)
        self._sender.start(self._writer)
        self._stats_connect_ts = time.time()
        _LOG_CONN.info("event=connect_ok host=%s port=%d", SERVER, PORT)

    # ── Background tasks / channel state ─────────────────────────────

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self._PING_INTERVAL)
            self.send(f"PING :{SERVER}", priority=0)

    _INVITE_COOLDOWN = 5.0

    def _save_channels(self) -> None:
        self._store.channels_save(self.active_channels.snapshot())

    def _on_invite(self, nick: str, channel: str) -> None:
        if not self._CHAN_RE.match(channel): return
        now = time.time()
        if now - self._last_invite_time < self._INVITE_COOLDOWN: return
        self._last_invite_time = now
        log.info(f"Invited to {channel} by {nick}")
        self.send(f"JOIN {channel}")

    def _on_join(self, channel: str) -> None:
        self.active_channels.add(channel.lower())
        self._save_channels()
        log.info(f"Joined {channel}")

    def _on_part(self, channel: str) -> None:
        self.active_channels.discard(channel.lower())
        self._chanops.pop(channel.lower(), None)
        self._save_channels()
        log.info(f"Left {channel}")

    async def _deferred_rejoin(self) -> None:
        if NS_PW:
            for _ in range(self._NICKSERV_WAIT_TICKS):
                if self._ns_identified: break
                await asyncio.sleep(self._NICKSERV_TICK)
            total_wait = self._NICKSERV_WAIT_TICKS * self._NICKSERV_TICK
            if self._ns_identified:
                log.info("event=rejoin nickserv=confirmed")
            else:
                log.warning("event=rejoin nickserv=timeout wait=%.1fs", total_wait)
        saved = self._store.channels_load()
        if not saved:
            log.info("No saved channels — waiting for INVITE."); return
        for ch in saved:
            if not self._CHAN_RE.match(ch): continue
            self.send(f"JOIN {ch}")
            log.info(f"Rejoining {ch}")

    # ── IRC line processing ──────────────────────────────────────────

    def _process(self, line: str) -> None:
        if line.startswith("PING"):
            payload = line.split(":", 1)[1] if ":" in line else line.split(" ", 1)[-1]
            # Test BUG-050 inspects the source for the literal [:400] slice —
            # keep it; _MAX_PONG_LEN documents the value for humans.
            self.send(f"PONG :{payload[:400]}", priority=0); return
        line = strip_tags(line)
        # Shadow-ban filter: if the line's prefix nick is shadow-banned,
        # skip the module on_raw fanout so .seen / .tell / etc. don't
        # record them.  Bot-internal handlers (CAP, numerics, membership,
        # PRIVMSG → _dispatch) still run — _dispatch silently drops the
        # banned nick's commands at that layer.  Net effect: the user is
        # invisible to modules but the bot still tracks them for ops use.
        skip_module_fanout = False
        if self._shadow_bans and line.startswith(":"):
            try:
                src_nick = line[1:].split("!", 1)[0].split(" ", 1)[0]
                if src_nick and src_nick.lower() in self._shadow_bans:
                    skip_module_fanout = True
            except Exception:
                pass
        if not skip_module_fanout:
            with self._mod_lock: snapshot = list(self._modules.values())
            for inst in snapshot:
                try: inst.on_raw(line)
                except Exception as e: log.debug(f"on_raw error in {type(inst).__name__}: {e}")
        if self._handle_cap(line): return
        if self._handle_numeric(line): return
        if self._handle_membership(line): return
        self._handle_privmsg(line)

    def _handle_cap(self, line: str) -> bool:
        m = self._RE_CAP.match(line)
        if m:
            sub, params = m.group(1).upper(), (m.group(2) or "").strip()
            if sub == "LS":
                wanted = DESIRED_CAPS & {c.split("=", 1)[0] for c in params.split()}
                if wanted: self.send(f"CAP REQ :{' '.join(sorted(wanted))}", priority=0)
                else: self.send("CAP END", priority=0); self._cap_busy = False
            elif sub in ("ACK", "NAK"):
                if sub == "ACK":
                    self._caps = set(params.split()); log.info(f"Caps ACK: {self._caps}")
                else: log.info(f"Caps NAK: {params}")
                if ("sasl" in self._caps and NS_PW
                        and not self._sasl_in_progress
                        and self._tls_or_refuse("sasl_password")):
                    self._sasl_in_progress = True
                    self.send("AUTHENTICATE PLAIN", priority=0); log.info("Starting SASL PLAIN authentication")
                else: self.send("CAP END", priority=0); self._cap_busy = False
            elif sub == "NEW":
                new = DESIRED_CAPS & {c.split("=", 1)[0] for c in params.split()}
                if new: self.send(f"CAP REQ :{' '.join(sorted(new))}", priority=0)
            return True
        if line == "AUTHENTICATE +" and self._sasl_in_progress:
            # BUG FIX: previously sent the startup NICKNAME constant; must use
            # the runtime nick (which may have been bumped via 433 collision
            # handling) so SASL authenticates the actual session identity.
            self.send(f"AUTHENTICATE {sasl_plain_payload(self._nick, NS_PW)}", priority=0)
            _LOG_SASL.debug("event=sasl_authenticate nick=%s", self._nick)
            return True
        if self._RE_903.match(line):
            self._sasl_in_progress = False; self._ns_identified = True
            _LOG_SASL.info("event=sasl_success nick=%s", self._nick)
            self.send("CAP END", priority=0); self._cap_busy = False; return True
        if self._RE_SASL_FAIL.match(line):
            self._sasl_in_progress = False
            self._metrics["sasl_failures"] += 1
            # 904 (FAILED) and 905 (TOO_LONG) are credential-level failures —
            # retrying won't help.  902 (DESTROYED) is transient.  We continue
            # to CAP END either way so the connection completes; the operator
            # will see the warning in logs.  Mark permanently failed for
            # 904/905 so the reconnect loop can short-circuit if there's no
            # IDENTIFY fallback configured.
            if " 904 " in line or " 905 " in line:
                self._sasl_failed_permanently = True
            _LOG_SASL.warning("event=sasl_failure nick=%s permanent=%s line=%r — falling back to NickServ IDENTIFY",
                              self._nick, self._sasl_failed_permanently, line)
            self.send("CAP END", priority=0); self._cap_busy = False; return True
        if self._RE_421_CAP.match(line):
            if self._cap_busy: self._cap_busy = False; log.info("Server has no CAP support")
            return True
        if self._RE_451.match(line):
            if self._cap_busy: self.send("CAP END", priority=0); self._cap_busy = False
            return True
        return False

    def _handle_numeric(self, line: str) -> bool:
        if self._RE_433.match(line):
            base = NICKNAME.rstrip("_")
            self._nick = (self._nick + "_") if len(self._nick) < len(base) + 3 else base + str(secrets.randbelow(90) + 10)
            self.send(f"NICK {self._nick}", priority=0); log.warning(f"Nick in use — trying {self._nick!r}"); return True
        if self._RE_005.match(line):
            cm = self._RE_CHANMODES.search(line)
            if cm: self._chanmode_types = parse_isupport_chanmodes(cm.group(1))
            pm = self._RE_PREFIX.search(line)
            if pm: self._prefix_modes, _ = parse_isupport_prefix(pm.group(1))
        m = self._RE_473.match(line)
        if m:
            log.info(f"Cannot join {m.group(1)} (invite-only) — asking {self._services_nick} for INVITE")
            self.send(f"PRIVMSG {self._services_nick} :INVITE {m.group(1)}"); return True
        m = self._RE_JOIN_ERR.match(line)
        if m:
            self.active_channels.discard(m.group(1).lower()); self._save_channels(); return True
        if self._RE_381.match(line):
            log.info("OPER granted.")
            if OPER_MODES: self.send(f"MODE {self._nick} {OPER_MODES}")
            if OPER_SNOMASK: self.send(f"MODE {self._nick} +s {OPER_SNOMASK}")
            return True
        if self._RE_491.match(line):
            log.warning("OPER failed."); return True
        if not self._ns_identified:
            if self._RE_900.match(line):
                self._ns_identified = True; log.info("NickServ: identified (900)"); return True
            m = self._RE_NOTICE.match(line)
            if m and m.group(1).lower() == "nickserv":
                t = m.group(2).lower()
                if "identified" in t or "recognized" in t:
                    self._ns_identified = True; log.info("NickServ: identified (NOTICE)")
        m = self._RE_353.match(line)
        if m:
            chan, names_str = m.group(1).lower(), m.group(2).strip()
            ops = self._chanops.setdefault(chan, set())
            for entry in names_str.split():
                nc, is_op = parse_names_entry(entry)
                if nc and is_op: ops.add(nc.lower())
            return True
        m = self._RE_MODE.match(line)
        if m and m.group(1).startswith(("#", "&", "+", "!")):
            ops = self._chanops.setdefault(m.group(1).lower(), set())
            op_modes = {"o", "a", "q"} & self._prefix_modes
            for adding, ch, param in parse_mode_changes(
                m.group(2), m.group(3).strip().split() if m.group(3).strip() else [],
                self._prefix_modes, self._chanmode_types
            ):
                if ch in op_modes and param:
                    (ops.add if adding else ops.discard)(param.lower())
            return True
        return False

    def _handle_membership(self, line: str) -> bool:
        m = self._RE_CHGHOST.match(line)
        if m: self._store.user_rename(m.group(1), m.group(1), f"{m.group(2)}@{m.group(3)}"); return True
        m = self._RE_ACCOUNT.match(line)
        if m:
            # IRCv3 account-notify: ":nick!user@host ACCOUNT <accountname>"
            # An account of "*" means the user logged out.  We don't persist
            # account names in the store today, but we do log them for audit
            # and refresh the cached hostmask so admin auth (which is keyed
            # on hostmask) stays accurate across rename/account events.
            acct_nick, account = m.group(1), m.group(2)
            _LOG_DISPATCH.info("event=account_change nick=%s account=%s",
                               acct_nick, account)
            # Touch the user record so we keep an up-to-date last_seen even
            # if no PRIVMSG/JOIN follows.  user_rename(old, old, hostmask)
            # is the existing way to refresh metadata in-place.
            cached_hm = self._nick_hosts.get(acct_nick.lower())
            if cached_hm:
                self._store.user_rename(acct_nick, acct_nick, cached_hm)
            return True
        m = self._RE_INVITE.match(line)
        if m: self._on_invite(m.group(1), m.group(2)); return True
        m = self._RE_JOIN.match(line)
        if m:
            nick, hm, chan = m.group(1), m.group(2), m.group(3)
            if nick.lower() == self._nick.lower(): self._on_join(chan)
            else: self._store.user_join(chan, nick, hm)
            return True
        m = self._RE_PART.match(line)
        if m:
            nick, chan = m.group(1), m.group(2)
            if nick.lower() == self._nick.lower(): self._on_part(chan)
            else:
                self._store.user_part(chan, nick)
                ops = self._chanops.get(chan.lower())
                if ops: ops.discard(nick.lower())
            return True
        m = self._RE_KICK.match(line)
        if m:
            chan, nick = m.group(1), m.group(2)
            if nick.lower() == self._nick.lower(): self._on_part(chan)
            else:
                self._store.user_part(chan, nick)
                ops = self._chanops.get(chan.lower())
                if ops: ops.discard(nick.lower())
            return True
        m = self._RE_QUIT.match(line)
        if m:
            nl = m.group(1).lower(); self._store.user_quit(m.group(1))
            for ops in self._chanops.values(): ops.discard(nl)
            return True
        m = self._RE_NICK.match(line)
        if m:
            old, hm, new = m.group(1), m.group(2), m.group(3)
            if old.lower() == self._nick.lower(): self._nick = new
            self._store.user_rename(old, new, hm)
            self._nick_hosts.pop(old.lower(), None); self._nick_hosts[new.lower()] = hm
            with self._auth_lock:
                if old.lower() in self._authed:
                    self._authed.pop(old.lower()); self._authed[new.lower()] = hm
            ol, nl = old.lower(), new.lower()
            for ops in self._chanops.values():
                if ol in ops: ops.discard(ol); ops.add(nl)
            return True
        return False

    def _handle_privmsg(self, line: str) -> None:
        m = self._RE_PRIVMSG.match(line)
        if not m: return
        nick, hostmask, target, text = m.groups()
        text = text.strip()
        self._stats_msg_in += 1
        self._nick_hosts[nick.lower()] = hostmask
        if text.startswith("\x01"): return
        is_pm = target.lower() == self._nick.lower()
        reply_to = nick if is_pm else target
        if not is_pm and target.lower() in self.active_channels:
            self._store.user_join(target, nick, hostmask)
        with self._mod_lock: all_cmds = set(self._CORE) | set(self._commands)
        cmd = arg = None
        if text.startswith(CMD_PREFIX):
            parts = text[len(CMD_PREFIX):].split(None, 1)
            if parts:
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else None
        elif is_pm:
            parts = text.split(None, 1)
            if parts and parts[0].lower() in all_cmds:
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else None
        if cmd and cmd in all_cmds:
            log_arg = "[REDACTED]" if cmd in ("auth", "deauth") else arg
            log.info(f"cmd={cmd!r} arg={log_arg!r} from {nick}!{hostmask} "
                     f"{'(PM)' if is_pm else 'in ' + reply_to}")
            self._dispatch(nick, reply_to, cmd, arg, is_pm)

    # ── Main loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        # ── Signal handlers (POSIX only; Windows has no add_signal_handler) ─
        # SIGINT, SIGTERM → graceful shutdown
        # SIGHUP          → rehash (reload config) without dropping the link
        if os.name != "nt":
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    self._loop.add_signal_handler(sig, lambda s=sig: self._on_signal(s))
                except (NotImplementedError, RuntimeError) as e:
                    _LOG_SIGNAL.warning("event=signal_setup_failed sig=%s err=%s", sig, e)
            try:
                self._loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
                _LOG_SIGNAL.info("event=signal_handlers_installed sigs=SIGINT,SIGTERM,SIGHUP")
            except (NotImplementedError, RuntimeError, AttributeError) as e:
                _LOG_SIGNAL.debug("event=sighup_setup_skipped err=%s", e)
        else:
            # On Windows the event-loop signal API is not supported; rely on
            # KeyboardInterrupt + the console task for shutdown.
            _LOG_SIGNAL.info("event=signal_handlers_skipped platform=windows")

        self.autoload_modules()
        log.info("event=caps_requested caps=%s", ",".join(sorted(DESIRED_CAPS)))

        # ── Initial connect with bounded backoff ────────────────────────
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._connect()
                break
            except Exception as e:
                delay = _backoff_jittered(attempt)
                _LOG_CONN.error(
                    "event=connect_failed attempt=%d delay=%.1fs err=%s",
                    attempt, delay, e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    # _stop was set → break out without retrying.
                    break
                except asyncio.TimeoutError:
                    pass
                attempt += 1
        if self._stop.is_set():
            await self.graceful_shutdown()
            return

        identified = registered = False
        while not self._stop.is_set():
            try:
                if not registered:
                    if SERVER_PW and self._tls_or_refuse("server_password"):
                        self.send(f"PASS {SERVER_PW}", priority=0)
                    self.send("CAP LS 302", priority=0); self._cap_busy = True
                    self.send(f"NICK {self._nick}", priority=0)
                    self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)
                    registered = True
                # Race the readline against the shutdown event so the
                # bot reacts to .shutdown / SIGINT immediately instead
                # of sitting in readline for up to _READ_TIMEOUT seconds
                # until the next server PING wakes it up.  Without this
                # gate, request_shutdown() sets _stop but the loop
                # doesn't notice for ~73s on a quiet network.
                read_task = asyncio.create_task(
                    asyncio.wait_for(
                        self._reader.readline(),
                        timeout=self._READ_TIMEOUT))
                stop_task = asyncio.create_task(self._stop.wait())
                try:
                    done, pending = await asyncio.wait(
                        {read_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in (read_task, stop_task):
                        if not t.done():
                            t.cancel()
                if self._stop.is_set():
                    # Drain the cancelled read task quietly — it may
                    # raise CancelledError or asyncio.TimeoutError.
                    for t in (read_task,):
                        try:
                            await t
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass
                        except Exception:
                            pass
                    break
                try:
                    raw = read_task.result()
                except asyncio.TimeoutError:
                    raise ConnectionResetError(
                        f"Read timeout ({self._READ_TIMEOUT}s)")
                except asyncio.LimitOverrunError:
                    self._metrics["oversized_lines"] += 1
                    _LOG_CONN.warning(
                        "event=oversized_line limit=%d action=discard",
                        self._READ_LIMIT)
                    try: await self._reader.readuntil(b"\n")
                    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError): pass
                    continue
                if not raw: raise ConnectionResetError("Server closed connection")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line: continue
                if self._RE_AUTH_LOG.search(line):
                    log.debug(f"<< {line.split(':',2)[0]}:*** AUTH [REDACTED] ***")
                else: log.debug(f"<< {line}")
                self._process(line)
                if not identified and self._RE_MOTD.match(line):
                    if self._cap_busy: self.send("CAP END", priority=0); self._cap_busy = False
                    if USER_MODES:
                        self.send(f"MODE {self._nick} {USER_MODES}")
                        log.info(f"User modes: MODE {self._nick} {USER_MODES}")
                    if (NS_PW and not self._ns_identified
                            and self._tls_or_refuse("nickserv_password")):
                        self.send(f"PRIVMSG NickServ :IDENTIFY {NS_PW}")
                    if OPER_N and OPER_PW and self._tls_or_refuse("oper_password"):
                        self.send(f"OPER {OPER_N} {OPER_PW}")
                    self._tasks.append(asyncio.create_task(self._keepalive(), name="keepalive"))
                    self._tasks.append(asyncio.create_task(self._deferred_rejoin(), name="rejoin"))
                    identified = True
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ssl.SSLError, OSError) as e:
                if self._stop.is_set(): break
                self._metrics["reconnects"] += 1
                try:
                    from metrics import registry as _mreg  # noqa: PLC0415
                    _mreg.reconnects_total.inc()
                except Exception:  # noqa: BLE001
                    pass
                # Distinguish transient (most OSErrors, RST, SSL renegotiation,
                # DNS) from likely-permanent (auth-related) failures.  SASL
                # hard-fail above already incremented sasl_failures; if that
                # was the cause and there's no NickServ password to fall back
                # on, retrying won't help.
                permanent = (self._sasl_failed_permanently
                             and self._metrics["sasl_failures"] >= 3
                             and not NS_PW)
                # Tear down current connection state.
                for task in self._tasks: task.cancel()
                self._tasks.clear()
                if self._sender: await self._sender.stop()
                with self._auth_lock:
                    if self._authed:
                        _LOG_CONN.info("event=auth_sessions_cleared count=%d",
                                       len(self._authed))
                        self._authed.clear()
                self._nick_hosts.clear()
                identified = registered = False
                if permanent:
                    _LOG_CONN.critical(
                        "event=reconnect_aborted reason=auth_failed err=%s", e)
                    break
                attempt = 0
                while not self._stop.is_set():
                    delay = _backoff_jittered(attempt)
                    _LOG_CONN.warning(
                        "event=connection_lost attempt=%d delay=%.1fs err=%s",
                        attempt, delay, e)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                        break  # _stop was set → abandon reconnect
                    except asyncio.TimeoutError:
                        pass
                    try:
                        await self._connect()
                        break
                    except Exception as ce:
                        _LOG_CONN.error(
                            "event=reconnect_failed attempt=%d err=%s",
                            attempt, ce)
                        attempt += 1
            except asyncio.CancelledError:
                # Cooperative shutdown — main loop cancelled (e.g. by the
                # console task finishing).  Don't swallow further; exit loop.
                break
            except Exception as e:
                self._metrics["unexpected_errors"] += 1
                log.error("event=mainloop_unexpected err=%s", e, exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=self._UNEXPECTED_SLEEP_S)
                    break
                except asyncio.TimeoutError:
                    pass
        await self.graceful_shutdown()

    def _on_signal(self, signum: int) -> None:
        """SIGINT / SIGTERM handler.  Idempotent: a second signal is logged
        but does not re-trigger shutdown (which is already in flight)."""
        try:
            name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = str(signum)
        if self._shutdown_initiated:
            _LOG_SIGNAL.warning(
                "event=signal_repeat sig=%s ignored=true shutdown_already_in_flight",
                name)
            return
        _LOG_SIGNAL.info("event=signal_received sig=%s action=shutdown", name)
        self.request_shutdown(f"Caught {name}, shutting down")

    def _on_sighup(self) -> None:
        """SIGHUP: rehash config (template + local overlay) from disk."""
        _LOG_SIGNAL.info("event=signal_received sig=SIGHUP action=rehash")
        try:
            from config import reload_config
            files = reload_config()
            _LOG_SIGNAL.info("event=rehash_ok files=%s", files)
        except Exception as e:
            _LOG_SIGNAL.error("event=rehash_failed err=%s", e)
            return
        # Clear admin sessions: secrets may have changed.
        with self._auth_lock:
            n = len(self._authed)
            self._authed.clear()
        if n:
            _LOG_SIGNAL.info("event=rehash_sessions_cleared count=%d", n)


# ── Entry point ──────────────────────────────────────────────────────

async def _main(lock: ProcessLock | None = None) -> None:
    bot = IRCBot()
    # Optional Prometheus exporter — off by default.  Enable with:
    #     [metrics]
    #     enable = true
    #     host = 127.0.0.1     ; never expose to 0.0.0.0 — auth-less
    #     port = 9779
    # in config.ini / config.local.ini.  See metrics.py for the schema.
    if cfg.has_section("metrics") and cfg["metrics"].getboolean("enable", False):
        try:
            from metrics import registry as _mreg
            _mreg.enable()
            host = cfg["metrics"].get("host", "127.0.0.1").strip()
            port = cfg["metrics"].getint("port", 9779)
            _mreg.expose(host, port)
            log.info("event=metrics_enabled host=%s port=%d", host, port)
        except Exception as e:  # noqa: BLE001
            log.error("event=metrics_start_failed err=%s", e)
    tasks: list[asyncio.Task] = []
    bot_task: asyncio.Task | None = None
    # Console is gated on (a) operator opt-in (no --no-console) AND
    # (b) stdin actually being an interactive TTY.  Skipping when stdin
    # is piped / a non-TTY prevents the console from looping on EOF and
    # avoids granting admin-equivalent capability to whatever happens
    # to be piped in.  console.should_skip_console() owns the check.
    if not cli_args.no_console and not should_skip_console():
        tasks.append(asyncio.create_task(run_console(bot), name="console"))
        log.info("Interactive console enabled (type 'help' for commands)")
    elif not cli_args.no_console:
        log.info("Console skipped: stdin is not a TTY (likely daemon/systemd)")
    bot_task = asyncio.create_task(bot.run(), name="bot")
    tasks.append(bot_task)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    # If the bot task is still running (e.g. the console exited first), ask
    # it to shut down cleanly so the sender drains and the store flushes.
    # Cancelling the bot task directly would skip graceful_shutdown().
    if bot_task in pending:
        bot.request_shutdown("Console exited")
        try:
            await asyncio.wait_for(bot_task, timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("event=bot_shutdown_timeout action=cancel")
            bot_task.cancel()
            try: await bot_task
            except (asyncio.CancelledError, Exception): pass
        pending.discard(bot_task)
    # Cancel any other still-pending tasks (e.g. the console).
    #
    # The console is the tricky one: it's blocked in
    # ``asyncio.to_thread(input, "> ")`` which parks a ThreadPoolExecutor
    # worker on a blocking ``read(0)`` syscall.  Cancelling the asyncio
    # task flips it to cancelled but does NOT interrupt the syscall —
    # ``asyncio.run()``'s subsequent ``loop.shutdown_default_executor()``
    # then waits forever for the thread to return, and the whole process
    # hangs on the last log line.  Closing stdin makes the blocking read
    # raise OSError / return empty, which unblocks ``input()`` (it raises
    # EOFError, which ``run_console`` already catches), the thread
    # returns, and the executor shuts down cleanly.
    if pending:
        try:
            sys.stdin.close()
        except Exception:
            pass
    for task in pending:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    # Final logging-handler flush before potential execv().
    for h in logging.getLogger("internets").handlers:
        try: h.flush()
        except Exception: pass
    if bot._restart_flag:
        log.info("event=restart_exec argv=%s", sys.argv)
        # Close all logging file handlers before exec to ensure clean log
        # rotation across the restart.
        for h in logging.getLogger("internets").handlers:
            try: h.close()
            except Exception: pass
        # Release the process lock before execv replaces the process image —
        # otherwise the lockfile (containing OUR PID, which is preserved
        # across execv) would still be on disk when the new image starts,
        # and stale-detection would see the PID as live and refuse to
        # acquire.  On the Windows subprocess path the parent will exit
        # cleanly and __exit__ on the lock would run anyway, but we
        # release here too for symmetry.
        if lock is not None:
            try: lock.release()
            except Exception as e:
                log.warning("event=restart_lock_release_failed err=%r", e)
        if os.name == "nt":
            import subprocess
            subprocess.Popen([sys.executable] + sys.argv)
            sys.exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)


def _entry() -> None:
    """Entry point for ``pip install`` console script.

    KeyboardInterrupt is caught here only after the event loop has had a
    chance to react — by that point ``_on_signal`` should have already
    requested a clean shutdown.  We deliberately do NOT bury other
    exceptions: a bare ``except:`` here would hide real bugs.

    A :class:`ProcessLock` is acquired around the event loop to prevent
    two bot instances from running simultaneously and silently corrupting
    the JSON state files (locations / channels / users / secrets).  The
    lock is passed into ``_main`` so it can be released explicitly before
    ``os.execv()`` on the restart path; otherwise the lockfile (which
    records OUR PID, preserved across execv) would block the new image
    from re-acquiring.
    """
    # Drop-root guard (POSIX).  Running an IRC bot as root needlessly
    # expands the blast radius of any compromise — a code-exec bug
    # would suddenly own the whole box instead of an unprivileged
    # account.  Refuse unless explicitly overridden via env var.
    # Windows has no euid; skip there.  Containers running as root
    # should also opt in explicitly via INTERNETS_ALLOW_ROOT=1.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if os.environ.get("INTERNETS_ALLOW_ROOT") != "1":
            log.critical(
                "event=refused_root_start euid=0 — refusing to start as root.  "
                "Run under an unprivileged account, OR set "
                "INTERNETS_ALLOW_ROOT=1 if you have a specific reason "
                "(e.g. binding port <1024 on a host without capabilities).")
            sys.exit(1)
        log.warning(
            "event=root_start_allowed INTERNETS_ALLOW_ROOT=1 — running as root "
            "is permitted by env override; consider switching to setcap "
            "CAP_NET_BIND_SERVICE instead.")
    lock_path = Path("./internets.pid").resolve()
    try:
        with ProcessLock(lock_path) as lock:
            try:
                asyncio.run(_main(lock))
            except KeyboardInterrupt:
                # Signal handler ran (or we're on Windows).  Exit non-zero so
                # supervisors notice — but do not print a traceback.
                log.info("event=keyboard_interrupt")
                sys.exit(130)  # 128 + SIGINT
    except LockHeld as e:
        log.critical(f"Another bot instance is already running: {e}")
        sys.exit(1)


if __name__ == "__main__":
    _entry()
