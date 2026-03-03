#!/usr/bin/env python3
"""
Internets — modular IRC bot.

Core commands: .help .modules .auth .deauth
               .load .unload .reload .reloadall .restart .rehash

Modules live in modules/. Each exposes setup(bot) -> BotModule.
See modules/base.py for the interface.
"""

import ssl
import socket
import re
import sys
import os
import time
import threading
import logging
import logging.handlers
import configparser
import importlib
import importlib.util
from pathlib import Path

from store  import Store, RateLimiter
from sender import Sender
from hashpw import verify_password

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
DESIRED_CAPS = {
    "multi-prefix", "away-notify", "account-notify", "chghost",
    "extended-join", "server-time", "message-tags",
}

import argparse

_cli = argparse.ArgumentParser(
    description="Internets — modular IRC bot",
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
      - global_debug is True (`.debug on`), OR
      - record's logger name is in the subsystem debug set (`.debug weather`)
    """

    def __init__(self, base_level=logging.INFO):
        super().__init__()
        self.base_level   = base_level
        self.global_debug = False
        self._subsystems  = set()  # e.g. {"internets.weather", "internets.store"}
        self._lock        = threading.Lock()

    def filter(self, record):
        if record.levelno >= self.base_level:
            return True
        if self.global_debug:
            return True
        with self._lock:
            # Check if this record's logger or any parent is in the debug set.
            name = record.name
            for sub in self._subsystems:
                if name == sub or name.startswith(sub + "."):
                    return True
        return False

    def set_base_level(self, level):
        self.base_level = level

    def add_subsystem(self, name):
        with self._lock:
            self._subsystems.add(name)

    def remove_subsystem(self, name):
        with self._lock:
            self._subsystems.discard(name)

    def clear_subsystems(self):
        with self._lock:
            self._subsystems.clear()

    def active_subsystems(self):
        with self._lock:
            return set(self._subsystems)


def _setup_logging():
    """Configure the internets logger with rotating file + console + optional debug file."""
    root = logging.getLogger("internets")
    root.setLevel(logging.DEBUG)  # let handlers decide what to emit
    root.handlers.clear()

    fmt = logging.Formatter(LOG_FMT)

    filt = _DebugFilter(getattr(logging, LOG_LEVEL, logging.INFO))

    # Main log file — level from config, rotated.
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX, backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setLevel(logging.DEBUG)  # filter does the real gating
    fh.setFormatter(fmt)
    fh.addFilter(filt)
    root.addHandler(fh)

    # Console — same level as file.
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    ch.addFilter(filt)
    root.addHandler(ch)

    # Optional debug file — always DEBUG, no filter, captures everything.
    if LOG_DEBUG:
        dh = logging.handlers.RotatingFileHandler(
            LOG_DEBUG, maxBytes=LOG_MAX, backupCount=LOG_BACKUPS, encoding="utf-8")
        dh.setLevel(logging.DEBUG)
        dh.setFormatter(fmt)
        dh._debug_file = True  # tag so runtime commands skip it
        root.addHandler(dh)

    return filt


_log_filter = _setup_logging()
log = logging.getLogger("internets")

# Apply --debug CLI flags.
if _args.debug is not None:
    if len(_args.debug) == 0:
        # --debug with no args: global debug
        _log_filter.global_debug = True
        log.info("CLI: global debug enabled")
    else:
        # --debug weather store: per-subsystem
        for sub in _args.debug:
            full = f"internets.{sub}" if not sub.startswith("internets") else sub
            logging.getLogger(full).setLevel(logging.DEBUG)
            _log_filter.add_subsystem(full)
            log.info(f"CLI: debug enabled for {full}")


def _get_hash():
    cfg.read("config.ini")
    return cfg["admin"].get("password_hash", "").strip()


def _validate_hash():
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


class IRCBot:
    # IRC line limit is 512 bytes incl. CRLF; 400 bytes body leaves room for any prefix.
    _MAX_BODY = 400

    _CORE = {
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

    def __init__(self):
        self.sock            = None
        self.cfg             = cfg
        self.active_channels = set()
        self._modules        = {}
        self._commands       = {}
        self._mod_lock       = threading.Lock()
        self._authed         = set()
        self._auth_fails     = {}  # nick_lower -> (fail_count, last_fail_time)
        self._AUTH_MAX_FAILS = 5
        self._AUTH_LOCKOUT   = 300  # 5-minute lockout after max failures
        self._ka_stop        = threading.Event()
        self._sender         = Sender()
        self._store          = Store(
            cfg["bot"].get("locations_file", "locations.json"),
            cfg["bot"].get("channels_file",  "channels.json"),
            cfg["bot"].get("users_file",     "users.json"),
        )
        self._rate     = RateLimiter(FLOOD_CD, API_CD)
        self._cap_busy = False
        self._caps     = set()
        self._nick     = NICKNAME
        # Channel operator tracking: {channel_lower: {nick_lower, ...}}
        # Populated from 353 (NAMES) and maintained via MODE +o/-o/+a/-a/+q/-q.
        # Prefixes ~(owner), &(admin), @(op) all count as "chanop" for ACL purposes.
        self._chanops  = {}
        self._ns_identified = False
        self._services_nick = cfg["bot"].get("services_nick", "ChanServ").strip()

    def send(self, msg, priority=1):
        self._sender.enqueue(msg, priority)

    def privmsg(self, target, msg):
        for chunk in self._split_msg(msg):
            self.send(f"PRIVMSG {target} :{chunk}")

    def notice(self, target, msg):
        for chunk in self._split_msg(msg):
            self.send(f"NOTICE {target} :{chunk}")

    def reply(self, nick, reply_to, msg, privileged=False):
        if not reply_to.startswith(("#", "&", "+", "!")):
            self.privmsg(nick, msg)
        elif privileged:
            self.notice(nick, msg)
        else:
            self.privmsg(reply_to, msg)

    def preply(self, nick, reply_to, msg):
        self.reply(nick, reply_to, msg, privileged=True)

    def _split_msg(self, msg):
        enc = msg.encode("utf-8", errors="replace")
        while enc:
            chunk = enc[:self._MAX_BODY]
            # Back up to the last valid UTF-8 character boundary.
            # Continuation bytes have the pattern 10xxxxxx (0x80..0xBF).
            if len(enc) > self._MAX_BODY:
                while chunk and (chunk[-1] & 0xC0) == 0x80:
                    chunk = chunk[:-1]
                if not chunk:
                    chunk = enc[:self._MAX_BODY]  # fallback: force split
            yield chunk.decode("utf-8", errors="replace")
            enc = enc[len(chunk):]

    def is_admin(self, nick):       return nick in self._authed
    def is_chanop(self, channel, nick):
        return nick.lower() in self._chanops.get(channel.lower(), set())
    def flood_limited(self, nick):  return self._rate.flood_check(nick, self.is_admin(nick))
    def rate_limited(self, nick):   return self._rate.api_check(nick)
    def loc_get(self, nick):        return self._store.loc_get(nick)
    def loc_set(self, nick, raw):   self._store.loc_set(nick, raw)
    def loc_del(self, nick):        return self._store.loc_del(nick)
    def channel_users(self, ch):    return self._store.channel_users(ch)

    def load_module(self, name):
        with self._mod_lock:
            # Reject path separators / parent refs.
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

    def unload_module(self, name):
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

    def reload_module(self, name):
        ok, msg = self.unload_module(name)
        return (False, msg) if not ok else self.load_module(name)

    def autoload_modules(self):
        for name in AUTO_LOAD:
            ok, msg = self.load_module(name)
            (log.info if ok else log.warning)(msg)

    def _require_admin(self, nick, reply_to):
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: auth first — /MSG {self._nick} AUTH <pw>")
            return False
        return True

    def cmd_auth(self, nick, reply_to, arg):
        h = _get_hash()
        if not h:
            self.preply(nick, reply_to, f"{nick}: no password_hash configured — run hashpw.py")
            return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: /MSG {self._nick} AUTH <password>")
            return

        # Prune stale lockout entries every 50 attempts.
        k = nick.lower()
        now = time.time()
        if len(self._auth_fails) > 50:
            self._auth_fails = {
                n: (f, t) for n, (f, t) in self._auth_fails.items()
                if now - t < self._AUTH_LOCKOUT
            }
        fails, last_t = self._auth_fails.get(k, (0, 0))
        if now - last_t > self._AUTH_LOCKOUT:
            fails = 0  # Reset after lockout expires
        if fails >= self._AUTH_MAX_FAILS:
            remaining = int(self._AUTH_LOCKOUT - (now - last_t))
            self.preply(nick, reply_to,
                f"{nick}: too many failed attempts — try again in {remaining}s")
            log.warning(f"Auth lockout: {nick} ({fails} failures)")
            return

        try:
            ok = verify_password(arg.strip(), h)
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

    def cmd_deauth(self, nick, reply_to, arg):
        if nick in self._authed:
            self._authed.discard(nick)
            self.preply(nick, reply_to, f"{nick}: session ended.")
        else:
            self.preply(nick, reply_to, f"{nick}: not authenticated.")

    def cmd_help(self, nick, reply_to, arg):
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
        for name, inst in self._modules.items():
            hl = inst.help_lines(p)
            if hl:
                lines.append(f"  [{name}]")
                lines.extend(hl)
        lines.append(f"  In PM the '{p}' prefix is optional.")
        for line in lines:
            self.preply(nick, reply_to, line)

    def cmd_modules(self, nick, reply_to, arg):
        loaded = list(self._modules)
        self.preply(nick, reply_to,
            f"Loaded: {', '.join(loaded)}" if loaded else "No modules loaded.")
        avail = sorted(
            p.stem for p in MODULES_DIR.glob("*.py")
            if p.stem not in ("__init__", "base", "geocode", "nws", "units")
            and p.stem not in self._modules
        )
        if avail:
            self.preply(nick, reply_to, f"Available: {', '.join(avail)}")

    def cmd_load(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}load <module>"); return
        _, msg = self.load_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_unload(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}unload <module>"); return
        _, msg = self.unload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_reload(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}reload <module>"); return
        _, msg = self.reload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_reloadall(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
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

    def cmd_restart(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        self.preply(nick, reply_to, "Restarting ...")
        log.info(f"Restart by {nick}")

        # Save state and unload modules before replacing process.
        try:
            self._store.channels_save(self.active_channels)
        except Exception:
            pass
        with self._mod_lock:
            names = list(self._modules)
        for name in names:
            try:
                self.unload_module(name)
            except Exception:
                pass
        try:
            self._store.stop()
        except Exception:
            pass

        try:
            self.send("QUIT :Restarting ...", priority=0)
        except Exception:
            pass
        time.sleep(2)  # Let sender thread flush QUIT before execv replaces process
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def cmd_rehash(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        try:
            cfg.read("config.ini")
        except Exception as e:
            self.preply(nick, reply_to, f"Failed to read config.ini: {e}"); return

        # Apply log level from config.
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

    def cmd_mode(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        p = CMD_PREFIX
        if not arg:
            self.preply(nick, reply_to, f"usage: {p}mode <+/-modes>  e.g. {p}mode +ix")
            return
        mode_str = arg.strip()
        if not re.match(r"^[a-zA-Z+\- ]+$", mode_str):
            self.preply(nick, reply_to, f"{nick}: invalid mode string.")
            return
        self.send(f"MODE {self._nick} {mode_str}")
        self.preply(nick, reply_to, f"MODE {self._nick} {mode_str}")
        log.info(f"Mode set by {nick}: {mode_str}")

    def cmd_snomask(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        p = CMD_PREFIX
        if not arg:
            self.preply(nick, reply_to, f"usage: {p}snomask <+/-flags>  e.g. {p}snomask +cCkK")
            return
        mask = arg.strip()
        if not re.match(r"^[a-zA-Z+\-]+$", mask):
            self.preply(nick, reply_to, f"{nick}: invalid snomask string.")
            return
        self.send(f"MODE {self._nick} +s {mask}")
        self.preply(nick, reply_to, f"MODE {self._nick} +s {mask}")
        log.info(f"Snomask set by {nick}: {mask}")

    _VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

    def cmd_loglevel(self, nick, reply_to, arg):
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

    def cmd_debug(self, nick, reply_to, arg):
        """Quick toggle: .debug [on|off|<subsystem> [off]]"""
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

        # .debug weather  or  .debug weather off
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

    def cmd_shutdown(self, nick, reply_to, arg):
        if not self._require_admin(nick, reply_to): return
        reason = arg.strip() if arg else "Shutting down"
        self.preply(nick, reply_to, f"Shutting down: {reason}")
        log.info(f"Shutdown by {nick}: {reason}")
        self.graceful_shutdown(f"QUIT :{reason}")

    def graceful_shutdown(self, quit_msg="QUIT :Shutting down"):
        log.info("Graceful shutdown initiated.")

        try:
            self._store.channels_save(self.active_channels)
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

        # Flush all store data after modules are unloaded (on_unload may
        # write state), then stop the periodic flush timer.
        try:
            self._store.stop()
            log.info("Store flushed to disk.")
        except Exception as e:
            log.warning(f"Store flush failed: {e}")

        try:
            self.send(quit_msg, priority=0)
        except Exception:
            pass
        time.sleep(2)

        self._ka_stop.set()
        self._sender.stop()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

        log.info("Shutdown complete.")
        sys.exit(0)

    def dispatch(self, nick, reply_to, cmd, arg, is_pm):
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in PM.")
            return
        if self.flood_limited(nick):
            self.notice(nick, f"{nick}: slow down ({FLOOD_CD}s cooldown)")
            log.debug(f"Flood drop: {cmd!r} from {nick}")
            return

        def run(fn, *a):
            def _wrapper():
                try:
                    fn(*a)
                except Exception as e:
                    log.error(f"Command {cmd!r} from {nick} crashed: {e}", exc_info=True)
            threading.Thread(target=_wrapper, daemon=True).start()

        if cmd in self._CORE:
            run(getattr(self, self._CORE[cmd]), nick, reply_to, arg)
        else:
            with self._mod_lock:
                entry = self._commands.get(cmd)
                inst  = self._modules.get(entry[0]) if entry else None
            if inst and entry:
                run(getattr(inst, entry[1]), nick, reply_to, arg)

    def _make_socket(self):
        use_ssl = cfg["irc"].getboolean("ssl",        fallback=True)
        verify  = cfg["irc"].getboolean("ssl_verify", fallback=True)
        log.info(f"Connecting {SERVER}:{PORT} "
                 f"({'SSL' if use_ssl else 'plain'}"
                 f"{', no verify' if use_ssl and not verify else ''})")
        raw = socket.create_connection((SERVER, PORT), timeout=30)
        if use_ssl:
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=SERVER)
        raw.settimeout(300)
        return raw

    def _start_keepalive(self):
        stop = self._ka_stop
        def _loop():
            while not stop.wait(timeout=90):
                try:
                    self.send(f"PING :{SERVER}", priority=0)
                except Exception:
                    break
        threading.Thread(target=_loop, daemon=True, name="keepalive").start()

    def _connect(self):
        self._ka_stop.set()
        self._sender.stop()
        self._ka_stop  = threading.Event()
        self._nick     = NICKNAME
        self._cap_busy = False
        self._caps     = set()
        self._chanops  = {}  # Wipe stale op data; rebuilt from 353 after rejoin
        self._ns_identified = False
        self.sock      = self._make_socket()
        self._sender.start(self.sock)
        self._start_keepalive()

    def _on_invite(self, nick, channel):
        log.info(f"Invited to {channel} by {nick}")
        self.send(f"JOIN {channel}")
        self.active_channels.add(channel.lower())
        self._store.channels_save(self.active_channels)

    def _on_join(self, channel):
        self.active_channels.add(channel.lower())
        self._store.channels_save(self.active_channels)
        log.info(f"Joined {channel}")

    def _on_part(self, channel):
        self.active_channels.discard(channel.lower())
        self._chanops.pop(channel.lower(), None)
        self._store.channels_save(self.active_channels)
        log.info(f"Left {channel}")

    def _rejoin_channels(self):
        saved = self._store.channels_load()
        if not saved:
            log.info("No saved channels — waiting for INVITE.")
            return
        for ch in saved:
            self.send(f"JOIN {ch}")
            self.active_channels.add(ch.lower())
            log.info(f"Rejoined {ch}")

    def _deferred_rejoin(self):
        """Wait for NickServ confirmation (up to 10s) then rejoin saved channels."""
        if NS_PW:
            deadline = time.time() + 10
            while not self._ns_identified and time.time() < deadline:
                time.sleep(0.25)
            if self._ns_identified:
                log.info("NickServ confirmed — rejoining channels.")
            else:
                log.warning("NickServ did not confirm within 10s — "
                            "rejoining anyway (some +R channels may reject).")
        self._rejoin_channels()

    def run(self):
        self.autoload_modules()
        log.info(f"Desired caps: {', '.join(sorted(DESIRED_CAPS))}")

        while True:
            try:
                self._connect()
                break
            except Exception as e:
                log.error(f"Connect failed: {e} — retry in 30s")
                time.sleep(30)

        buf, identified, registered = "", False, False

        while True:
            try:
                if not registered and self.sock:
                    if SERVER_PW:
                        self.send(f"PASS {SERVER_PW}", priority=0)
                    self.send("CAP LS 302", priority=0)
                    self._cap_busy = True
                    self.send(f"NICK {self._nick}", priority=0)
                    self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)
                    registered = True

                data = self.sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    raise ConnectionResetError("Server closed connection")

                buf += data
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
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
                        # Set user modes before anything else (e.g. +ix for
                        # cloaking before joining channels).
                        if USER_MODES:
                            self.send(f"MODE {self._nick} {USER_MODES}")
                            log.info(f"User modes: MODE {self._nick} {USER_MODES}")
                        if NS_PW:
                            self.send(f"PRIVMSG NickServ :IDENTIFY {NS_PW}")
                        if OPER_N and OPER_PW:
                            self.send(f"OPER {OPER_N} {OPER_PW}")
                        # Rejoin in background: waits for NickServ confirmation
                        # (if applicable) before sending JOINs, so +R channels
                        # and ChanServ access lists work.
                        threading.Thread(target=self._deferred_rejoin,
                                         daemon=True, name="rejoin").start()
                        identified = True

            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, ssl.SSLError, OSError) as e:
                log.warning(f"Lost connection: {e} — reconnect in 15s")
                self._sender.stop()
                # Nicks may belong to different people after reconnect.
                if self._authed:
                    log.info(f"Cleared {len(self._authed)} admin session(s) on disconnect.")
                    self._authed.clear()
                identified, registered, buf = False, False, ""
                time.sleep(15)
                while True:
                    try:
                        self._connect()
                        break
                    except Exception as ce:
                        log.error(f"Reconnect failed: {ce} — retry in 30s")
                        time.sleep(30)

            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}")
                time.sleep(5)

    def _process(self, line):
        if line.startswith("PING"):
            payload = line.split(":", 1)[1] if ":" in line else line.split(" ", 1)[-1]
            self.send(f"PONG :{payload}", priority=0)
            return

        # Strip IRCv3 message tags before parsing. Tags carry metadata like
        # server-time but don't change how we handle the underlying message.
        if line.startswith("@"):
            _, _, line = line.partition(" ")

        # Let modules see every raw line for numerics, NOTICEs, etc.
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
                self.send("CAP END", priority=0)
                self._cap_busy = False
            elif sub == "NEW":
                offered = {cap.split("=", 1)[0] for cap in params.split()}
                new = DESIRED_CAPS & offered
                if new:
                    self.send(f"CAP REQ :{' '.join(sorted(new))}", priority=0)
            return

        # 421 = "Unknown command" — server has no CAP support at all
        if re.match(r":\S+ 421 \S+ CAP ", line):
            if self._cap_busy:
                self._cap_busy = False
                log.info("Server has no CAP support — continuing without IRCv3")
            return

        # 451 = "Not registered" — some servers fire this before we send CAP END
        if re.match(r":\S+ 451 ", line):
            if self._cap_busy:
                self.send("CAP END", priority=0)
                self._cap_busy = False
            return

        # 433 = "Nickname in use" — try alternatives, capped at 9 chars of suffix.
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

        # 473 = ERR_INVITEONLYCHAN — channel is +i and we have no invite.
        # Ask ChanServ to re-invite us (works if bot's NickServ account has
        # channel access). Format: :server 473 botnick #channel :Cannot join
        m = re.match(r":\S+ 473 \S+ (\S+)", line)
        if m:
            chan = m.group(1)
            svc  = self._services_nick
            log.info(f"Cannot join {chan} (invite-only) — asking {svc} for INVITE")
            self.send(f"PRIVMSG {svc} :INVITE {chan}")
            return

        # 471/474/475 = Channel full / Banned / Bad key — log and remove from
        # active_channels so we don't retry endlessly. User must re-invite.
        m = re.match(r":\S+ (471|474|475) \S+ (\S+)", line)
        if m:
            num, chan = m.group(1), m.group(2)
            reasons  = {"471": "channel full", "474": "banned", "475": "bad key"}
            log.warning(f"Cannot join {chan} ({reasons.get(num, num)}) — "
                        f"removing from saved channels")
            self.active_channels.discard(chan.lower())
            self._store.channels_save(self.active_channels)
            return

        # 381 = RPL_YOUREOPER — OPER succeeded.
        # Apply oper-only modes and server notice mask.
        if re.match(r":\S+ 381 ", line):
            log.info("OPER granted by server.")
            if OPER_MODES:
                self.send(f"MODE {self._nick} {OPER_MODES}")
                log.info(f"Oper modes: MODE {self._nick} {OPER_MODES}")
            if OPER_SNOMASK:
                self.send(f"MODE {self._nick} +s {OPER_SNOMASK}")
                log.info(f"Snomask: MODE {self._nick} +s {OPER_SNOMASK}")
            return

        # 491 = ERR_NOOPERHOST — OPER failed (wrong host/credentials).
        if re.match(r":\S+ 491 ", line):
            log.warning("OPER failed — wrong credentials or host not permitted.")
            return

        # NickServ identification confirmation.
        # Matches NOTICE from NickServ containing "identified" or "recognized",
        # and numeric 900 (RPL_LOGGEDIN) sent by some services on IDENTIFY.
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
                    # Don't return — let other NOTICE handlers see it too

        # 353 = RPL_NAMREPLY — parse channel operator prefixes from NAMES list.
        # Format: :server 353 botnick [=*@] #channel :@nick1 +nick2 nick3
        # With multi-prefix cap: :server 353 botnick = #channel :~@nick1 @+nick2
        # Prefixes ~(owner/+q), &(admin/+a), @(op/+o) all grant chanop status.
        m = re.match(r":\S+ 353 \S+ [=*@] (\S+) :(.*)", line)
        if m:
            chan, names_str = m.group(1).lower(), m.group(2).strip()
            ops = self._chanops.setdefault(chan, set())
            for entry in names_str.split():
                # Strip all prefix chars to get the bare nick
                nick_clean = entry.lstrip("~&@%+")
                if not nick_clean:
                    continue
                # Check if any op-level prefix is present
                prefix = entry[:len(entry) - len(nick_clean)]
                if set(prefix) & {"~", "&", "@"}:
                    ops.add(nick_clean.lower())
            return

        # MODE — track +o/-o, +a/-a, +q/-q to maintain chanop state.
        # Format: :nick!user@host MODE #channel +oq nick1 nick2
        # Mode chars that grant/revoke chanop status: q(owner), a(admin), o(op)
        m = re.match(r":\S+ MODE (\S+) ([+-]\S+)(.*)", line)
        if m:
            chan = m.group(1)
            if not chan.startswith(("#", "&", "+", "!")):
                pass  # User mode, not channel mode — ignore
            else:
                mode_str = m.group(2)
                args     = m.group(3).strip().split() if m.group(3).strip() else []
                chan_l    = chan.lower()
                ops      = self._chanops.setdefault(chan_l, set())
                adding   = True
                arg_idx  = 0
                for ch in mode_str:
                    if ch == "+":
                        adding = True
                    elif ch == "-":
                        adding = False
                    elif ch in ("o", "a", "q"):
                        # These modes all take a nick parameter
                        if arg_idx < len(args):
                            target = args[arg_idx].lower()
                            arg_idx += 1
                            if adding:
                                ops.add(target)
                                log.debug(f"Chanop add: {target} in {chan} (+{ch})")
                            else:
                                ops.discard(target)
                                log.debug(f"Chanop remove: {target} in {chan} (-{ch})")
                    elif ch in ("h", "v", "b", "e", "I", "k"):
                        # These modes also take a parameter — consume it
                        arg_idx += 1
                    elif ch == "l" and adding:
                        # +l takes a param, -l does not
                        arg_idx += 1
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

        # JOIN with optional extended-join fields (account, realname)
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
            # Track our own nick changes (NickServ ghost/reclaim, SVSNICK, etc.)
            if old_nick.lower() == self._nick.lower():
                self._nick = new_nick
                log.info(f"Own nick changed: {old_nick} -> {new_nick}")
            self._store.user_rename(old_nick, new_nick, host)
            if old_nick in self._authed:
                self._authed.discard(old_nick)
                self._authed.add(new_nick)
                log.info(f"Auth migrated: {old_nick} -> {new_nick}")
            # Migrate chanop status to new nick
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
            self.dispatch(nick, reply_to, cmd, arg, is_pm)


# ── Interactive console ──────────────────────────────────────────────

_CONSOLE_HELP = """\
  debug [on|off]            global debug toggle
  debug <sub> [off]         per-subsystem debug (e.g. debug weather)
  loglevel [LEVEL]          show or set base level (DEBUG/INFO/WARNING/ERROR)
  loglevel <logger> LEVEL   set a specific logger
  status                    show bot state (nick, channels, modules, log levels)
  shutdown [reason]         graceful shutdown
  quit                      alias for shutdown"""


def _run_console(bot):
    """Stdin command loop.  Runs in a daemon thread; exits when stdin closes."""
    while True:
        try:
            line = input("> ").strip()
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
            print(f"  channels = {', '.join(sorted(bot.active_channels)) or '(none)'}")
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
            bot.graceful_shutdown(f"QUIT :{reason}")

        else:
            print(f"Unknown command: {cmd!r} — type 'help' for commands.")


if __name__ == "__main__":
    import signal
    bot = IRCBot()

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down.")
        bot.graceful_shutdown("QUIT :Caught signal, shutting down")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if not _args.no_console and sys.stdin.isatty():
        threading.Thread(target=_run_console, args=(bot,),
                         daemon=True, name="console").start()
        log.info("Interactive console enabled (type 'help' for commands)")

    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            _shutdown(2, None)
        except SystemExit:
            raise  # Let graceful_shutdown's sys.exit propagate
        except Exception as e:
            log.error(f"Crash: {e} — restart in 30s")
            time.sleep(30)
