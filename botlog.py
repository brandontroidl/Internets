"""Logging infrastructure: safe formatter, debug filter, setup, and helpers.

Initializes the ``internets`` logger at import time.  Exports ``log``
(the root logger), ``log_filter`` (the debug filter instance), and the
``apply_debug`` / ``apply_loglevel`` helpers shared by IRC admin
commands and the interactive console.
"""

from __future__ import annotations

import re
import sys
import os
import logging
import logging.handlers
import threading
from typing import Any

from config import (
    cfg, CONFIG_PATH, __version__, cli_args,
    LOG_LEVEL, LOG_FILE, LOG_MAX, LOG_BACKUPS, LOG_DEBUG, LOG_FMT,
    USER_MODES, OPER_MODES, OPER_SNOMASK,
)
from hashpw import verify_password  # noqa: F401 — re-exported for internets.py


# ── Formatter and filter ─────────────────────────────────────────────

class _SafeFormatter(logging.Formatter):
    """Formatter that strips CR/LF/NUL from user-controlled log data.

    Sanitizes record.msg and record.args to prevent log injection via
    format-string interpolation (e.g. ``log.info("cmd: %s", attacker_input)``).
    Works on a *copy* of the record so other handlers see the original.
    Exception tracebacks (which naturally contain newlines) are preserved.
    """

    _CONTROL_RE = re.compile(r"[\r\n\x00]")

    def _clean(self, val: Any) -> Any:
        return self._CONTROL_RE.sub("", val) if isinstance(val, str) else val

    def format(self, record: logging.LogRecord) -> str:
        safe = logging.makeLogRecord(record.__dict__)
        safe.msg = self._clean(str(safe.msg))
        if safe.args:
            if isinstance(safe.args, dict):
                safe.args = {k: self._clean(v) for k, v in safe.args.items()}
            elif isinstance(safe.args, tuple):
                safe.args = tuple(self._clean(a) for a in safe.args)
        return super().format(safe)


class DebugFilter(logging.Filter):
    """Attached to main-log and console handlers.  Passes a record if:
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


# ── Setup ────────────────────────────────────────────────────────────

def _setup_logging() -> DebugFilter:
    """Configure the internets logger with rotating file + console + optional debug file."""
    root = logging.getLogger("internets")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fmt  = _SafeFormatter(LOG_FMT)
    filt = DebugFilter(getattr(logging, LOG_LEVEL, logging.INFO))

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


log_filter = _setup_logging()
log = logging.getLogger("internets")
log.info(f"Internets v{__version__} starting")

# Apply CLI --debug flags.
if cli_args.debug is not None:
    if len(cli_args.debug) == 0:
        log_filter.global_debug = True
        log.info("CLI: global debug enabled")
    else:
        for sub in cli_args.debug:
            full = f"internets.{sub}" if not sub.startswith("internets") else sub
            logging.getLogger(full).setLevel(logging.DEBUG)
            log_filter.add_subsystem(full)
            log.info(f"CLI: debug enabled for {full}")


# ── Startup validation ───────────────────────────────────────────────

def get_hash() -> str:
    """Re-read config.ini and return the current password_hash."""
    cfg.read(CONFIG_PATH)
    return cfg["admin"].get("password_hash", "").strip()


def _validate_hash() -> None:
    h = get_hash()
    if not h:
        log.warning("No password_hash in config.ini — auth disabled. Run hashpw.py.")
        return
    prefix = h.split("$")[0] if "$" in h else ""
    if prefix not in ("scrypt", "bcrypt", "argon2"):
        log.critical(f"Invalid password_hash prefix '{prefix}' — run hashpw.py and restart.")
        sys.exit(1)
    log.info(f"Admin password hash loaded ({prefix}).")


_validate_hash()

# BUG-029: Warn if config file is world-readable (contains credentials).
if os.name == "posix":
    try:
        _cfg_stat = os.stat(CONFIG_PATH)
        if _cfg_stat.st_mode & 0o004:
            log.warning("config.ini is world-readable — consider: chmod 640 config.ini")
    except OSError:
        pass

_MODE_VALID = re.compile(r"^[a-zA-Z+\- ]*$")
for _name, _val in [("user_modes", USER_MODES), ("oper_modes", OPER_MODES),
                     ("oper_snomask", OPER_SNOMASK)]:
    if _val and not _MODE_VALID.match(_val):
        log.critical(f"Invalid {_name} = {_val!r} in config.ini — "
                     f"only letters, +, -, and spaces allowed.")
        sys.exit(1)
    if _val:
        log.info(f"Config {_name} = {_val}")


# ── Shared debug/loglevel helpers ────────────────────────────────────

VALID_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


def apply_debug(args: list[str], reply: Any = print) -> None:
    """Apply a debug command.  *reply* is print() for console or a callback."""
    if not args or args[0] == "on":
        log_filter.global_debug = True
        reply("Debug output ON (all subsystems)")
        return
    if args[0] == "off":
        log_filter.global_debug = False
        log_filter.clear_subsystems()
        reply(f"Debug output OFF (back to {LOG_LEVEL})")
        return
    sub = args[0] if args[0].startswith("internets") else f"internets.{args[0]}"
    if len(args) >= 2 and args[1] == "off":
        logging.getLogger(sub).setLevel(logging.NOTSET)
        log_filter.remove_subsystem(sub)
        reply(f"{sub} debug OFF")
    else:
        logging.getLogger(sub).setLevel(logging.DEBUG)
        log_filter.add_subsystem(sub)
        reply(f"{sub} debug ON")


def apply_loglevel(args: list[str], reply: Any = print) -> str | None:
    """Apply a loglevel command.  Returns error string or None on success."""
    if not args:
        lvl_name = logging.getLevelName(log_filter.base_level)
        reply(f"  base level = {lvl_name}")
        if log_filter.global_debug:
            reply("  global debug = ON")
        active = log_filter.active_subsystems()
        if active:
            reply(f"  debug subsystems: {', '.join(sorted(active))}")
        if LOG_DEBUG:
            reply(f"  debug file = {LOG_DEBUG}")
        return None

    if len(args) == 1:
        level = args[0].upper()
        if level not in VALID_LEVELS:
            return f"Invalid level — use: {', '.join(VALID_LEVELS)}"
        log_filter.set_base_level(getattr(logging, level))
        log_filter.global_debug = False
        reply(f"Base level set to {level}")
        return None

    if len(args) == 2:
        target, level = args[0], args[1].upper()
        if not target.startswith("internets"):
            return "Logger must start with 'internets'"
        full = target if "." in target else f"internets.{target}"
        if level == "DEBUG":
            logging.getLogger(full).setLevel(logging.DEBUG)
            log_filter.add_subsystem(full)
            reply(f"{full} = DEBUG")
        elif level == "NOTSET":
            logging.getLogger(full).setLevel(logging.NOTSET)
            log_filter.remove_subsystem(full)
            reply(f"{full} = NOTSET (inherits parent)")
        elif level in VALID_LEVELS:
            logging.getLogger(full).setLevel(getattr(logging, level))
            log_filter.remove_subsystem(full)
            reply(f"{full} = {level}")
        else:
            return f"Invalid level — use: {', '.join(VALID_LEVELS)} or NOTSET"
        return None

    return "usage: loglevel [LEVEL | <logger> LEVEL]"
