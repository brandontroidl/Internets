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
        """Compact command index; `.help <cmd>` or `.help <module>` for details."""
        p = CMD_PREFIX
        admin = self.is_admin(nick)
        with self._mod_lock:
            module_items = list(self._modules.items())

        if arg:
            target = arg.strip().split()[0].lower()
            if target.startswith(p):
                target = target[len(p):]
            for name, inst in module_items:
                if not inst.is_configured() and not admin:
                    continue
                cmds = {k.lower(): k for k in getattr(inst, "COMMANDS", {})}
                if target in cmds:
                    cmd = cmds[target]
                    hl = inst.help_lines(p)
                    match_prefix = f"{p}{cmd}"
                    matched = [
                        ln for ln in hl
                        if ln.lstrip().split(None, 1)[0:1] == [match_prefix]
                        or ln.lstrip().startswith(f"{match_prefix}/")
                        or ln.lstrip().startswith(f"{match_prefix} ")
                    ]
                    if not matched:
                        matched = hl
                    for ln in matched:
                        self.preply(nick, reply_to, ln)
                    return
            for name, inst in module_items:
                if name.lower() == target:
                    if not inst.is_configured() and not admin:
                        break
                    hl = inst.help_lines(p)
                    self.preply(nick, reply_to, f"\x02[{name}]\x02")
                    for ln in hl:
                        self.preply(nick, reply_to, ln)
                    return
            self.preply(nick, reply_to,
                f"no command '{target}' loaded — try {p}help")
            return

        # IRC server /HELP-style two-block layout: decorative banner,
        # centred title, separator, 4-column grid of UPPERCASE commands,
        # then (for admins only) a second banner block with admin-only
        # commands.  Layout intentionally mimics ProvisionIRCd / classic
        # ircd-hybrid /HELP output.

        # Collect every PUBLIC command name: core public (help, modules,
        # version, auth) + the canonical alias of every loaded module
        # whose ``is_configured()`` is True (admins see un-configured ones
        # too, since they may be hot-loading a key in).
        public: list[str] = ["help", "modules", "version", "auth"]
        hidden: list[str] = []
        for name, inst in module_items:
            cmds_map = getattr(inst, "COMMANDS", {})
            if not cmds_map:
                continue
            if not inst.is_configured() and not admin:
                hidden.append(name)
                continue
            if not inst.is_configured():
                hidden.append(name)
            # Canonical alias = FIRST entry in the module's COMMANDS dict.
            by_method: dict[str, str] = {}
            for cmd, method in cmds_map.items():
                if method not in by_method:
                    by_method[method] = cmd
            public.extend(by_method.values())

        # Admin-only commands — listed flat (not module-grouped).
        admin_cmds_list: list[str] = []
        if admin:
            admin_cmds_list = [
                "deauth", "load", "unload", "reload", "reloadall",
                "restart", "rehash",
                "mode", "snomask", "raw", "nick", "say", "act",
                "shutdown", "loglevel", "debug",
                "uptime", "stats", "audit", "fingerprint",
                "shadow-ban", "shadow-unban", "shadow-list",
            ]

        BANNER = "§~¤§¤~~¤§¤~~¤§¤~~¤§¤~~¤§¤~~¤§¤~~¤§¤~~¤§¤~~¤§¤~§"
        TITLE  = f"~~~~~~~~~ Internets v{__version__} Help ~~~~~~~~~"
        ADMIN_TITLE = f"~~~~~~~~~ Internets v{__version__} Help (admin) ~~~~~~~~~"

        out: list[str] = []
        # ── public block ──────────────────────────────────────────────
        out.append(f"* {BANNER}")
        out.append(f"* {TITLE}")
        out.append(f"* {BANNER}")
        out.append("* -")
        for row in _help_grid(sorted(set(public))):
            out.append(f"* {row}")
        out.append("* -")
        out.append(f"* Use {p}help <command> for more information, if available.")

        # ── admin block (only if authed) ──────────────────────────────
        if admin and admin_cmds_list:
            out.append("* -")
            out.append("* -")
            out.append(f"* {BANNER}")
            out.append(f"* {ADMIN_TITLE}")
            out.append(f"* {BANNER}")
            out.append("* -")
            for row in _help_grid(sorted(set(admin_cmds_list))):
                out.append(f"* {row}")
            if hidden:
                out.append("* -")
                out.append(f"* (hidden, no key: {', '.join(sorted(set(hidden)))})")

        for line in out:
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

    async def cmd_raw(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Inject a raw IRC protocol line.  ADMIN ONLY.  Audit-logged."""
        if not self._require_admin(nick, reply_to): return
        if not arg or not arg.strip():
            self.preply(nick, reply_to,
                f"usage: {CMD_PREFIX}raw <IRC line>  e.g. {CMD_PREFIX}raw WHOIS alice")
            return
        line = arg.strip()
        if any(c in line for c in ("\r", "\n", "\x00")):
            self.preply(nick, reply_to, f"{nick}: line contains CR/LF/NUL — rejected.")
            return
        if len(line.encode("utf-8", errors="replace")) > 510:
            self.preply(nick, reply_to, f"{nick}: line exceeds 510 bytes — rejected.")
            return
        self.send(line)
        self.preply(nick, reply_to, f">> {line}")
        log.info(f"Raw line sent by {nick}: {line!r}")
        self._audit(nick, "raw", line)

    # ── Speech / nick ───────────────────────────────────────────────

    def _split_target_and_text(self, arg: str | None, reply_to: str
                               ) -> tuple[str | None, str | None]:
        """Parse "[target] <text>" — if first token looks like a target use it,
        else fall back to ``reply_to`` (the channel/user the command was invoked in)."""
        if not arg or not arg.strip():
            return None, None
        parts = arg.strip().split(None, 1)
        first = parts[0]
        looks_like_target = (
            first.startswith(("#", "&", "+", "!"))
            or (re.match(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,29}$", first)
                and len(parts) > 1)
        )
        if looks_like_target and len(parts) > 1:
            return first, parts[1].strip()
        return reply_to, arg.strip()

    async def cmd_say(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Speak as the bot.  Usage: .say [target] <text> — target defaults to current channel."""
        if not self._require_admin(nick, reply_to): return
        target, text = self._split_target_and_text(arg, reply_to)
        if not target or not text:
            self.preply(nick, reply_to,
                f"usage: {CMD_PREFIX}say [target] <text>  (target defaults to current channel)")
            return
        if "," in target or " " in target:
            self.preply(nick, reply_to, f"{nick}: invalid target — no spaces or commas allowed.")
            return
        self.privmsg(target, text)
        log.info(f".say by {nick} → {target}: {text!r}")
        self._audit(nick, "say", {"target": target, "text": text})

    async def cmd_act(self, nick: str, reply_to: str, arg: str | None) -> None:
        """CTCP ACTION (/me) as the bot.  Usage: .act [target] <text>."""
        if not self._require_admin(nick, reply_to): return
        target, text = self._split_target_and_text(arg, reply_to)
        if not target or not text:
            self.preply(nick, reply_to,
                f"usage: {CMD_PREFIX}act [target] <text>  (target defaults to current channel)")
            return
        if "," in target or " " in target:
            self.preply(nick, reply_to, f"{nick}: invalid target — no spaces or commas allowed.")
            return
        # CTCP ACTION = wrap text in \x01ACTION ...\x01 inside a PRIVMSG.
        self.privmsg(target, f"\x01ACTION {text}\x01")
        log.info(f".act by {nick} → {target}: {text!r}")
        self._audit(nick, "act", {"target": target, "text": text})

    async def cmd_nick(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Change the bot's nickname.  Usage: .nick <newnick>.  Audit-logged."""
        if not self._require_admin(nick, reply_to): return
        if not arg or not arg.strip():
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}nick <newnick>")
            return
        new = arg.strip().split()[0]
        # RFC 2812 — first char letter or special, then up to 29 of letter/digit/special/-.
        # Cap at 30 to be safe across networks that allow shorter.
        if not re.match(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,29}$", new):
            self.preply(nick, reply_to,
                f"{nick}: invalid nick — must start with a letter and use IRC-legal chars only.")
            return
        if new == self._nick:
            self.preply(nick, reply_to, f"{nick}: already using that nick.")
            return
        self.send(f"NICK {new}")
        # Local _nick update happens when the server confirms via the NICK echo
        # (see line ~846 in internets.py).  We don't change it pre-emptively to
        # avoid divergence if the server rejects the change (e.g. 432/433/437).
        self.preply(nick, reply_to, f">> NICK {new}  (waiting for server confirmation)")
        log.info(f"Nick change requested by {nick}: {self._nick} → {new}")
        self._audit(nick, "nick", {"from": self._nick, "to": new})

    # ── Diagnostics ─────────────────────────────────────────────────

    async def cmd_uptime(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show bot process uptime and current-connection uptime."""
        if not self._require_admin(nick, reply_to): return
        now = time.time()
        boot = getattr(self, "_stats_boot_ts", now)
        conn = getattr(self, "_stats_connect_ts", None)
        proc_age = _humanize_delta(now - boot)
        boot_iso = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(boot))
        if conn:
            conn_age = _humanize_delta(now - conn)
            conn_msg = f"connected {conn_age}"
        else:
            conn_msg = "not connected"
        self.preply(nick, reply_to,
            f"process up \x02{proc_age}\x02 since {boot_iso}  |  {conn_msg}")

    async def cmd_stats(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Bot runtime stats: counters, queue depth, memory, audit log size."""
        if not self._require_admin(nick, reply_to): return
        now = time.time()
        boot = getattr(self, "_stats_boot_ts", now)
        conn = getattr(self, "_stats_connect_ts", None)
        cmd_n  = getattr(self, "_stats_cmd_count", 0)
        in_n   = getattr(self, "_stats_msg_in",  0)
        out_n  = getattr(self, "_stats_msg_out", 0)

        # Sender queue depth (asyncio.PriorityQueue.qsize() is approximate
        # but adequate for an ops-facing counter).
        q_depth: int | str = "n/a"
        sender = getattr(self, "_sender", None)
        if sender is not None and getattr(sender, "_q", None) is not None:
            try:
                q_depth = sender._q.qsize()
            except Exception:
                pass

        with self._mod_lock:
            mod_total = len(self._modules)
            mod_configured = sum(1 for inst in self._modules.values()
                                 if inst.is_configured())
        chan_n = len(self.active_channels)
        rss_kb = _read_rss_kb()
        rss_s  = f"{rss_kb / 1024:.1f} MiB" if rss_kb else "n/a"

        try:
            audit_n: int | str = _audit().count()
        except Exception:
            audit_n = "n/a"

        proc_age = _humanize_delta(now - boot)
        conn_age = _humanize_delta(now - conn) if conn else "—"

        lines = [
            f"── \x02stats\x02 ─────────────────────────────────────────",
            f"  uptime          process \x02{proc_age}\x02 / conn \x02{conn_age}\x02",
            f"  modules         {mod_configured} configured / {mod_total} loaded "
                f"|  channels {chan_n}",
            f"  traffic         cmds \x02{cmd_n}\x02  |  PRIVMSG in \x02{in_n}\x02  "
                f"out \x02{out_n}\x02",
            f"  send queue      {q_depth} / {getattr(sender, 'MAX_QUEUE', '?') if sender else '?'} slots",
            f"  audit log       {audit_n} records",
            f"  memory (RSS)    {rss_s}",
        ]
        for line in lines:
            self.preply(nick, reply_to, line)

    async def cmd_audit(self, nick: str, reply_to: str, arg: str | None) -> None:
        """View the audit log.  ``.audit [N|grep <pattern>]`` (default last 10)."""
        if not self._require_admin(nick, reply_to): return
        try:
            audit = _audit()
        except Exception as e:
            self.preply(nick, reply_to, f"{nick}: audit log unavailable: {e!r}")
            return
        path = audit.path
        if not path.exists():
            self.preply(nick, reply_to, "audit log is empty (no records yet).")
            return

        n = 10
        pattern: str | None = None
        if arg and arg.strip():
            parts = arg.strip().split(None, 1)
            head = parts[0].lower()
            if head == "grep" and len(parts) > 1:
                pattern = parts[1].strip()
                n = 50
            elif head.isdigit():
                n = min(max(int(head), 1), 200)
            elif head == "tail":
                n = 5
            elif head == "verify":
                ok, idx = audit.verify()
                if ok:
                    self.preply(nick, reply_to,
                        f"audit chain intact ({audit.count()} records).")
                else:
                    self.preply(nick, reply_to,
                        f"\x02audit chain BROKEN\x02 at record index {idx}.")
                return
            else:
                self.preply(nick, reply_to,
                    f"usage: {CMD_PREFIX}audit [N | grep <pattern> | tail | verify]")
                return

        # Read all records (audit log files are small — append-only admin ops).
        try:
            with path.open("r", encoding="utf-8") as f:
                entries = [_audit_parse(line) for line in f if line.strip()]
        except OSError as e:
            self.preply(nick, reply_to, f"audit log read failed: {type(e).__name__}")
            return
        entries = [e for e in entries if e is not None]

        if pattern:
            pat = pattern.lower()
            matched = [e for e in entries if pat in _audit_haystack(e).lower()]
            tail = matched[-n:]
            header = f"── audit grep \x02{pattern}\x02 — {len(matched)} match(es), showing last {len(tail)} ──"
        else:
            tail = entries[-n:]
            header = f"── audit log — last {len(tail)} of {len(entries)} ──"

        self.preply(nick, reply_to, header)
        if not tail:
            self.preply(nick, reply_to, "  (no matching entries)")
            return
        for e in tail:
            self.preply(nick, reply_to, _audit_format(e))

    # ── Cross-reference / moderation ────────────────────────────────

    async def cmd_fingerprint(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Cross-reference everything the bot knows about a nick: hostmask,
        channels, last seen, tells, notes, audit mentions, shadow-ban status."""
        if not self._require_admin(nick, reply_to): return
        if not arg or not arg.strip():
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}fingerprint <nick>")
            return
        target = arg.strip().split()[0]
        tlow = target.lower()

        lines = [f"── \x02fingerprint:\x02 {target} ──"]

        host = self._nick_hosts.get(tlow)
        lines.append(f"  hostmask        {host or '(unknown — not seen this session)'}")

        # Channels currently tracked as containing this nick.  Stored as
        # part of the per-channel user tracking on self._store.
        chans: list[str] = []
        try:
            for ch in sorted(self.active_channels):
                users = self._store.channel_users(ch) if hasattr(self._store, "channel_users") else {}
                if tlow in {u.lower() for u in users}:
                    chans.append(ch)
        except Exception:
            pass
        lines.append(f"  in channels     {', '.join(chans) if chans else '(none currently tracked)'}")

        # Shadow-ban status
        banned = tlow in getattr(self, "_shadow_bans", set())
        ban_reason = getattr(self, "_shadow_ban_reasons", {}).get(tlow, "")
        if banned:
            lines.append(f"  \x0304shadow-banned\x03  {ban_reason or '(no reason recorded)'}")
        else:
            lines.append(f"  shadow-banned   no")

        # Seen module data — read seen.json if present
        seen_path = _state_file(self.cfg, "seen", "seen.json")
        seen_entry = _read_json_dict(seen_path).get(tlow)
        if isinstance(seen_entry, dict):
            ts  = seen_entry.get("ts", 0)
            ev  = seen_entry.get("event", "?")
            ch  = seen_entry.get("channel") or "—"
            det = seen_entry.get("detail")  or ""
            age = _humanize_delta(time.time() - float(ts)) if ts else "?"
            det_s = f": {det}" if det else ""
            lines.append(f"  last seen       {age} ago — {ev} in {ch}{det_s}")
        else:
            lines.append(f"  last seen       (no .seen data)")

        # Tell module data — count pending tells TO and FROM this nick
        tell_path = _state_file(self.cfg, "tell", "tells.json")
        tells_db = _read_json_dict(tell_path)
        tells_to = len(tells_db.get(tlow, []))
        tells_from = sum(
            1 for entries in tells_db.values() if isinstance(entries, list)
            for e in entries if isinstance(e, dict)
            and str(e.get("from", "")).lower() == tlow
        )
        lines.append(f"  tells           {tells_to} pending to them, {tells_from} sent by them")

        # Notes count (don't dump content — privacy)
        notes_path = _state_file(self.cfg, "notes", "notes.json")
        notes_db = _read_json_dict(notes_path)
        notes_n = len(notes_db.get(tlow, [])) if isinstance(notes_db.get(tlow), list) else 0
        lines.append(f"  notes           {notes_n} note(s)")

        # Audit log mentions — actor and "args" field
        mentions = _count_audit_mentions(target)
        lines.append(f"  audit mentions  {mentions['as_actor']} as actor, "
                     f"{mentions['in_args']} in args")

        for line in lines:
            self.preply(nick, reply_to, line)

    async def cmd_shadow_ban(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Silently drop ALL traffic from a nick — commands and on_raw delivery.
        Usage: .shadow-ban <nick> [reason].  Audit-logged."""
        if not self._require_admin(nick, reply_to): return
        if not arg or not arg.strip():
            self.preply(nick, reply_to,
                f"usage: {CMD_PREFIX}shadow-ban <nick> [reason]")
            return
        parts = arg.strip().split(None, 1)
        target = parts[0]
        reason = parts[1].strip() if len(parts) > 1 else ""
        tlow = target.lower()
        if tlow == self._nick.lower():
            self.preply(nick, reply_to, f"{nick}: refusing to shadow-ban the bot itself.")
            return
        if tlow == nick.lower():
            self.preply(nick, reply_to, f"{nick}: refusing to shadow-ban yourself.")
            return
        if not hasattr(self, "_shadow_bans"):
            self.preply(nick, reply_to, f"{nick}: shadow-ban store not initialised.")
            return
        if tlow in self._shadow_bans:
            self.preply(nick, reply_to, f"{nick}: {target} is already shadow-banned.")
            return
        self._shadow_bans.add(tlow)
        if reason:
            self._shadow_ban_reasons[tlow] = reason
        self._save_shadow_bans()
        self.preply(nick, reply_to,
            f"\x0304shadow-banned\x03 \x02{target}\x02 — silently ignored from now on.")
        log.info(f"Shadow-ban added by {nick}: {target!r} reason={reason!r}")
        self._audit(nick, "shadow-ban", {"nick": target, "reason": reason})

    async def cmd_shadow_unban(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Lift a shadow-ban.  Usage: .shadow-unban <nick>."""
        if not self._require_admin(nick, reply_to): return
        if not arg or not arg.strip():
            self.preply(nick, reply_to, f"usage: {CMD_PREFIX}shadow-unban <nick>")
            return
        target = arg.strip().split()[0]
        tlow = target.lower()
        if not hasattr(self, "_shadow_bans") or tlow not in self._shadow_bans:
            self.preply(nick, reply_to, f"{nick}: {target} is not shadow-banned.")
            return
        self._shadow_bans.discard(tlow)
        self._shadow_ban_reasons.pop(tlow, None)
        self._save_shadow_bans()
        self.preply(nick, reply_to, f"\x0303unbanned\x03 \x02{target}\x02.")
        log.info(f"Shadow-unban by {nick}: {target!r}")
        self._audit(nick, "shadow-unban", target)

    async def cmd_shadow_list(self, nick: str, reply_to: str, arg: str | None) -> None:
        """List all current shadow-bans."""
        if not self._require_admin(nick, reply_to): return
        bans = sorted(getattr(self, "_shadow_bans", set()))
        if not bans:
            self.preply(nick, reply_to, "no shadow-bans active.")
            return
        self.preply(nick, reply_to,
            f"── \x02shadow-bans\x02 ({len(bans)}) ──")
        for n in bans:
            reason = getattr(self, "_shadow_ban_reasons", {}).get(n, "")
            self.preply(nick, reply_to, f"  \x02{n}\x02  {reason}".rstrip())

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


# ── Module-level helpers used by .help / .uptime / .stats / .audit / .fingerprint ──

def _help_grid(items: list[str], cols: int = 4, col_w: int = 14) -> list[str]:
    """Render ``items`` as an IRC /HELP-style grid of UPPERCASE labels.

    Layout: left-to-right, top-to-bottom, ``cols`` columns of ``col_w``
    chars each (right-padded with spaces; the last column on a line is
    not padded so trailing whitespace doesn't waste bytes).
    Returns a list of rendered rows, one per output line.
    """
    if not items:
        return []
    rows: list[str] = []
    upper = [s.upper() for s in items]
    for i in range(0, len(upper), cols):
        chunk = upper[i:i + cols]
        parts = [c.ljust(col_w) for c in chunk[:-1]]
        parts.append(chunk[-1])
        rows.append("".join(parts))
    return rows




def _humanize_delta(seconds: float) -> str:
    """Render a non-negative time delta as a compact human string."""
    s = max(0, int(seconds))
    if s < 60:  return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        h, rem = divmod(s, 3600); m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    d, rem = divmod(s, 86400); h, _ = divmod(rem, 3600)
    return f"{d}d {h}h"


def _read_rss_kb() -> int | None:
    """Resident set size in KB on Linux (reads /proc/self/status).  None elsewhere."""
    import os as _os
    if _os.name != "posix":
        return None
    try:
        with open("/proc/self/status", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) if len(parts) >= 2 else None
    except (OSError, ValueError):
        return None
    return None


def _audit_parse(line: str) -> dict | None:
    """Parse one JSON-line audit record; return None on garbage."""
    import json as _json
    try:
        obj = _json.loads(line)
        return obj if isinstance(obj, dict) else None
    except _json.JSONDecodeError:
        return None


def _audit_haystack(e: dict) -> str:
    """Flatten an audit entry to one string for ``grep`` matching."""
    args = e.get("args", "")
    if not isinstance(args, str):
        import json as _json
        try:
            args = _json.dumps(args, ensure_ascii=False)
        except Exception:
            args = str(args)
    return " ".join(str(e.get(k, "")) for k in ("ts", "actor", "host", "action")) + " " + args


def _audit_format(e: dict) -> str:
    """One-line summary of an audit record for IRC display."""
    ts = e.get("ts", "?")
    if isinstance(ts, str) and len(ts) >= 19:
        ts = ts[:19].replace("T", " ")
    actor  = e.get("actor",  "?")
    action = e.get("action", "?")
    args   = e.get("args",   "")
    if not isinstance(args, str):
        import json as _json
        try:
            args = _json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            args = str(args)
    if len(args) > 160:
        args = args[:157] + "..."
    return f"  {ts}  \x02{actor}\x02  {action}  {args}".rstrip()


def _state_file(cfg, section: str, default: str):
    """Return the configured path for a module's state file (Path), or ``default``."""
    from pathlib import Path as _Path
    try:
        if section in cfg:
            return _Path(cfg[section].get("file", default))
    except Exception:
        pass
    return _Path(default)


def _read_json_dict(path) -> dict:
    """Load a top-level JSON dict from ``path``; return ``{}`` on any error."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, _json.JSONDecodeError):
        return {}


def _count_audit_mentions(target: str) -> dict:
    """Walk the audit log; count records where ``target`` is the actor and
    where ``target`` appears as a substring (case-insensitive) in the args."""
    audit = _audit()
    if not audit.path.exists():
        return {"as_actor": 0, "in_args": 0}
    target_low = target.lower()
    as_actor = 0
    in_args  = 0
    try:
        with audit.path.open("r", encoding="utf-8") as f:
            for line in f:
                e = _audit_parse(line.strip())
                if not e:
                    continue
                if str(e.get("actor", "")).lower() == target_low:
                    as_actor += 1
                args = e.get("args", "")
                if not isinstance(args, str):
                    import json as _json
                    try:
                        args = _json.dumps(args, ensure_ascii=False)
                    except Exception:
                        args = str(args)
                if target_low in args.lower():
                    in_args += 1
    except OSError:
        pass
    return {"as_actor": as_actor, "in_args": in_args}
