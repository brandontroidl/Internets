"""Admin and core IRC command handlers for the Internets bot.

Extracted as a mixin so the IRCBot class stays focused on connection,
dispatch, and state.  All methods receive ``(self, nick, reply_to, arg)``
and are coroutines invoked via the command dispatch system.
"""

from __future__ import annotations

import asyncio
import re
import time
import logging
from typing import Any

from config import (
    cfg, CONFIG_PATH, __version__,
    CMD_PREFIX, MODULES_DIR,
)
from botlog import (
    log_filter, get_hash, apply_debug, apply_loglevel,
)
from hashpw import verify_password
from audit_log import default as _audit

log = logging.getLogger("internets")


class AdminCommandsMixin:
    """All ``cmd_*`` methods for IRCBot.  Mixed in as a base class."""

    # Provided by IRCBot — declared here for type checkers.
    _nick: str
    _authed: dict[str, str]
    _auth_fails: dict[str, tuple[int, float]]
    _auth_lock: Any
    _mod_lock: Any
    _nick_hosts: dict[str, str]
    _modules: dict[str, Any]
    _commands: dict[str, tuple[str, str]]

    _AUTH_CLEANUP_THRESHOLD: int
    _AUTH_MAX_FAILS: int
    _AUTH_LOCKOUT: int

    def preply(self, nick: str, reply_to: str, msg: str) -> None: ...
    def send(self, msg: str, priority: int = 1) -> None: ...
    def is_admin(self, nick: str) -> bool: ...
    def load_module(self, name: str) -> tuple[bool, str]: ...
    def unload_module(self, name: str) -> tuple[bool, str]: ...
    def reload_module(self, name: str) -> tuple[bool, str]: ...
    def request_shutdown(self, reason: str = "Shutting down") -> None: ...

    # ── Helpers ──────────────────────────────────────────────────────

    def _require_admin(self, nick: str, reply_to: str) -> bool:
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: auth first — /MSG {self._nick} AUTH <pw>")
            return False
        return True

    def _audit(self, nick: str, action: str, args: object = None) -> None:
        """Append an audit record for a completed admin action.

        Resolves the actor's hostmask from ``self._nick_hosts`` defensively
        (empty string if not tracked).  Audit-log failures must never
        break the admin command, so all exceptions are caught and merely
        warned.
        """
        try:
            hostmask = self._nick_hosts.get(nick.lower(), "")
            _audit().record(nick, hostmask, action, args)
        except Exception as e:
            log.warning(f"audit_log record failed: {e}")

    # ── Auth ─────────────────────────────────────────────────────────

    async def cmd_auth(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Authenticate as bot admin.  PM only.  Brute-force lockout after 5 failures.

        Security notes:
        - The password value is never logged (only its presence / length).
        - Lockout window is refreshed by any attempt while locked, so an
          attacker can't trickle in one attempt per lockout period.
        - On success the auth is bound to the nick AND the current
          hostmask; ``is_admin`` re-checks the hostmask on every call
          (see internets.IRCBot.is_admin).
        - verify_password runs in a thread; we catch only the documented
          ValueError (config error) and swallow any other backend error
          as a generic failure to avoid leaking timing-distinguishable
          exception text.
        """
        h = get_hash()
        if not h:
            self.preply(nick, reply_to, f"{nick}: no password_hash configured — run hashpw.py")
            return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: /MSG {self._nick} AUTH <password>")
            return
        if len(arg) > 128:
            self.preply(nick, reply_to, f"{nick}: password too long.")
            return

        k = nick.lower()
        now = time.time()
        with self._auth_lock:
            if len(self._auth_fails) > self._AUTH_CLEANUP_THRESHOLD:
                self._auth_fails = {
                    n: (f, t) for n, (f, t) in self._auth_fails.items()
                    if now - t < self._AUTH_LOCKOUT
                }
            fails, last_t = self._auth_fails.get(k, (0, 0))
            if now - last_t > self._AUTH_LOCKOUT:
                fails = 0
            if fails >= self._AUTH_MAX_FAILS:
                remaining = int(self._AUTH_LOCKOUT - (now - last_t))
                # Refresh the timer so trickled attempts keep the lockout
                # alive (sliding window) — prevents one-attempt-per-window
                # bypass of the rate limit.
                self._auth_fails[k] = (fails, now)
                hm = self._nick_hosts.get(k, "unknown")
                self.preply(nick, reply_to,
                    f"{nick}: too many failed attempts — try again in {remaining}s")
                log.warning(f"Auth lockout: {nick} ({hm}) {fails} failures")
                return

        try:
            ok = await asyncio.to_thread(verify_password, arg.strip(), h)
        except ValueError as e:
            # ValueError comes from hashpw for known config issues
            # ("No password hash configured" / "Unrecognised hash format"
            # / "bcrypt not installed").  These do NOT contain the
            # password — safe to log the message text.
            log.error(f"Auth config error for {nick}: {e}")
            self.preply(nick, reply_to, f"{nick}: config error — see log for details.")
            return
        except Exception as e:
            # Defence in depth: argon2/scrypt/bcrypt backends should
            # already return False on bad input, but if any of them
            # raises an unexpected error we MUST treat it as a failed
            # attempt and never include str(e) in the log (some backends
            # echo partial input or hash fragments in exception text).
            log.error(f"Auth backend error for {nick}: {type(e).__name__}")
            with self._auth_lock:
                self._auth_fails[k] = (fails + 1, now)
            self.preply(nick, reply_to, f"{nick}: wrong password.")
            return
        if ok:
            hostmask = self._nick_hosts.get(k, "unknown")
            with self._auth_lock:
                self._auth_fails.pop(k, None)
                self._authed[k] = hostmask
            self.preply(nick, reply_to, f"{nick}: authenticated.")
            log.info(f"Auth granted: {nick} ({hostmask})")
            # Never pass the password (or any derivative) as args.
            self._audit(nick, "auth", None)
        else:
            with self._auth_lock:
                self._auth_fails[k] = (fails + 1, now)
            hm = self._nick_hosts.get(k, "unknown")
            self.preply(nick, reply_to, f"{nick}: wrong password.")
            log.warning(f"Failed auth: {nick} ({hm}) {fails + 1}/{self._AUTH_MAX_FAILS}")

    async def cmd_deauth(self, nick: str, reply_to: str, arg: str | None) -> None:
        """End the current admin session."""
        ended = False
        with self._auth_lock:
            if nick.lower() in self._authed:
                del self._authed[nick.lower()]
                ended = True
        if ended:
            self.preply(nick, reply_to, f"{nick}: session ended.")
            self._audit(nick, "deauth", None)
        else:
            self.preply(nick, reply_to, f"{nick}: not authenticated.")

    # ── Info ─────────────────────────────────────────────────────────

    async def cmd_help(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display available commands.  Admin commands visible only when authed."""
        p = CMD_PREFIX
        lines = [
            f"── {self._nick} v{__version__} ──────────────────────────────────────────",
            f"  {p}help  {p}modules  {p}version  {p}auth <pw>",
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
        hidden: list[str] = []
        for name, inst in module_items:
            # Skip modules that loaded but aren't usable (no API key etc.).
            # Keeps `.help` compact and avoids advertising commands that
            # will just say "not configured" if invoked.
            if not inst.is_configured():
                hidden.append(name)
                continue
            hl = inst.help_lines(p)
            if hl:
                lines.append(f"  [{name}]")
                lines.extend(hl)
        if hidden and self.is_admin(nick):
            lines.append(f"  (hidden, no key: {', '.join(sorted(hidden))})")
        lines.append(f"  In PM the '{p}' prefix is optional.")
        for line in lines:
            self.preply(nick, reply_to, line)

    async def cmd_version(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display bot version and repository URL."""
        self.preply(nick, reply_to,
            f"Internets {__version__} — async modular IRC bot  "
            f"https://github.com/brandontroidl/Internets")

    async def cmd_modules(self, nick: str, reply_to: str, arg: str | None) -> None:
        """List loaded and available modules with per-module command counts."""
        with self._mod_lock:
            loaded_items = list(self._modules.items())
        if loaded_items:
            # Each module: name (N cmds)
            parts = []
            for name, inst in loaded_items:
                n = len(getattr(inst, "COMMANDS", {}))
                parts.append(f"{name} ({n})")
            self.preply(nick, reply_to,
                f"Loaded ({len(loaded_items)}): {', '.join(parts)}")
        else:
            self.preply(nick, reply_to, "No modules loaded.")
        loaded_names = {n for n, _ in loaded_items}
        avail = sorted(
            p.stem for p in MODULES_DIR.glob("*.py")
            if p.stem not in ("__init__", "base", "geocode", "units")
            and p.stem not in loaded_names
        )
        if avail:
            self.preply(nick, reply_to, f"Available: {', '.join(avail)}")
        self.preply(nick, reply_to,
            f"Use {CMD_PREFIX}help to see commands grouped by module.")

    # ── Module management ────────────────────────────────────────────

    async def cmd_load(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}load <module>"); return
        mod = arg.strip().lower()
        _, msg = self.load_module(mod)
        self.preply(nick, reply_to, msg)
        self._audit(nick, "load", mod)

    async def cmd_unload(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}unload <module>"); return
        mod = arg.strip().lower()
        _, msg = self.unload_module(mod)
        self.preply(nick, reply_to, msg)
        self._audit(nick, "unload", mod)

    async def cmd_reload(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        if not arg:
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}reload <module>"); return
        mod = arg.strip().lower()
        _, msg = self.reload_module(mod)
        self.preply(nick, reply_to, msg)
        self._audit(nick, "reload", mod)

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
        self._audit(nick, "reloadall", None)

    async def cmd_restart(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        self.preply(nick, reply_to, "Restarting ...")
        log.info(f"Restart by {nick}")
        # Record before request_shutdown — once shutdown begins the
        # process may not get another chance to flush an audit write.
        self._audit(nick, "restart", None)
        self._restart_flag = True
        self.request_shutdown("Restarting ...")

    async def cmd_rehash(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Reload config + config.local overlay and clear admin sessions.  Admin only."""
        if not self._require_admin(nick, reply_to): return
        try:
            # reload_config() re-reads BOTH config.ini AND config.local.ini.
            # Re-reading the template alone would clobber the overlay's
            # password_hash with its empty placeholder.
            from config import reload_config
            reload_config()
        except Exception as e:
            log.error(f"Rehash config read failed: {e}")
            self.preply(nick, reply_to, f"{nick}: failed to read config — see log for details.")
            return

        new_level = cfg["logging"].get("level", "INFO").upper()
        lvl = getattr(logging, new_level, None)
        if lvl:
            log_filter.set_base_level(lvl)
            log_filter.global_debug = False
            log_filter.clear_subsystems()
            self.preply(nick, reply_to, f"Log level: {new_level}")

        h = get_hash()
        if not h:
            self.preply(nick, reply_to, "Config reloaded — no password_hash set.")
        else:
            # Tight prefix match: must be exactly one of the three known
            # algorithms followed by '$'.  Reject anything else without
            # echoing the (potentially attacker-supplied) value back.
            prefix = h.split("$", 1)[0] if "$" in h else ""
            if prefix not in ("scrypt", "bcrypt", "argon2"):
                self.preply(nick, reply_to,
                    "Bad password_hash format — run hashpw.py.")
                log.error(f"Rehash: invalid hash prefix (len={len(prefix)})")
                return
            self.preply(nick, reply_to, f"Config reloaded — {prefix} hash active.")
        with self._auth_lock:
            n = len(self._authed)
            self._authed.clear()
        if n:
            self.preply(nick, reply_to, f"Cleared {n} admin session(s) — re-authenticate.")
        log.info(f"Rehash by {nick}")
        self._audit(nick, "rehash", None)

    # ── IRC oper / modes ─────────────────────────────────────────────

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
        self._audit(nick, "mode", mode_str)

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
        self._audit(nick, "snomask", mask)

    # ── Logging ──────────────────────────────────────────────────────

    async def cmd_loglevel(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        parts = arg.strip().split() if arg else []
        reply_fn = lambda msg: self.preply(nick, reply_to, msg)
        if not parts:
            self.preply(nick, reply_to, "Log levels:")
        err = apply_loglevel(parts, reply_fn)
        if err:
            self.preply(nick, reply_to, f"{nick}: {err}")
        elif parts:
            log.info(f"Log level changed by {nick}: {' '.join(parts)}")
            # Record level + optional logger name.  No parts = read-only
            # listing, no error = applied successfully.
            self._audit(nick, "loglevel", " ".join(parts))

    async def cmd_debug(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        parts = arg.strip().lower().split() if arg else []
        reply_fn = lambda msg: self.preply(nick, reply_to, msg)
        apply_debug(parts, reply_fn)
        log.info(f"Debug changed by {nick}: {' '.join(parts) or 'on'}")
        self._audit(nick, "debug", " ".join(parts) or "on")

    # ── Shutdown ─────────────────────────────────────────────────────

    async def cmd_shutdown(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._require_admin(nick, reply_to): return
        reason = arg.strip() if arg else "Shutting down"
        self.preply(nick, reply_to, f"Shutting down: {reason}")
        log.info(f"Shutdown by {nick}: {reason}")
        # Record before request_shutdown — once the shutdown begins the
        # process may not get another chance to flush an audit write.
        # Log only the supplied reason (not the default placeholder).
        self._audit(nick, "shutdown", arg.strip() if arg else None)
        self.request_shutdown(reason)
