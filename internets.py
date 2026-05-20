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
import secrets
import signal
import threading
import logging
import importlib
import importlib.util
import time
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
from console import run_console
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
    return min(base * (2 ** attempt), cap)


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
        "mode": "cmd_mode", "snomask": "cmd_snomask",
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

    # ── Outbound messaging ───────────────────────────────────────────

    def send(self, msg: str, priority: int = 1) -> None:
        if self._sender: self._sender.enqueue(msg, priority)

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
                log.info(f"Loaded {name} ({list(inst.COMMANDS)})")
                return True, f"'{name}' loaded ({len(inst.COMMANDS)} commands)."
            except Exception as e:
                log.error(f"Load '{name}': {e}")
                return False, f"Error loading '{name}' — see log for details."

    def unload_module(self, name: str) -> tuple[bool, str]:
        with self._mod_lock:
            if name not in self._modules:
                return False, f"'{name}' not loaded."
            try:
                self._modules[name].on_unload()
                for cmd in [c for c, v in self._commands.items() if v[0] == name]:
                    del self._commands[cmd]
                del self._modules[name]
                log.info(f"Unloaded {name}")
                return True, f"'{name}' unloaded."
            except Exception as e:
                log.error(f"Unload '{name}': {e}")
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
        self._quit_msg = f"QUIT :{reason}"
        if self._stop and self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def graceful_shutdown(self) -> None:
        log.info("Graceful shutdown initiated.")
        try: self._save_channels()
        except Exception as e: log.warning(f"Channel save failed: {e}")
        with self._mod_lock: names = list(self._modules)
        for name in names:
            try: self.unload_module(name)
            except Exception as e: log.warning(f"Unload {name} failed: {e}")
        try: self._store.stop(); log.info("Store flushed to disk.")
        except Exception as e: log.warning(f"Store flush failed: {e}")
        try: self.send(self._quit_msg, priority=0)
        except Exception: pass
        await asyncio.sleep(2)
        if self._sender: await self._sender.stop()
        if self._writer:
            try: self._writer.close(); await self._writer.wait_closed()
            except Exception: pass
        for task in self._tasks: task.cancel()
        log.info("Shutdown complete.")

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, nick: str, reply_to: str, cmd: str,
                  arg: str | None, is_pm: bool) -> None:
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in PM."); return
        if self.flood_limited(nick):
            self.notice(nick, f"{nick}: slow down ({FLOOD_CD}s cooldown)"); return
        if arg and len(arg) > self._MAX_ARG_LEN:
            self.notice(nick, f"{nick}: input too long (max {self._MAX_ARG_LEN} chars)."); return
        active = sum(1 for t in self._tasks if not t.done() and (t.get_name() or "").startswith("cmd-"))
        if active >= self._MAX_TASKS:
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
            task = self._loop.create_task(
                self._run_cmd(handler, nick, reply_to, arg, cmd), name=f"cmd-{cmd}")
            self._tasks.append(task)
            task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    async def _run_cmd(self, handler: Any, nick: str, reply_to: str,
                       arg: str | None, cmd: str) -> None:
        try:
            await handler(nick, reply_to, arg)
        except Exception as e:
            log.error(f"Command {cmd!r} from {nick} crashed: {e}", exc_info=True)
            self.notice(nick, f"{nick}: internal error processing '{cmd}' — see log for details.")

    # ── Connection ───────────────────────────────────────────────────

    async def _connect(self) -> None:
        use_ssl = cfg["irc"].getboolean("ssl", fallback=True)
        verify  = cfg["irc"].getboolean("ssl_verify", fallback=True)
        log.info(f"Connecting {SERVER}:{PORT} ({'SSL' if use_ssl else 'plain'}"
                 f"{', no verify' if use_ssl and not verify else ''})")
        ssl_ctx: ssl.SSLContext | None = None
        if use_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        self._reader, self._writer = await asyncio.open_connection(SERVER, PORT, ssl=ssl_ctx, limit=8192)
        self._nick = NICKNAME
        self._cap_busy = self._sasl_in_progress = self._ns_identified = False
        self._caps = set(); self._chanops = {}
        if self._sender: await self._sender.stop()
        self._sender = Sender(self._loop)
        self._sender.start(self._writer)

    # ── Background tasks / channel state ─────────────────────────────

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(90)
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
            for _ in range(40):
                if self._ns_identified: break
                await asyncio.sleep(0.25)
            if self._ns_identified:
                log.info("NickServ confirmed — rejoining channels.")
            else:
                log.warning("NickServ did not confirm within 10s — rejoining anyway.")
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
            self.send(f"PONG :{payload[:400]}", priority=0); return
        line = strip_tags(line)
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
                if "sasl" in self._caps and NS_PW and not self._sasl_in_progress:
                    self._sasl_in_progress = True
                    self.send("AUTHENTICATE PLAIN", priority=0); log.info("Starting SASL PLAIN authentication")
                else: self.send("CAP END", priority=0); self._cap_busy = False
            elif sub == "NEW":
                new = DESIRED_CAPS & {c.split("=", 1)[0] for c in params.split()}
                if new: self.send(f"CAP REQ :{' '.join(sorted(new))}", priority=0)
            return True
        if line == "AUTHENTICATE +" and self._sasl_in_progress:
            self.send(f"AUTHENTICATE {sasl_plain_payload(NICKNAME, NS_PW)}", priority=0); return True
        if self._RE_903.match(line):
            self._sasl_in_progress = False; self._ns_identified = True
            log.info("SASL authentication successful"); self.send("CAP END", priority=0); self._cap_busy = False; return True
        if self._RE_SASL_FAIL.match(line):
            self._sasl_in_progress = False
            log.warning("SASL authentication failed — will fall back to NickServ IDENTIFY")
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
        if self._RE_ACCOUNT.match(line): return True
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
        for sig in (signal.SIGTERM, signal.SIGINT):
            try: self._loop.add_signal_handler(sig, lambda s=sig: self._on_signal(s))
            except NotImplementedError: pass
        self.autoload_modules()
        log.info(f"Desired caps: {', '.join(sorted(DESIRED_CAPS))}")
        attempt = 0
        while True:
            try: await self._connect(); break
            except Exception as e:
                delay = _backoff(attempt)
                log.error(f"Connect failed: {e} — retry in {delay:.0f}s")
                await asyncio.sleep(delay); attempt += 1
        identified = registered = False
        while not self._stop.is_set():
            try:
                if not registered:
                    if SERVER_PW: self.send(f"PASS {SERVER_PW}", priority=0)
                    self.send("CAP LS 302", priority=0); self._cap_busy = True
                    self.send(f"NICK {self._nick}", priority=0)
                    self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)
                    registered = True
                try: raw = await asyncio.wait_for(self._reader.readline(), timeout=300)
                except asyncio.TimeoutError: raise ConnectionResetError("Read timeout (300s)")
                except asyncio.LimitOverrunError:
                    log.warning("Oversized IRC line (>8KB) — discarding")
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
                    if NS_PW and not self._ns_identified: self.send(f"PRIVMSG NickServ :IDENTIFY {NS_PW}")
                    if OPER_N and OPER_PW: self.send(f"OPER {OPER_N} {OPER_PW}")
                    self._tasks.append(asyncio.create_task(self._keepalive(), name="keepalive"))
                    self._tasks.append(asyncio.create_task(self._deferred_rejoin(), name="rejoin"))
                    identified = True
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ssl.SSLError, OSError) as e:
                if self._stop.is_set(): break
                for task in self._tasks: task.cancel()
                self._tasks.clear()
                if self._sender: await self._sender.stop()
                with self._auth_lock:
                    if self._authed: log.info(f"Cleared {len(self._authed)} admin session(s)."); self._authed.clear()
                self._nick_hosts.clear(); identified = registered = False
                attempt = 0
                while not self._stop.is_set():
                    delay = _backoff(attempt)
                    log.warning(f"Lost connection: {e} — reconnect in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    try: await self._connect(); break
                    except Exception as ce: log.error(f"Reconnect failed: {ce}"); attempt += 1
            except asyncio.CancelledError: break
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)
        await self.graceful_shutdown()

    def _on_signal(self, signum: int) -> None:
        log.info(f"Received signal {signum}, shutting down.")
        self._quit_msg = "QUIT :Caught signal, shutting down"
        if self._stop: self._stop.set()


# ── Entry point ──────────────────────────────────────────────────────

async def _main() -> None:
    bot = IRCBot()
    tasks: list[asyncio.Task] = []
    if not cli_args.no_console and sys.stdin.isatty():
        tasks.append(asyncio.create_task(run_console(bot), name="console"))
        log.info("Interactive console enabled (type 'help' for commands)")
    tasks.append(asyncio.create_task(bot.run(), name="bot"))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    if bot._restart_flag:
        log.info("Executing restart ...")
        if os.name == "nt":
            import subprocess; subprocess.Popen([sys.executable] + sys.argv); sys.exit(0)
        else: os.execv(sys.executable, [sys.executable] + sys.argv)


def _entry() -> None:
    """Entry point for ``pip install`` console script."""
    try: asyncio.run(_main())
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    _entry()
