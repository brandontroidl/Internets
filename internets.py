#!/usr/bin/env python3
"""
Internets — async modular IRC bot.

Architecture: asyncio event loop for connection, dispatch, and background
tasks.  Module command handlers are coroutines.  Blocking I/O (HTTP via
requests, disk, CPU-heavy work like password hashing) runs via
asyncio.to_thread() inside the handler.

Core commands: .help .modules .auth .deauth
               .load .unload .reload .reloadall .restart .rehash

Modules live in modules/. Each exposes setup(bot) -> BotModule.
See modules/base.py for the interface.
"""

from __future__ import annotations

import asyncio
import ssl
import re
import sys
import os
import time
import base64
import signal
import threading
import logging
import logging.handlers
import configparser
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Optional

from store    import Store, RateLimiter
from sender   import Sender
from hashpw   import verify_password
from protocol import (
    strip_tags,
    parse_isupport_chanmodes,
    parse_isupport_prefix,
    parse_mode_changes,
    parse_names_entry,
    sasl_plain_payload,
)

cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
cfg.read("config.ini")

SERVER    = cfg["irc"]["server"]
PORT      = int(cfg["irc"]["port"])
NICKNAME  = cfg["irc"]["nickname"]
REALNAME  = cfg["irc"]["realname"]
NS_PW     = cfg["irc"].get("nickserv_password", "").strip()
SERVER_PW = cfg["irc"].get("server_password",   "").strip()
OPER_N    = cfg["irc"].get("oper_name",          "").strip()
OPER_PW   = cfg["irc"].get("oper_password",      "").strip()
USER_MODES = cfg["irc"].get("user_modes",         "").strip()
OPER_MODES = cfg["irc"].get("oper_modes",         "").strip()
OPER_SNOMASK = cfg["irc"].get("oper_snomask",     "").strip()

CMD_PREFIX  = cfg["bot"]["command_prefix"]
API_CD      = int(cfg["bot"]["api_cooldown"])
FLOOD_CD    = int(cfg["bot"].get("flood_cooldown", "3"))
MODULES_DIR = Path(cfg["bot"].get("modules_dir", "modules"))
AUTO_LOAD   = [m.strip() for m in cfg["bot"].get("autoload", "").split(",") if m.strip()]

# All optional — the bot works fine if the server supports none of these.
DESIRED_CAPS: set[str] = {
    "multi-prefix", "away-notify", "account-notify", "chghost",
    "extended-join", "server-time", "message-tags", "sasl",
}


class ChannelSet:
    """Thread-safe set of active channel names (lowercased).

    Thread safety is required because module command handlers run in
    asyncio.to_thread (thread pool), but state mutations happen in
    the event loop thread.
    """

    def __init__(self) -> None:
        self._channels: set[str] = set()
        self._lock = threading.Lock()

    def add(self, ch: str) -> None:
        with self._lock:
            self._channels.add(ch.lower())

    def discard(self, ch: str) -> None:
        with self._lock:
            self._channels.discard(ch.lower())

    def snapshot(self) -> set[str]:
        with self._lock:
            return set(self._channels)

    def __contains__(self, ch: str) -> bool:
        with self._lock:
            return ch.lower() in self._channels

    def __iter__(self):
        return iter(self.snapshot())

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._channels)

    def __len__(self) -> int:
        with self._lock:
            return len(self._channels)


def _backoff(attempt: int, base: float = 15.0, cap: float = 300.0) -> float:
    """Exponential backoff: 15, 30, 60, 120, 240, 300 (capped at 5 min)."""
    return min(base * (2 ** attempt), cap)


# ── CLI ──────────────────────────────────────────────────────────────

import argparse

_cli = argparse.ArgumentParser(
    description="Internets — async modular IRC bot",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""\
debug examples:
  %(prog)s --debug                   global debug (all subsystems)
  %(prog)s --debug weather store     debug only weather + store
  %(prog)s --loglevel WARNING        suppress INFO from console + main log
  %(prog)s --debug-file debug.log    capture all DEBUG to separate file
  %(prog)s --no-console              disable stdin command loop (for daemons)

interactive console (type 'help' at the > prompt while running):
  debug, loglevel, status, shutdown""")
_cli.add_argument("--debug", nargs="*", metavar="SUBSYSTEM", default=None,
                   help="enable debug output.  No args = global debug.  "
                        "With args = per-subsystem (e.g. --debug weather store)")
_cli.add_argument("--loglevel", metavar="LEVEL", default=None,
                   help="base log level: DEBUG, INFO, WARNING, ERROR")
_cli.add_argument("--debug-file", metavar="PATH", default=None,
                   help="write all DEBUG output to this file (overrides config)")
_cli.add_argument("--no-console", action="store_true", default=False,
                   help="disable interactive stdin console (for daemonized use)")
_args = _cli.parse_args()

# CLI overrides applied before logging setup.
LOG_LEVEL   = (_args.loglevel or cfg["logging"]["level"]).upper()
LOG_FILE    = cfg["logging"]["log_file"]
LOG_MAX     = int(cfg["logging"].get("max_bytes",    "5242880"))  # 5 MB default
LOG_BACKUPS = int(cfg["logging"].get("backup_count", "3"))
LOG_DEBUG   = _args.debug_file or cfg["logging"].get("debug_file", "").strip()
LOG_FMT     = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class _DebugFilter(logging.Filter):
    """
    Attached to main-log and console handlers.  Passes a record if:
      - record level >= self.base_level (normal output), OR
      - global_debug is True (.debug on), OR
      - record's logger name is in the subsystem debug set (.debug weather)
    """

    def __init__(self, base_level: int = logging.INFO) -> None:
        super().__init__()
        self.base_level: int  = base_level
        self.global_debug: bool = False
        self._subsystems: set[str] = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= self.base_level:
            return True
        if self.global_debug:
            return True
        with self._lock:
            name = record.name
            for sub in self._subsystems:
                if name == sub or name.startswith(sub + "."):
                    return True
        return False

    def set_base_level(self, level: int) -> None:
        self.base_level = level

    def add_subsystem(self, name: str) -> None:
        with self._lock:
            self._subsystems.add(name)

    def remove_subsystem(self, name: str) -> None:
        with self._lock:
            self._subsystems.discard(name)

    def clear_subsystems(self) -> None:
        with self._lock:
            self._subsystems.clear()

    def active_subsystems(self) -> set[str]:
        with self._lock:
            return set(self._subsystems)


def _setup_logging() -> _DebugFilter:
    """Configure the internets logger with rotating file + console + optional debug file."""
    root = logging.getLogger("internets")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fmt  = logging.Formatter(LOG_FMT)
    filt = _DebugFilter(getattr(logging, LOG_LEVEL, logging.INFO))

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX, backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(filt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    ch.addFilter(filt)
    root.addHandler(ch)

    if LOG_DEBUG:
        dh = logging.handlers.RotatingFileHandler(
            LOG_DEBUG, maxBytes=LOG_MAX, backupCount=LOG_BACKUPS, encoding="utf-8")
        dh.setLevel(logging.DEBUG)
        dh.setFormatter(fmt)
        dh._debug_file = True
        root.addHandler(dh)

    return filt


_log_filter = _setup_logging()
log = logging.getLogger("internets")

if _args.debug is not None:
    if len(_args.debug) == 0:
        _log_filter.global_debug = True
        log.info("CLI: global debug enabled")
    else:
        for sub in _args.debug:
            full = f"internets.{sub}" if not sub.startswith("internets") else sub
            logging.getLogger(full).setLevel(logging.DEBUG)
            _log_filter.add_subsystem(full)
            log.info(f"CLI: debug enabled for {full}")


def _get_hash() -> str:
    cfg.read("config.ini")
    return cfg["admin"].get("password_hash", "").strip()


def _validate_hash() -> None:
    h = _get_hash()
    if not h:
        log.warning("No password_hash in config.ini — auth disabled. Run hashpw.py.")
        return
    prefix = h.split("$")[0] if "$" in h else ""
    if prefix not in ("scrypt", "bcrypt", "argon2"):
        log.critical(f"Invalid password_hash prefix '{prefix}' — run hashpw.py and restart.")
        sys.exit(1)
    log.info(f"Admin password hash loaded ({prefix}).")


_validate_hash()

_MODE_VALID = re.compile(r"^[a-zA-Z+\- ]*$")
for _name, _val in [("user_modes", USER_MODES), ("oper_modes", OPER_MODES),
                     ("oper_snomask", OPER_SNOMASK)]:
    if _val and not _MODE_VALID.match(_val):
        log.critical(f"Invalid {_name} = {_val!r} in config.ini — "
                     f"only letters, +, -, and spaces allowed.")
        sys.exit(1)
    if _val:
        log.info(f"Config {_name} = {_val}")


# ═════════════════════════════════════════════════════════════════════
# IRCBot
# ═════════════════════════════════════════════════════════════════════

class IRCBot:
    _MAX_BODY = 400

    _CORE: dict[str, str] = {
        "help":      "cmd_help",
        "modules":   "cmd_modules",
        "auth":      "cmd_auth",
        "deauth":    "cmd_deauth",
        "load":      "cmd_load",
        "unload":    "cmd_unload",
        "reload":    "cmd_reload",
        "reloadall": "cmd_reloadall",
        "restart":   "cmd_restart",
        "rehash":    "cmd_rehash",
        "mode":      "cmd_mode",
        "snomask":   "cmd_snomask",
        "shutdown":  "cmd_shutdown",
        "die":       "cmd_shutdown",
        "loglevel":  "cmd_loglevel",
        "debug":     "cmd_debug",
    }

    def __init__(self) -> None:
        self.cfg               = cfg
        self.active_channels: ChannelSet = ChannelSet()
        self._modules: dict[str, Any]  = {}
        self._commands: dict[str, tuple[str, str]] = {}
        self._mod_lock       = threading.Lock()
        self._authed: set[str] = set()
        self._auth_fails: dict[str, tuple[int, float]] = {}
        self._AUTH_MAX_FAILS: int = 5
        self._AUTH_LOCKOUT: int   = 300
        self._nick: str    = NICKNAME
        self._chanops: dict[str, set[str]] = {}
        self._ns_identified = False
        self._sasl_in_progress = False
        self._cap_busy = False
        self._caps: set[str] = set()
        self._services_nick = cfg["bot"].get("services_nick", "ChanServ").strip()
        self._chanmode_types: dict[str, str] = {
            "b": "A", "e": "A", "I": "A",
            "k": "B",
            "l": "C",
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

        # Set once run() starts.
        self._loop:    asyncio.AbstractEventLoop | None = None
        self._sender:  Sender | None = None
        self._reader:  asyncio.StreamReader | None = None
        self._writer:  asyncio.StreamWriter | None = None
        self._stop:    asyncio.Event | None = None
        self._quit_msg = "QUIT :Shutting down"
        self._restart_flag = False
        self._tasks: list[asyncio.Task] = []

    # ── Outbound messaging (sync, thread-safe via Sender) ────────────

    def send(self, msg: str, priority: int = 1) -> None:
        if self._sender:
            self._sender.enqueue(msg, priority)

    def privmsg(self, target: str, msg: str) -> None:
        for chunk in self._split_msg(msg):
            self.send(f"PRIVMSG {target} :{chunk}")

    def notice(self, target: str, msg: str) -> None:
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

    # ── Accessors (sync, called from module threads) ─────────────────

    def is_admin(self, nick: str) -> bool:       return nick in self._authed
    def is_chanop(self, channel: str, nick: str) -> bool:
        return nick.lower() in self._chanops.get(channel.lower(), set())
    def flood_limited(self, nick: str) -> bool:  return self._rate.flood_check(nick, self.is_admin(nick))
    def rate_limited(self, nick: str) -> bool:   return self._rate.api_check(nick)
    def loc_get(self, nick: str) -> str | None:  return self._store.loc_get(nick)
    def loc_set(self, nick: str, raw: str) -> None:  self._store.loc_set(nick, raw)
    def loc_del(self, nick: str) -> bool:        return self._store.loc_del(nick)
    def channel_users(self, ch: str) -> dict[str, Any]:  return self._store.channel_users(ch)

    # ── Module management ────────────────────────────────────────────

    def load_module(self, name: str) -> tuple[bool, str]:
        with self._mod_lock:
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                return False, f"Invalid module name '{name}' — lowercase alphanumeric and _ only."
            if name in self._modules:
                return False, f"'{name}' already loaded."
            path = MODULES_DIR / f"{name}.py"
            if not path.exists():
                return False, f"'{path}' not found."
            try:
                spec = importlib.util.spec_from_file_location(f"modules.{name}", path)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not hasattr(mod, "setup"):
                    return False, f"'{name}' has no setup()."
                inst  = mod.setup(self)
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
                return False, f"Error loading '{name}': {e}"

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
                return False, f"Error unloading '{name}': {e}"

    def reload_module(self, name: str) -> tuple[bool, str]:
        ok, msg = self.unload_module(name)
        return (False, msg) if not ok else self.load_module(name)

    def autoload_modules(self) -> None:
        for name in AUTO_LOAD:
            ok, msg = self.load_module(name)
            (log.info if ok else log.warning)(msg)

    # ── Admin / core commands (async) ──────────────────────────────────

    def _require_admin(self, nick: str, reply_to: str) -> bool:
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: auth first — /MSG {self._nick} AUTH <pw>")
            return False
        return True

    async def cmd_auth(self, nick: str, reply_to: str, arg: str | None) -> None:
        h = _get_hash()
        if not h:
            self.preply(nick, reply_to, f"{nick}: no password_hash configured — run hashpw.py")
            return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: /MSG {self._nick} AUTH <password>")
            return

        k = nick.lower()
        now = time.time()
        if len(self._auth_fails) > 50:
            self._auth_fails = {
                n: (f, t) for n, (f, t) in self._auth_fails.items()
                if now - t < self._AUTH_LOCKOUT
            }
        fails, last_t = self._auth_fails.get(k, (0, 0))
        if now - last_t > self._AUTH_LOCKOUT:
            fails = 0
        if fails >= self._AUTH_MAX_FAILS:
            remaining = int(self._AUTH_LOCKOUT - (now - last_t))
            self.preply(nick, reply_to,
                f"{nick}: too many failed attempts — try again in {remaining}s")
            log.warning(f"Auth lockout: {nick} ({fails} failures)")
            return

        try:
            ok = await asyncio.to_thread(verify_password, arg.strip(), h)
        except ValueError as e:
            self.preply(nick, reply_to, f"{nick}: config error — {e}")
            return
        if ok:
            self._auth_fails.pop(k, None)
            self._authed.add(nick)
            self.preply(nick, reply_to, f"{nick}: authenticated.")
            log.info(f"Auth granted: {nick}")
        else:
            self._auth_fails[k] = (fails + 1, now)
            self.preply(nick, reply_to, f"{nick}: wrong password.")
            log.warning(f"Failed auth: {nick} ({fails + 1}/{self._AUTH_MAX_FAILS})")

    async def cmd_deauth(self, nick: str, reply_to: str, arg: str | None) -> None:
        if nick in self._authed:
            self._authed.discard(nick)
            self.preply(nick, reply_to, f"{nick}: session ended.")
        else:
            self.preply(nick, reply_to, f"{nick}: not authenticated.")

    async def cmd_help(self, nick: str, reply_to: str, arg: str | None) -> None:
        p     = CMD_PREFIX
        lines = [
            f"── {self._nick} ──────────────────────────────────────────────────",
            f"  {p}help  {p}modules  {p}auth <pw>",
        ]
        if self.is_admin(nick):
            lines += [
                f"  {p}deauth  {p}load/unload/reload <mod>  {p}reloadall",
                f"  {p}restart  {p}rehash  {p}mode  {p}snomask      [admin]",
                f"  {p}shutdown [reason]  / {p}die [reason]           [admin]",
                f"  {p}loglevel [LEVEL | <logger> LEVEL]              [admin]",
                f"  {p}debug [on|off|<subsystem> [off]]               [admin]",
            ]
        lines.append("────────────────────────────────────────────────────────────")
        with self._mod_lock:
            module_items = list(self._modules.items())
        for name, inst in module_items:
            hl = inst.help_lines(p)
            if hl:
                lines.append(f"  [{name}]")
                lines.extend(hl)
        lines.append(f"  In PM the '{p}' prefix is optional.")
        for line in lines:
            self.preply(nick, reply_to, line)

    async def cmd_modules(self, nick: str, reply_to: str, arg: str | None) -> None:
        with self._mod_lock:
            loaded = list(self._modules)
        self.preply(nick, reply_to,
            f"Loaded: {', '.join(loaded)}" if loaded else "No modules loaded.")
        avail = sorted(
            p.stem for p in MODULES_DIR.glob("*.py")
            if p.stem not in ("__init__", "base", "geocode", "nws", "units")
            and p.stem not in loaded
        )
        if avail:
            self.preply(nick, reply_to, f"Available: {', '.join(avail)}")

    async def cmd_load(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}load <module>"); return
        _, msg = self.load_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    async def cmd_unload(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}unload <module>"); return
        _, msg = self.unload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    async def cmd_reload(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}reload <module>"); return
        _, msg = self.reload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    async def cmd_reloadall(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        with self._mod_lock:
            names = list(self._modules)
        if not names:
            self.preply(nick, reply_to, "No modules loaded."); return
        self.preply(nick, reply_to, f"Reloading: {', '.join(names)}")
        ok, fail = [], []
        for n in names:
            (ok if self.reload_module(n)[0] else fail).append(n)
        parts = ([f"OK: {', '.join(ok)}"] if ok else []) + \
                ([f"FAILED: {', '.join(fail)}"] if fail else [])
        self.preply(nick, reply_to, " | ".join(parts))

    async def cmd_restart(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        self.preply(nick, reply_to, "Restarting ...")
        log.info(f"Restart by {nick}")
        self._restart_flag = True
        self.request_shutdown("Restarting ...")

    async def cmd_rehash(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        try:
            cfg.read("config.ini")
        except Exception as e:
            self.preply(nick, reply_to, f"Failed to read config.ini: {e}"); return

        new_level = cfg["logging"].get("level", "INFO").upper()
        lvl = getattr(logging, new_level, None)
        if lvl:
            _log_filter.set_base_level(lvl)
            _log_filter.global_debug = False
            _log_filter.clear_subsystems()
            self.preply(nick, reply_to, f"Log level: {new_level}")

        h = _get_hash()
        if not h:
            self.preply(nick, reply_to, "Config reloaded — no password_hash set.")
        else:
            prefix = h.split("$")[0] if "$" in h else ""
            if prefix not in ("scrypt", "bcrypt", "argon2"):
                self.preply(nick, reply_to, f"Bad hash prefix '{prefix}' — run hashpw.py.")
                return
            self.preply(nick, reply_to, f"Config reloaded — {prefix} hash active.")
        n = len(self._authed)
        self._authed.clear()
        if n:
            self.preply(nick, reply_to, f"Cleared {n} admin session(s) — re-authenticate.")
        log.info(f"Rehash by {nick}")

    async def cmd_mode(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}mode <+/-modes>"); return
        mode_str = arg.strip()
        if not re.match(r"^[a-zA-Z+\- ]+$", mode_str):
            self.preply(nick, reply_to, f"{nick}: invalid mode string."); return
        self.send(f"MODE {self._nick} {mode_str}")
        self.preply(nick, reply_to, f"MODE {self._nick} {mode_str}")
        log.info(f"Mode set by {nick}: {mode_str}")

    async def cmd_snomask(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}snomask <+/-flags>"); return
        mask = arg.strip()
        if not re.match(r"^[a-zA-Z+\-]+$", mask):
            self.preply(nick, reply_to, f"{nick}: invalid snomask string."); return
        self.send(f"MODE {self._nick} +s {mask}")
        self.preply(nick, reply_to, f"MODE {self._nick} +s {mask}")
        log.info(f"Snomask set by {nick}: {mask}")

    _VALID_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

    async def cmd_loglevel(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        p = CMD_PREFIX

        if not arg:
            lvl_name = logging.getLevelName(_log_filter.base_level)
            lines = [f"  base level = {lvl_name}"]
            if _log_filter.global_debug:
                lines.append("  global debug = ON")
            active = _log_filter.active_subsystems()
            if active:
                lines.append(f"  debug subsystems: {', '.join(sorted(active))}")
            if LOG_DEBUG:
                lines.append(f"  debug file = {LOG_DEBUG}")
            self.preply(nick, reply_to, "Log levels:")
            for line in lines:
                self.preply(nick, reply_to, line)
            return

        parts = arg.strip().split()
        if len(parts) == 1:
            level = parts[0].upper()
            if level not in self._VALID_LEVELS:
                self.preply(nick, reply_to,
                    f"{nick}: invalid level — use: {', '.join(self._VALID_LEVELS)}")
                return
            _log_filter.set_base_level(getattr(logging, level))
            _log_filter.global_debug = False
            self.preply(nick, reply_to, f"Base level set to {level}")
            log.info(f"Log level set to {level} by {nick}")
        elif len(parts) == 2:
            target, level = parts[0], parts[1].upper()
            if not target.startswith("internets"):
                self.preply(nick, reply_to, f"{nick}: logger must start with 'internets'")
                return
            if level == "DEBUG":
                full = target if "." in target else f"internets.{target}"
                logging.getLogger(full).setLevel(logging.DEBUG)
                _log_filter.add_subsystem(full)
                self.preply(nick, reply_to, f"{full} = DEBUG")
                log.info(f"Log level {full} = DEBUG by {nick}")
            elif level == "NOTSET":
                full = target if "." in target else f"internets.{target}"
                logging.getLogger(full).setLevel(logging.NOTSET)
                _log_filter.remove_subsystem(full)
                self.preply(nick, reply_to, f"{full} = NOTSET (inherits parent)")
                log.info(f"Log level {full} = NOTSET by {nick}")
            elif level in self._VALID_LEVELS:
                logging.getLogger(target).setLevel(getattr(logging, level))
                _log_filter.remove_subsystem(target)
                self.preply(nick, reply_to, f"{target} = {level}")
                log.info(f"Log level {target} = {level} by {nick}")
            else:
                self.preply(nick, reply_to,
                    f"{nick}: invalid level — use: {', '.join(self._VALID_LEVELS)} or NOTSET")
        else:
            self.preply(nick, reply_to, f"usage: {p}loglevel [LEVEL | <logger> <LEVEL>]")

    async def cmd_debug(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg or arg.strip().lower() == "on":
            _log_filter.global_debug = True
            self.preply(nick, reply_to, "Debug output ON (all subsystems)")
            log.info(f"Debug ON by {nick}")
            return
        parts = arg.strip().lower().split()
        if parts[0] == "off":
            _log_filter.global_debug = False
            _log_filter.clear_subsystems()
            self.preply(nick, reply_to, f"Debug output OFF (back to {LOG_LEVEL})")
            log.info(f"Debug OFF by {nick}")
            return
        subsys = f"internets.{parts[0]}" if not parts[0].startswith("internets") else parts[0]
        if len(parts) >= 2 and parts[1] == "off":
            logging.getLogger(subsys).setLevel(logging.NOTSET)
            _log_filter.remove_subsystem(subsys)
            self.preply(nick, reply_to, f"{subsys} debug OFF")
            log.info(f"Debug {subsys} OFF by {nick}")
        else:
            logging.getLogger(subsys).setLevel(logging.DEBUG)
            _log_filter.add_subsystem(subsys)
            self.preply(nick, reply_to, f"{subsys} debug ON")
            log.info(f"Debug {subsys} ON by {nick}")

    async def cmd_shutdown(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        reason = arg.strip() if arg else "Shutting down"
        self.preply(nick, reply_to, f"Shutting down: {reason}")
        log.info(f"Shutdown by {nick}: {reason}")
        self.request_shutdown(reason)

    # ── Shutdown coordination ────────────────────────────────────────

    def request_shutdown(self, reason: str = "Shutting down") -> None:
        """Thread-safe: request the event loop to shut down cleanly."""
        self._quit_msg = f"QUIT :{reason}"
        if self._stop and self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def graceful_shutdown(self) -> None:
        """Clean exit: save state, unload modules, send QUIT, close socket."""
        log.info("Graceful shutdown initiated.")

        try:
            self._store.channels_save(self.active_channels.snapshot())
        except Exception as e:
            log.warning(f"Channel save failed: {e}")

        with self._mod_lock:
            names = list(self._modules)
        for name in names:
            try:
                ok, msg = self.unload_module(name)
                log.info(f"Unload {name}: {msg}")
            except Exception as e:
                log.warning(f"Unload {name} failed: {e}")

        try:
            self._store.stop()
            log.info("Store flushed to disk.")
        except Exception as e:
            log.warning(f"Store flush failed: {e}")

        try:
            self.send(self._quit_msg, priority=0)
        except Exception:
            pass

        # Give the sender time to flush QUIT.
        await asyncio.sleep(2)

        if self._sender:
            await self._sender.stop()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        # Cancel all running tasks.
        for task in self._tasks:
            task.cancel()

        log.info("Shutdown complete.")

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, nick: str, reply_to: str, cmd: str,
                  arg: str | None, is_pm: bool) -> None:
        """Create an async task to run a command handler.

        Called from _process() which runs in the event loop thread, so
        loop.create_task() is safe.  All handlers are coroutines.
        """
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in PM.")
            return
        if self.flood_limited(nick):
            self.notice(nick, f"{nick}: slow down ({FLOOD_CD}s cooldown)")
            log.debug(f"Flood drop: {cmd!r} from {nick}")
            return

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
                self._run_cmd(handler, nick, reply_to, arg, cmd),
                name=f"cmd-{cmd}",
            )
            self._tasks.append(task)
            task.add_done_callback(self._tasks.remove)

    async def _run_cmd(self, handler: Any, nick: str, reply_to: str,
                       arg: str | None, cmd: str) -> None:
        """Run an async command handler as a task."""
        try:
            await handler(nick, reply_to, arg)
        except Exception as e:
            log.error(f"Command {cmd!r} from {nick} crashed: {e}", exc_info=True)

    # ── Connection ───────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open an async SSL/plain connection to the IRC server."""
        use_ssl = cfg["irc"].getboolean("ssl",        fallback=True)
        verify  = cfg["irc"].getboolean("ssl_verify", fallback=True)
        log.info(f"Connecting {SERVER}:{PORT} "
                 f"({'SSL' if use_ssl else 'plain'}"
                 f"{', no verify' if use_ssl and not verify else ''})")

        ssl_ctx: ssl.SSLContext | None = None
        if use_ssl:
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode    = ssl.CERT_NONE

        self._reader, self._writer = await asyncio.open_connection(
            SERVER, PORT, ssl=ssl_ctx,
        )

        self._nick = NICKNAME
        self._cap_busy = False
        self._caps     = set()
        self._chanops  = {}
        self._ns_identified = False
        self._sasl_in_progress = False

        # (Re)start the sender on the new writer.
        if self._sender:
            await self._sender.stop()
        self._sender = Sender(self._loop)
        self._sender.start(self._writer)

    # ── Background tasks ─────────────────────────────────────────────

    async def _keepalive(self) -> None:
        """Send PING every 90s to detect dead connections."""
        while True:
            await asyncio.sleep(90)
            self.send(f"PING :{SERVER}", priority=0)

    async def _deferred_rejoin(self) -> None:
        """Wait for NickServ confirmation (up to 10s) then rejoin saved channels."""
        if NS_PW:
            for _ in range(40):
                if self._ns_identified:
                    break
                await asyncio.sleep(0.25)
            if self._ns_identified:
                log.info("NickServ confirmed — rejoining channels.")
            else:
                log.warning("NickServ did not confirm within 10s — "
                            "rejoining anyway (some +R channels may reject).")
        saved = self._store.channels_load()
        if not saved:
            log.info("No saved channels — waiting for INVITE.")
            return
        for ch in saved:
            self.send(f"JOIN {ch}")
            self.active_channels.add(ch.lower())
            log.info(f"Rejoined {ch}")

    # ── Channel state ────────────────────────────────────────────────

    def _on_invite(self, nick: str, channel: str) -> None:
        log.info(f"Invited to {channel} by {nick}")
        self.send(f"JOIN {channel}")
        self.active_channels.add(channel.lower())
        self._store.channels_save(self.active_channels.snapshot())

    def _on_join(self, channel: str) -> None:
        self.active_channels.add(channel.lower())
        self._store.channels_save(self.active_channels.snapshot())
        log.info(f"Joined {channel}")

    def _on_part(self, channel: str) -> None:
        self.active_channels.discard(channel.lower())
        self._chanops.pop(channel.lower(), None)
        self._store.channels_save(self.active_channels.snapshot())
        log.info(f"Left {channel}")

    # ── IRC line processing ──────────────────────────────────────────

    def _process(self, line: str) -> None:
        """Parse a single IRC line and dispatch.

        Runs in the event loop thread.  All work is in-memory;
        command handlers are dispatched to the thread pool.
        """
        if line.startswith("PING"):
            payload = line.split(":", 1)[1] if ":" in line else line.split(" ", 1)[-1]
            self.send(f"PONG :{payload}", priority=0)
            return

        line = strip_tags(line)

        # Let modules see every raw line.
        with self._mod_lock:
            snapshot = list(self._modules.values())
        for inst in snapshot:
            try:
                inst.on_raw(line)
            except Exception as e:
                log.debug(f"on_raw error in {type(inst).__name__}: {e}")

        m = re.match(r"(?::\S+ )?CAP \S+ (\S+)(?: :?(.*))?", line)
        if m:
            sub    = m.group(1).upper()
            params = (m.group(2) or "").strip()
            if sub == "LS":
                offered = {cap.split("=", 1)[0] for cap in params.split()}
                wanted = DESIRED_CAPS & offered
                if wanted:
                    self.send(f"CAP REQ :{' '.join(sorted(wanted))}", priority=0)
                else:
                    self.send("CAP END", priority=0)
                    self._cap_busy = False
            elif sub in ("ACK", "NAK"):
                if sub == "ACK":
                    self._caps = set(params.split())
                    log.info(f"Caps ACK: {self._caps}")
                else:
                    log.info(f"Caps NAK: {params}")
                if "sasl" in self._caps and NS_PW and not self._sasl_in_progress:
                    self._sasl_in_progress = True
                    self.send("AUTHENTICATE PLAIN", priority=0)
                    log.info("Starting SASL PLAIN authentication")
                else:
                    self.send("CAP END", priority=0)
                    self._cap_busy = False
            elif sub == "NEW":
                offered = {cap.split("=", 1)[0] for cap in params.split()}
                new = DESIRED_CAPS & offered
                if new:
                    self.send(f"CAP REQ :{' '.join(sorted(new))}", priority=0)
            return

        if line == "AUTHENTICATE +" and self._sasl_in_progress:
            payload = sasl_plain_payload(NICKNAME, NS_PW)
            self.send(f"AUTHENTICATE {payload}", priority=0)
            return

        if re.match(r":\S+ 903 ", line):
            self._sasl_in_progress = False
            self._ns_identified = True
            log.info("SASL authentication successful")
            self.send("CAP END", priority=0)
            self._cap_busy = False
            return

        if re.match(r":\S+ (902|904|905) ", line):
            self._sasl_in_progress = False
            log.warning("SASL authentication failed — will fall back to NickServ IDENTIFY")
            self.send("CAP END", priority=0)
            self._cap_busy = False
            return

        if re.match(r":\S+ 421 \S+ CAP ", line):
            if self._cap_busy:
                self._cap_busy = False
                log.info("Server has no CAP support — continuing without IRCv3")
            return

        if re.match(r":\S+ 451 ", line):
            if self._cap_busy:
                self.send("CAP END", priority=0)
                self._cap_busy = False
            return

        if re.match(r":\S+ 433 ", line):
            base = NICKNAME.rstrip("_")
            if len(self._nick) < len(base) + 3:
                self._nick = self._nick + "_"
            else:
                import random
                self._nick = base + str(random.randint(10, 99))
            self.send(f"NICK {self._nick}", priority=0)
            log.warning(f"Nick in use — trying {self._nick!r}")
            return

        if re.match(r":\S+ 005 ", line):
            cm = re.search(r"CHANMODES=(\S+)", line)
            if cm:
                self._chanmode_types = parse_isupport_chanmodes(cm.group(1))
                log.debug(f"ISUPPORT CHANMODES: {len(self._chanmode_types)} modes parsed")
            pm = re.search(r"PREFIX=(\S+)", line)
            if pm:
                self._prefix_modes, _ = parse_isupport_prefix(pm.group(1))
                log.debug(f"ISUPPORT PREFIX modes: {self._prefix_modes}")

        m = re.match(r":\S+ 473 \S+ (\S+)", line)
        if m:
            chan = m.group(1)
            svc  = self._services_nick
            log.info(f"Cannot join {chan} (invite-only) — asking {svc} for INVITE")
            self.send(f"PRIVMSG {svc} :INVITE {chan}")
            return

        m = re.match(r":\S+ (471|474|475) \S+ (\S+)", line)
        if m:
            num, chan = m.group(1), m.group(2)
            reasons  = {"471": "channel full", "474": "banned", "475": "bad key"}
            log.warning(f"Cannot join {chan} ({reasons.get(num, num)}) — "
                        f"removing from saved channels")
            self.active_channels.discard(chan.lower())
            self._store.channels_save(self.active_channels.snapshot())
            return

        if re.match(r":\S+ 381 ", line):
            log.info("OPER granted by server.")
            if OPER_MODES:
                self.send(f"MODE {self._nick} {OPER_MODES}")
                log.info(f"Oper modes: MODE {self._nick} {OPER_MODES}")
            if OPER_SNOMASK:
                self.send(f"MODE {self._nick} +s {OPER_SNOMASK}")
                log.info(f"Snomask: MODE {self._nick} +s {OPER_SNOMASK}")
            return

        if re.match(r":\S+ 491 ", line):
            log.warning("OPER failed — wrong credentials or host not permitted.")
            return

        if not self._ns_identified:
            if re.match(r":\S+ 900 ", line):
                self._ns_identified = True
                log.info("NickServ: identified (900 numeric)")
                return
            m = re.match(r":([^!]+)!\S+ NOTICE \S+ :(.*)", line)
            if m:
                src, text = m.group(1), m.group(2).lower()
                if src.lower() == "nickserv" and (
                    "identified" in text or "recognized" in text
                ):
                    self._ns_identified = True
                    log.info("NickServ: identified (NOTICE)")

        m = re.match(r":\S+ 353 \S+ [=*@] (\S+) :(.*)", line)
        if m:
            chan, names_str = m.group(1).lower(), m.group(2).strip()
            ops = self._chanops.setdefault(chan, set())
            for entry in names_str.split():
                nick_clean, is_op = parse_names_entry(entry)
                if nick_clean and is_op:
                    ops.add(nick_clean.lower())
            return

        m = re.match(r":\S+ MODE (\S+) ([+-]\S+)(.*)", line)
        if m:
            chan = m.group(1)
            if chan.startswith(("#", "&", "+", "!")):
                mode_str = m.group(2)
                args     = m.group(3).strip().split() if m.group(3).strip() else []
                chan_l    = chan.lower()
                ops      = self._chanops.setdefault(chan_l, set())
                op_modes = {"o", "a", "q"} & self._prefix_modes
                for adding, ch, param in parse_mode_changes(
                    mode_str, args, self._prefix_modes, self._chanmode_types
                ):
                    if ch in op_modes and param:
                        target = param.lower()
                        if adding:
                            ops.add(target)
                            log.debug(f"Chanop add: {target} in {chan} (+{ch})")
                        else:
                            ops.discard(target)
                            log.debug(f"Chanop remove: {target} in {chan} (-{ch})")
            return

        m = re.match(r":([^!]+)![^@]+@\S+ CHGHOST (\S+) (\S+)", line)
        if m:
            self._store.user_rename(m.group(1), m.group(1), f"{m.group(2)}@{m.group(3)}")
            return

        m = re.match(r":([^!]+)![^@]+@\S+ ACCOUNT (\S+)", line)
        if m:
            log.debug(f"ACCOUNT: {m.group(1)} -> {m.group(2)}")
            return

        m = re.match(r":([^!]+)![^@]+@\S+ INVITE \S+ :?(\S+)", line)
        if m:
            self._on_invite(m.group(1), m.group(2))
            return

        m = re.match(r":([^!]+)![^@]+@(\S+) JOIN :?(\S+)(?:\s+\S+)?", line)
        if m:
            nick, host, chan = m.group(1), m.group(2), m.group(3)
            if nick.lower() == self._nick.lower():
                self._on_join(chan)
            else:
                self._store.user_join(chan, nick, host)
            return

        m = re.match(r":([^!]+)![^@]+@\S+ PART :?(\S+)", line)
        if m:
            nick, chan = m.group(1), m.group(2)
            if nick.lower() == self._nick.lower():
                self._on_part(chan)
            else:
                self._store.user_part(chan, nick)
                ops = self._chanops.get(chan.lower())
                if ops:
                    ops.discard(nick.lower())
            return

        m = re.match(r":\S+ KICK (\S+) (\S+)", line)
        if m:
            chan, nick = m.group(1), m.group(2)
            if nick.lower() == self._nick.lower():
                self._on_part(chan)
                log.info(f"Kicked from {chan}")
            else:
                self._store.user_part(chan, nick)
                ops = self._chanops.get(chan.lower())
                if ops:
                    ops.discard(nick.lower())
            return

        m = re.match(r":([^!]+)![^@]+@\S+ QUIT", line)
        if m:
            nick_l = m.group(1).lower()
            self._store.user_quit(m.group(1))
            for ops in self._chanops.values():
                ops.discard(nick_l)
            return

        m = re.match(r":([^!]+)![^@]+@(\S+) NICK :?(\S+)", line)
        if m:
            old_nick, host, new_nick = m.group(1), m.group(2), m.group(3)
            if old_nick.lower() == self._nick.lower():
                self._nick = new_nick
                log.info(f"Own nick changed: {old_nick} -> {new_nick}")
            self._store.user_rename(old_nick, new_nick, host)
            if old_nick in self._authed:
                self._authed.discard(old_nick)
                self._authed.add(new_nick)
                log.info(f"Auth migrated: {old_nick} -> {new_nick}")
            old_l, new_l = old_nick.lower(), new_nick.lower()
            for ops in self._chanops.values():
                if old_l in ops:
                    ops.discard(old_l)
                    ops.add(new_l)
            return

        m = re.match(r":([^!]+)![^@]+@(\S+) PRIVMSG (\S+) :(.*)", line)
        if not m:
            return

        nick, host, target, text = m.groups()
        text     = text.strip()
        is_pm    = target.lower() == self._nick.lower()
        reply_to = nick if is_pm else target

        if not is_pm and target.lower() in self.active_channels:
            self._store.user_join(target, nick, host)

        with self._mod_lock:
            all_cmds = set(self._CORE) | set(self._commands)
        cmd = arg = None

        if text.startswith(CMD_PREFIX):
            rest = text[len(CMD_PREFIX):]
            parts = rest.split(None, 1)
            if parts:
                cmd   = parts[0].lower()
                arg   = parts[1].strip() if len(parts) > 1 else None
        elif is_pm:
            parts = text.split(None, 1)
            if parts and parts[0].lower() in all_cmds:
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else None

        if cmd and cmd in all_cmds:
            log_arg = "[REDACTED]" if cmd in ("auth", "deauth") else arg
            log.info(f"cmd={cmd!r} arg={log_arg!r} from {nick}!{host} "
                     f"{'(PM)' if is_pm else 'in ' + reply_to}")
            self._dispatch(nick, reply_to, cmd, arg, is_pm)

    # ── Main loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point.  Call with asyncio.run() or as a task."""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        # Signal handlers (Unix only).
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(
                    sig, lambda s=sig: self._on_signal(s))
            except NotImplementedError:
                pass  # Windows

        self.autoload_modules()
        log.info(f"Desired caps: {', '.join(sorted(DESIRED_CAPS))}")

        # Initial connect with backoff.
        attempt = 0
        while True:
            try:
                await self._connect()
                break
            except Exception as e:
                delay = _backoff(attempt)
                log.error(f"Connect failed: {e} — retry in {delay:.0f}s")
                await asyncio.sleep(delay)
                attempt += 1

        identified = False
        registered = False

        while not self._stop.is_set():
            try:
                if not registered:
                    if SERVER_PW:
                        self.send(f"PASS {SERVER_PW}", priority=0)
                    self.send("CAP LS 302", priority=0)
                    self._cap_busy = True
                    self.send(f"NICK {self._nick}", priority=0)
                    self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)
                    registered = True

                # Read one line at a time.
                try:
                    raw = await asyncio.wait_for(
                        self._reader.readline(), timeout=300)
                except asyncio.TimeoutError:
                    # No data in 300s — connection is probably dead.
                    raise ConnectionResetError("Read timeout (300s)")

                if not raw:
                    raise ConnectionResetError("Server closed connection")

                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue

                # Redact auth passwords from debug log.
                if re.search(r"PRIVMSG\s+\S+\s+:\.?AUTH\s", line, re.IGNORECASE):
                    log.debug(f"<< {line.split(':',2)[0]}:*** AUTH [REDACTED] ***")
                else:
                    log.debug(f"<< {line}")

                self._process(line)

                if not identified and re.match(r":\S+ (376|422) ", line):
                    if self._cap_busy:
                        self.send("CAP END", priority=0)
                        self._cap_busy = False
                    if USER_MODES:
                        self.send(f"MODE {self._nick} {USER_MODES}")
                        log.info(f"User modes: MODE {self._nick} {USER_MODES}")
                    if NS_PW and not self._ns_identified:
                        self.send(f"PRIVMSG NickServ :IDENTIFY {NS_PW}")
                    if OPER_N and OPER_PW:
                        self.send(f"OPER {OPER_N} {OPER_PW}")
                    # Start keepalive and deferred rejoin as async tasks.
                    ka_task = asyncio.create_task(
                        self._keepalive(), name="keepalive")
                    self._tasks.append(ka_task)
                    rejoin_task = asyncio.create_task(
                        self._deferred_rejoin(), name="rejoin")
                    self._tasks.append(rejoin_task)
                    identified = True

            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, ssl.SSLError, OSError) as e:
                if self._stop.is_set():
                    break

                # Cancel background tasks.
                for task in self._tasks:
                    task.cancel()
                self._tasks.clear()
                if self._sender:
                    await self._sender.stop()

                if self._authed:
                    log.info(f"Cleared {len(self._authed)} admin session(s) on disconnect.")
                    self._authed.clear()
                identified, registered = False, False

                # Reconnect with backoff.
                attempt = 0
                while not self._stop.is_set():
                    delay = _backoff(attempt)
                    log.warning(f"Lost connection: {e} — reconnect in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    try:
                        await self._connect()
                        break
                    except Exception as ce:
                        log.error(f"Reconnect failed: {ce}")
                        attempt += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)

        # Clean exit.
        await self.graceful_shutdown()

    def _on_signal(self, signum: int) -> None:
        log.info(f"Received signal {signum}, shutting down.")
        self._quit_msg = "QUIT :Caught signal, shutting down"
        if self._stop:
            self._stop.set()


# ── Interactive console ──────────────────────────────────────────────

_CONSOLE_HELP = """\
  debug [on|off]            global debug toggle
  debug <sub> [off]         per-subsystem debug (e.g. debug weather)
  loglevel [LEVEL]          show or set base level (DEBUG/INFO/WARNING/ERROR)
  loglevel <logger> LEVEL   set a specific logger
  status                    show bot state (nick, channels, modules, log levels)
  shutdown [reason]         graceful shutdown
  quit                      alias for shutdown"""


async def _run_console(bot: IRCBot) -> None:
    """Async console: reads stdin in a thread, processes commands."""
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
            line = line.strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower()
        args  = parts[1:]

        if cmd == "help":
            print(_CONSOLE_HELP)

        elif cmd == "debug":
            if not args or args[0] == "on":
                _log_filter.global_debug = True
                print("Debug output ON (all subsystems)")
            elif args[0] == "off":
                _log_filter.global_debug = False
                _log_filter.clear_subsystems()
                print(f"Debug output OFF (back to {LOG_LEVEL})")
            else:
                sub = f"internets.{args[0]}" if not args[0].startswith("internets") else args[0]
                if len(args) >= 2 and args[1] == "off":
                    logging.getLogger(sub).setLevel(logging.NOTSET)
                    _log_filter.remove_subsystem(sub)
                    print(f"{sub} debug OFF")
                else:
                    logging.getLogger(sub).setLevel(logging.DEBUG)
                    _log_filter.add_subsystem(sub)
                    print(f"{sub} debug ON")

        elif cmd == "loglevel":
            valid = ("DEBUG", "INFO", "WARNING", "ERROR")
            if not args:
                lvl_name = logging.getLevelName(_log_filter.base_level)
                print(f"  base level = {lvl_name}")
                if _log_filter.global_debug:
                    print("  global debug = ON")
                active = _log_filter.active_subsystems()
                if active:
                    print(f"  debug subsystems: {', '.join(sorted(active))}")
                if LOG_DEBUG:
                    print(f"  debug file = {LOG_DEBUG}")
            elif len(args) == 1:
                level = args[0].upper()
                if level not in valid:
                    print(f"Invalid level — use: {', '.join(valid)}")
                else:
                    _log_filter.set_base_level(getattr(logging, level))
                    _log_filter.global_debug = False
                    print(f"Base level set to {level}")
            elif len(args) == 2:
                target, level = args[0], args[1].upper()
                if not target.startswith("internets"):
                    print("Logger must start with 'internets'")
                elif level == "DEBUG":
                    full = target if "." in target else f"internets.{target}"
                    logging.getLogger(full).setLevel(logging.DEBUG)
                    _log_filter.add_subsystem(full)
                    print(f"{full} = DEBUG")
                elif level in valid or level == "NOTSET":
                    logging.getLogger(target).setLevel(getattr(logging, level))
                    _log_filter.remove_subsystem(target)
                    print(f"{target} = {level}")
                else:
                    print(f"Invalid level — use: {', '.join(valid)} or NOTSET")
            else:
                print("usage: loglevel [LEVEL | <logger> LEVEL]")

        elif cmd == "status":
            print(f"  nick     = {bot._nick}")
            print(f"  channels = {', '.join(sorted(bot.active_channels.snapshot())) or '(none)'}")
            with bot._mod_lock:
                mods = list(bot._modules)
            print(f"  modules  = {', '.join(mods) or '(none)'}")
            print(f"  admins   = {', '.join(sorted(bot._authed)) or '(none)'}")
            lvl_name = logging.getLevelName(_log_filter.base_level)
            print(f"  log level = {lvl_name}"
                  f"{' (global debug ON)' if _log_filter.global_debug else ''}")
            active = _log_filter.active_subsystems()
            if active:
                print(f"  debug subs = {', '.join(sorted(active))}")

        elif cmd in ("shutdown", "quit"):
            reason = " ".join(args) if args else "Console shutdown"
            log.info(f"Console shutdown: {reason}")
            bot.request_shutdown(reason)
            break

        else:
            print(f"Unknown command: {cmd!r} — type 'help' for commands.")


# ── Entry point ──────────────────────────────────────────────────────

async def _main() -> None:
    bot = IRCBot()

    tasks: list[asyncio.Task] = []

    if not _args.no_console and sys.stdin.isatty():
        tasks.append(asyncio.create_task(_run_console(bot), name="console"))
        log.info("Interactive console enabled (type 'help' for commands)")

    tasks.append(asyncio.create_task(bot.run(), name="bot"))

    # Wait for the bot task to finish (shutdown or crash).
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if bot._restart_flag:
        log.info("Executing restart via os.execv ...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
