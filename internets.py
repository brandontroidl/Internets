#!/usr/bin/env python3
"""
Internets — modular IRC bot with SSL support and dynamic module loading.

Core commands (always available):
  .help              List all commands from all loaded modules
  .modules           List loaded/available modules
  .auth <password>   Authenticate as admin (PM only)
  .deauth            Drop admin session (PM only)
  .load  <module>    Load a module by name    [admin only]
  .unload <module>   Unload a loaded module   [admin only]
  .reload <module>   Reload a module in-place [admin only]

Modules live in the modules/ directory. Each exposes a setup(bot) function
that returns a BotModule instance. See modules/base.py for the interface.
"""

import ssl
import socket
import time
import threading
import logging
import configparser
import sys
import os
import re
import json
import importlib
import importlib.util
import queue
from pathlib import Path
from hashpw import verify_password

# ─── Config ───────────────────────────────────────────────────────────────────

cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
cfg.read("config.ini")

IRC_SERVER    = cfg["irc"]["server"]
IRC_PORT      = int(cfg["irc"]["port"])
NICKNAME      = cfg["irc"]["nickname"]
REALNAME      = cfg["irc"]["realname"]
NICKSERV_PW   = cfg["irc"].get("nickserv_password", "").strip()
SERVER_PW     = cfg["irc"].get("server_password",   "").strip()
OPER_NAME     = cfg["irc"].get("oper_name",          "").strip()
OPER_PW       = cfg["irc"].get("oper_password",      "").strip()

CMD_PREFIX    = cfg["bot"]["command_prefix"]
API_COOLDOWN    = int(cfg["bot"]["api_cooldown"])
FLOOD_COOLDOWN  = int(cfg["bot"].get("flood_cooldown", "3"))
LOC_FILE      = cfg["bot"].get("locations_file",  "locations.json")
CHANNELS_FILE = cfg["bot"].get("channels_file",   "channels.json")
USERS_FILE    = cfg["bot"].get("users_file",       "users.json")
MODULES_DIR   = Path(cfg["bot"].get("modules_dir", "modules"))
AUTO_LOAD     = [m.strip() for m in cfg["bot"].get("autoload", "").split(",") if m.strip()]

# IRCv3 capabilities we want to request
# The bot functions fine if the server doesn't support any of these
DESIRED_CAPS = {
    "multi-prefix",    # see all channel mode prefixes on a user (@+nick etc)
    "away-notify",     # get AWAY notifications for users in shared channels
    "account-notify",  # get ACCOUNT messages when a user's account changes
    "chghost",         # get CHGHOST when a user's host changes (no fake quit/join)
    "extended-join",   # account name and realname included in JOIN messages
    "server-time",     # message-tags with server timestamps
    "message-tags",    # general message tag support
}

def _get_admin_hash() -> str:
    """Read password_hash fresh from config.ini each time — supports live rehash."""
    cfg.read("config.ini")
    return cfg["admin"].get("password_hash", "").strip()

LOG_LEVEL     = cfg["logging"]["level"]
LOG_FILE      = cfg["logging"]["log_file"]

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("internets")

# ─── Startup hash validation ──────────────────────────────────────────────────

def _validate_hash_on_startup():
    h = _get_admin_hash()
    if not h:
        log.warning(
            "No admin password_hash set in config.ini. "
            "Module management will be disabled. "
            "Run: python hashpw.py  to generate one."
        )
        return
    prefix = h.split("$")[0] if "$" in h else ""
    if prefix not in ("scrypt", "bcrypt", "argon2"):
        log.critical(
            f"Invalid password_hash format in config.ini (got prefix '{prefix}'). "
            "Must start with 'scrypt$', 'bcrypt$', or 'argon2$'. "
            "Run: python hashpw.py  to generate a valid hash."
        )
        sys.exit(1)
    log.info(f"Admin password hash loaded ({prefix}).")

_validate_hash_on_startup()

# ─── Generic JSON store ───────────────────────────────────────────────────────

def _load_json(path: str, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:
        log.warning(f"Load {path}: {e}")
    return default

def _save_json(path: str, data):
    try:
        Path(path).write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning(f"Save {path}: {e}")

# ─── Location store ───────────────────────────────────────────────────────────

_loc_lock = threading.Lock()

def _load_locs() -> dict:
    return _load_json(LOC_FILE, {})

def _save_locs(data: dict):
    _save_json(LOC_FILE, data)

# ─── Persistent channel store ─────────────────────────────────────────────────

_chan_lock = threading.Lock()

def _load_channels() -> list:
    return _load_json(CHANNELS_FILE, [])

def _save_channels(channels: set):
    with _chan_lock:
        _save_json(CHANNELS_FILE, sorted(channels))

# ─── Per-channel user registry ────────────────────────────────────────────────

_users_lock = threading.Lock()

def _load_users() -> dict:
    return _load_json(USERS_FILE, {})

def _save_users(data: dict):
    _save_json(USERS_FILE, data)

def user_join(channel: str, nick: str, hostmask: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _users_lock:
        data = _load_users()
        ch   = data.setdefault(channel.lower(), {})
        entry = ch.setdefault(nick.lower(), {
            "nick": nick, "hostmask": hostmask,
            "first_seen": now, "last_seen": now
        })
        entry["last_seen"] = now
        entry["hostmask"]  = hostmask
        entry["nick"]      = nick
        _save_users(data)

def user_part(channel: str, nick: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _users_lock:
        data = _load_users()
        entry = data.get(channel.lower(), {}).get(nick.lower())
        if entry:
            entry["last_seen"] = now
            _save_users(data)

def user_quit(nick: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _users_lock:
        data    = _load_users()
        updated = False
        for ch in data.values():
            if nick.lower() in ch:
                ch[nick.lower()]["last_seen"] = now
                updated = True
        if updated:
            _save_users(data)

def user_rename(old_nick: str, new_nick: str, hostmask: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _users_lock:
        data    = _load_users()
        updated = False
        for ch in data.values():
            if old_nick.lower() in ch:
                entry = ch.pop(old_nick.lower())
                entry.update({"nick": new_nick, "hostmask": hostmask, "last_seen": now})
                ch[new_nick.lower()] = entry
                updated = True
        if updated:
            _save_users(data)

def channel_users(channel: str) -> dict:
    return _load_users().get(channel.lower(), {})

# ─── Rate limiting ────────────────────────────────────────────────────────────
#
# Two tiers:
#   flood  — all commands:   FLOOD_COOLDOWN seconds (default 3s)  silently dropped
#   api    — api commands:   API_COOLDOWN seconds   (default 10s) notified
#
# _flood_calls  : { nick -> last_any_command_time }
# _api_calls    : { nick -> last_api_command_time }

_rate_lock   = threading.Lock()
_flood_calls: dict = {}
_api_calls:   dict = {}

# ─── IRC Bot ──────────────────────────────────────────────────────────────────

class IRCBot:
    # ── outbound send queue token bucket ──────────────────────────────────
    # Most IRC servers kill with "Excess Flood" if you send too fast.
    # Token bucket: 5 burst tokens, refill 1 token every 1.5 seconds.
    # PONG/QUIT/CAP responses go into a priority queue and bypass the bucket
    # so keepalive and cap negotiation are never delayed.
    _BUCKET_CAPACITY = 5
    _BUCKET_REFILL   = 1.5   # seconds per token

    def __init__(self):
        self.sock               = None
        self._lock              = threading.Lock()
        self.active_channels: set = set()
        self.cfg                = cfg
        self._modules: dict     = {}
        self._commands: dict    = {}
        self._authed_nicks: set = set()
        self._keepalive_stop    = threading.Event()

        # Outbound send queue — (priority, msg)  lower priority = sent first
        # priority 0 = immediate (PONG, QUIT, CAP), 1 = normal
        self._send_q: queue.PriorityQueue = queue.PriorityQueue()
        self._send_counter = 0   # tie-breaker for same-priority items
        self._sender_stop  = threading.Event()
        self._sender_thread: threading.Thread = None

        # IRCv3 cap negotiation state
        self._cap_negotiating = False
        self._caps_acked: set = set()
        self._nick_attempt    = NICKNAME

    # ── public API for modules ─────────────────────────────────────────────

    # Maximum safe message body length.
    # IRC line limit is 512 bytes including CRLF.
    # Overhead worst case: ":nick!user@host PRIVMSG #channel :" ≈ 70 chars.
    # 400 chars leaves headroom for any realistic prefix.
    _MAX_MSG = 400

    def privmsg(self, target: str, msg: str):
        for chunk in self._chunk(msg):
            self.send(f"PRIVMSG {target} :{chunk}")

    def notice(self, target: str, msg: str):
        """Send a NOTICE — for help output and privileged command responses."""
        for chunk in self._chunk(msg):
            self.send(f"NOTICE {target} :{chunk}")

    def _chunk(self, msg: str) -> list:
        """Split message into safe-length pieces (bytes, not chars)."""
        encoded = msg.encode("utf-8", errors="replace")
        pieces = []
        while encoded:
            chunk, encoded = encoded[:self._MAX_MSG], encoded[self._MAX_MSG:]
            pieces.append(chunk.decode("utf-8", errors="replace"))
        return pieces

    def reply(self, nick: str, reply_to: str, msg: str, privileged: bool = False):
        """
        Route a response based on context:
          PM (reply_to == nick):        PRIVMSG to nick
          Channel, regular command:     PRIVMSG to channel
          Channel, privileged command:  NOTICE to nick only
        """
        is_pm = not reply_to.startswith(("#", "&", "+", "!"))
        if is_pm:
            self.privmsg(nick, msg)
        elif privileged:
            self.notice(nick, msg)
        else:
            self.privmsg(reply_to, msg)

    def preply(self, nick: str, reply_to: str, msg: str):
        """Privileged reply shortcut — NOTICE in channel, PRIVMSG in PM."""
        self.reply(nick, reply_to, msg, privileged=True)

    def send(self, msg: str, priority: int = 1):
        """
        Enqueue a raw IRC line.
        priority 0 = immediate (PONG/QUIT/CAP — bypass token bucket)
        priority 1 = normal (subject to token bucket)
        """
        with self._lock:
            self._send_counter += 1
            self._send_q.put((priority, self._send_counter, msg))

    def _send_direct(self, msg: str):
        """Write directly to socket — called only from the sender thread."""
        log.debug(f">> {msg}")
        try:
            self.sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            log.warning(f"Send error: {e}")

    def _sender_loop(self):
        """
        Dedicated thread that drains the send queue through a token bucket.
        Immediate items (priority 0) skip the bucket entirely.
        Normal items (priority 1) consume one token; if empty, wait for refill.
        """
        tokens      = float(self._BUCKET_CAPACITY)
        last_refill = time.monotonic()

        while not self._sender_stop.is_set():
            try:
                priority, _, msg = self._send_q.get(timeout=0.1)
            except queue.Empty:
                # Refill tokens while idle
                now = time.monotonic()
                elapsed = now - last_refill
                tokens = min(self._BUCKET_CAPACITY, tokens + elapsed / self._BUCKET_REFILL)
                last_refill = now
                continue

            now     = time.monotonic()
            elapsed = now - last_refill
            tokens  = min(self._BUCKET_CAPACITY, tokens + elapsed / self._BUCKET_REFILL)
            last_refill = now

            if priority == 0:
                # Immediate — no token cost, no wait
                self._send_direct(msg)
            else:
                # Normal — wait for a token
                while tokens < 1.0 and not self._sender_stop.is_set():
                    time.sleep(0.05)
                    now     = time.monotonic()
                    elapsed = now - last_refill
                    tokens  = min(self._BUCKET_CAPACITY, tokens + elapsed / self._BUCKET_REFILL)
                    last_refill = now
                tokens -= 1.0
                self._send_direct(msg)

    def _start_sender(self):
        """Start (or restart) the sender thread."""
        self._sender_stop.clear()
        self._send_q = queue.PriorityQueue()
        self._send_counter = 0
        t = threading.Thread(target=self._sender_loop, daemon=True, name="sender")
        t.start()
        self._sender_thread = t

    def _stop_sender(self):
        self._sender_stop.set()

    def flood_limited(self, nick: str) -> bool:
        """
        Global per-nick flood gate applied to every command.
        Returns True (drop silently) if nick is sending faster than FLOOD_COOLDOWN.
        Authed admins bypass this entirely — they are never flood-gated.
        Does NOT update the timestamp if limited — lets the timer keep running.
        """
        if self.is_admin(nick):
            return False
        now = time.time()
        with _rate_lock:
            last = _flood_calls.get(nick.lower(), 0)
            if now - last < FLOOD_COOLDOWN:
                return True
            _flood_calls[nick.lower()] = now
        return False

    def rate_limited(self, nick: str) -> bool:
        """
        Per-nick API cooldown for expensive external requests (weather etc).
        Returns True if nick is within API_COOLDOWN of their last API call.
        Always enforced regardless of admin status — respects upstream API ToS.
        Callers are expected to notify the user when True.
        """
        now = time.time()
        with _rate_lock:
            last = _api_calls.get(nick.lower(), 0)
            if now - last < API_COOLDOWN:
                return True
            _api_calls[nick.lower()] = now
        return False

    def loc_get(self, nick: str):
        with _loc_lock:
            return _load_locs().get(nick.lower())

    def loc_set(self, nick: str, raw: str):
        with _loc_lock:
            d = _load_locs(); d[nick.lower()] = raw; _save_locs(d)

    def loc_del(self, nick: str) -> bool:
        with _loc_lock:
            d = _load_locs()
            if nick.lower() in d:
                del d[nick.lower()]; _save_locs(d); return True
            return False

    def channel_users(self, channel: str) -> dict:
        return channel_users(channel)

    # ── module manager ─────────────────────────────────────────────────────

    def load_module(self, name: str) -> tuple:
        if name in self._modules:
            return False, f"Module '{name}' is already loaded."
        mod_path = MODULES_DIR / f"{name}.py"
        if not mod_path.exists():
            return False, f"Module file '{mod_path}' not found."
        try:
            spec     = importlib.util.spec_from_file_location(f"modules.{name}", mod_path)
            module   = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "setup"):
                return False, f"Module '{name}' has no setup() function."
            instance  = module.setup(self)
            conflicts = [
                cmd for cmd in instance.COMMANDS
                if cmd in self._commands and self._commands[cmd][0] != name
            ]
            if conflicts:
                return False, f"Module '{name}' conflicts with: {', '.join(conflicts)}"
            instance.on_load()
            self._modules[name] = instance
            for cmd, method in instance.COMMANDS.items():
                self._commands[cmd] = (name, method)
            log.info(f"Loaded module: {name} (commands: {list(instance.COMMANDS.keys())})")
            return True, f"Module '{name}' loaded ({len(instance.COMMANDS)} commands registered)."
        except Exception as e:
            log.error(f"Failed to load module '{name}': {e}")
            return False, f"Error loading '{name}': {e}"

    def unload_module(self, name: str) -> tuple:
        if name not in self._modules:
            return False, f"Module '{name}' is not loaded."
        try:
            self._modules[name].on_unload()
            for cmd in [c for c, v in self._commands.items() if v[0] == name]:
                del self._commands[cmd]
            del self._modules[name]
            log.info(f"Unloaded module: {name}")
            return True, f"Module '{name}' unloaded."
        except Exception as e:
            log.error(f"Failed to unload '{name}': {e}")
            return False, f"Error unloading '{name}': {e}"

    def reload_module(self, name: str) -> tuple:
        ok, msg = self.unload_module(name)
        if not ok:
            return False, msg
        return self.load_module(name)

    def autoload_modules(self):
        for name in AUTO_LOAD:
            ok, msg = self.load_module(name)
            log.info(msg) if ok else log.warning(f"Autoload failed: {msg}")

    # ── admin auth ─────────────────────────────────────────────────────────

    def is_admin(self, nick: str) -> bool:
        return nick in self._authed_nicks

    def cmd_auth(self, nick: str, reply_to: str, arg):
        h = _get_admin_hash()
        if not h:
            self.preply(nick, reply_to,
                f"{nick}: no password_hash configured. "
                f"Run hashpw.py and set password_hash in config.ini.")
            return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: usage: /MSG {NICKNAME} AUTH <password>")
            return
        try:
            ok = verify_password(arg.strip(), h)
        except ValueError as e:
            self.preply(nick, reply_to, f"{nick}: configuration error — {e}")
            log.error(f"Auth config error: {e}")
            return
        if ok:
            self._authed_nicks.add(nick)
            self.preply(nick, reply_to, f"{nick}: you are now authenticated as admin.")
            log.info(f"Admin auth granted: {nick}")
        else:
            self.preply(nick, reply_to, f"{nick}: incorrect password.")
            log.warning(f"Failed admin auth attempt from {nick}")

    def cmd_deauth(self, nick: str, reply_to: str, arg):
        if nick in self._authed_nicks:
            self._authed_nicks.discard(nick)
            self.preply(nick, reply_to, f"{nick}: admin session ended.")
        else:
            self.preply(nick, reply_to, f"{nick}: you are not authenticated.")

    # ── core commands ──────────────────────────────────────────────────────

    def cmd_help(self, nick: str, reply_to: str, arg):
        p        = CMD_PREFIX
        is_admin = self.is_admin(nick)

        lines = [f"── {NICKNAME} Commands ─────────────────────────────────────────────"]
        lines += [
            f"  {p}help               This message",
            f"  {p}modules            List loaded/available modules",
            f"  {p}auth <pw>          Authenticate as admin (PM only)",
        ]

        # Admin-only commands only shown to authed users
        if is_admin:
            lines += [
                f"  {p}deauth             End admin session (PM only)",
                f"  {p}load      <module>   Load a module        [admin]",
                f"  {p}unload    <module>   Unload a module      [admin]",
                f"  {p}reload    <module>   Reload a module      [admin]",
                f"  {p}reloadall            Reload all modules   [admin]",
                f"  {p}restart              Restart bot process  [admin]",
                f"  {p}rehash               Reload config.ini    [admin]",
            ]

        lines.append("────────────────────────────────────────────────────────────────────")

        for mod_name, instance in self._modules.items():
            mod_lines = instance.help_lines(p)
            if mod_lines:
                lines.append(f"  [{mod_name}]")
                lines.extend(mod_lines)

        lines += [
            f"────────────────────────────────────────────────────────────────────",
            f"  In PM you can drop the '{p}' prefix.",
        ]

        for line in lines:
            self.preply(nick, reply_to, line)

    def cmd_modules(self, nick: str, reply_to: str, arg):
        if self._modules:
            self.preply(nick, reply_to, f"Loaded: {', '.join(self._modules.keys())}")
        else:
            self.preply(nick, reply_to, "No modules currently loaded.")
        available = sorted(
            p.stem for p in MODULES_DIR.glob("*.py")
            if p.stem not in ("__init__", "base") and p.stem not in self._modules
        )
        if available:
            self.preply(nick, reply_to, f"Available: {', '.join(available)}")

    def cmd_load(self, nick: str, reply_to: str, arg):
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: usage: {CMD_PREFIX}load <module>"); return
        _, msg = self.load_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_unload(self, nick: str, reply_to: str, arg):
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: usage: {CMD_PREFIX}unload <module>"); return
        _, msg = self.unload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_reload(self, nick: str, reply_to: str, arg):
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: usage: {CMD_PREFIX}reload <module>"); return
        _, msg = self.reload_module(arg.strip().lower())
        self.preply(nick, reply_to, msg)

    def cmd_reloadall(self, nick: str, reply_to: str, arg):
        """Reload every currently loaded module in sequence."""
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return
        names = list(self._modules.keys())
        if not names:
            self.preply(nick, reply_to, "No modules are loaded."); return
        self.preply(nick, reply_to, f"Reloading {len(names)} module(s): {', '.join(names)}")
        ok_list, fail_list = [], []
        for name in names:
            ok, msg = self.reload_module(name)
            (ok_list if ok else fail_list).append(name)
            log.info(f"reloadall: {msg}")
        parts = []
        if ok_list:
            parts.append(f"OK: {', '.join(ok_list)}")
        if fail_list:
            parts.append(f"FAILED: {', '.join(fail_list)}")
        self.preply(nick, reply_to, " | ".join(parts))

    def cmd_restart(self, nick: str, reply_to: str, arg):
        """
        Full process restart via os.execv — picks up changes to internets.py itself.
        The process replaces itself in-place; the bot will reconnect automatically.
        """
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return
        self.preply(nick, reply_to, f"{nick}: restarting process — back in a moment ...")
        log.info(f"Process restart requested by {nick}")
        # Brief pause so the PRIVMSG flushes before the socket closes
        import time as _t; _t.sleep(1)
        try:
            self.send("QUIT :Restarting ...", priority=0)
        except Exception:
            pass
        # Replace current process with a fresh copy of itself
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def cmd_rehash(self, nick: str, reply_to: str, arg):
        """
        Re-read config.ini live — picks up a new password_hash without restarting.
        Also reloads [bot] cooldown values and [weather] user_agent.
        Drops all active admin sessions since the password may have changed.
        """
        if not self.is_admin(nick):
            self.preply(nick, reply_to, f"{nick}: you must {CMD_PREFIX}auth first (PM only)."); return

        try:
            cfg.read("config.ini")
        except Exception as e:
            self.preply(nick, reply_to, f"{nick}: failed to read config.ini — {e}")
            log.error(f"Rehash failed: {e}")
            return

        h = _get_admin_hash()
        if not h:
            self.preply(nick, reply_to,
                f"{nick}: config reloaded but no password_hash is set — "
                f"module management disabled until one is configured.")
            log.warning("Rehash: no password_hash in config")
        else:
            prefix = h.split("$")[0] if "$" in h else ""
            if prefix not in ("scrypt", "bcrypt", "argon2"):
                self.preply(nick, reply_to,
                    f"{nick}: config reloaded but password_hash format is invalid "
                    f"(got '{prefix}$...'). Must be scrypt$, bcrypt$, or argon2$.")
                log.error(f"Rehash: invalid hash prefix '{prefix}'")
                return
            self.preply(nick, reply_to, f"{nick}: config reloaded — new {prefix} hash active.")
            log.info(f"Rehash: new {prefix} hash loaded by {nick}")

        # Drop all authed sessions — password may have changed
        count = len(self._authed_nicks)
        self._authed_nicks.clear()
        if count:
            log.info(f"Rehash: cleared {count} active admin session(s)")
            self.preply(nick, reply_to,
                f"All admin sessions have been cleared — re-authenticate to continue.")

    # ── dispatcher ─────────────────────────────────────────────────────────

    _CORE_COMMANDS = {
        "help":      "cmd_help",
        "modules":   "cmd_modules",
        "load":      "cmd_load",
        "unload":    "cmd_unload",
        "reload":    "cmd_reload",
        "reloadall": "cmd_reloadall",
        "restart":   "cmd_restart",
        "rehash":    "cmd_rehash",
        "auth":      "cmd_auth",
        "deauth":    "cmd_deauth",
    }

    def dispatch(self, nick: str, reply_to: str, cmd: str, arg, is_pm: bool):
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in a private message.")
            return

        # Global flood gate — silently drop if nick is sending too fast
        if self.flood_limited(nick):
            log.debug(f"Flood gate: dropped {cmd!r} from {nick}")
            return

        def run(fn, *a):
            threading.Thread(target=fn, args=a, daemon=True).start()

        if cmd in self._CORE_COMMANDS:
            run(getattr(self, self._CORE_COMMANDS[cmd]), nick, reply_to, arg)
            return

        if cmd in self._commands:
            mod_name, method_name = self._commands[cmd]
            instance = self._modules.get(mod_name)
            if instance:
                run(getattr(instance, method_name), nick, reply_to, arg)

    # ── connection ─────────────────────────────────────────────────────────

    def _make_socket(self):
        """Create and return a connected (and SSL-wrapped if needed) socket."""
        use_ssl    = cfg["irc"].getboolean("ssl",        fallback=True)
        ssl_verify = cfg["irc"].getboolean("ssl_verify", fallback=True)
        log.info(
            f"Connecting to {IRC_SERVER}:{IRC_PORT} "
            f"({'SSL' if use_ssl else 'plain'}"
            f"{', no cert verify' if use_ssl and not ssl_verify else ''})"
        )
        raw = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=30)
        if use_ssl:
            ctx = ssl.create_default_context()
            if not ssl_verify:
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=IRC_SERVER)
        else:
            sock = raw
        sock.settimeout(300)
        return sock

    def _start_keepalive(self):
        stop = self._keepalive_stop
        def _loop():
            while not stop.wait(timeout=90):
                try:
                    self.send(f"PING :{IRC_SERVER}", priority=0)
                except Exception:
                    break
        threading.Thread(target=_loop, daemon=True, name="keepalive").start()

    def _connect(self):
        """
        Stop old keepalive and sender, build a new socket, assign it,
        start new sender thread and keepalive.
        Does NOT send NICK/USER — caller handles the registration flow.
        Raises on failure so the caller can retry.
        """
        self._keepalive_stop.set()
        self._stop_sender()
        self._keepalive_stop = threading.Event()
        self._nick_attempt    = NICKNAME
        self._cap_negotiating = False
        self._caps_acked      = set()
        self.sock = self._make_socket()
        self._start_sender()
        self._start_keepalive()

    def rejoin_saved_channels(self):
        saved = _load_channels()
        if not saved:
            log.info("No saved channels — waiting for INVITE.")
            return
        for ch in saved:
            self.send(f"JOIN {ch}")
            self.active_channels.add(ch.lower())
            log.info(f"Rejoined saved channel: {ch}")

    def _on_invite(self, nick: str, channel: str):
        log.info(f"Invited to {channel} by {nick}")
        self.send(f"JOIN {channel}")
        self.active_channels.add(channel.lower())
        _save_channels(self.active_channels)

    def _on_bot_join(self, channel: str):
        self.active_channels.add(channel.lower())
        _save_channels(self.active_channels)
        log.info(f"Joined {channel}")

    def _on_bot_part(self, channel: str):
        self.active_channels.discard(channel.lower())
        _save_channels(self.active_channels)
        log.info(f"Left {channel}")

    # ── main loop ──────────────────────────────────────────────────────────

    def run(self):
        self.autoload_modules()
        log.info(f"IRCv3 caps requested: {', '.join(sorted(DESIRED_CAPS))}")

        # Initial connection — retry forever until it works
        while True:
            try:
                self._connect()
                break
            except Exception as e:
                log.error(f"Connection failed: {e} — retrying in 30s")
                time.sleep(30)

        buf        = ""
        identified = False

        while True:
            try:
                # If we just (re)connected, begin IRCv3 registration
                if not identified and self.sock:
                    if SERVER_PW:
                        self.send(f"PASS {SERVER_PW}", priority=0)
                    # CAP LS 302 must be sent before NICK/USER to pause registration
                    # until capability negotiation completes
                    self.send("CAP LS 302", priority=0)
                    self._cap_negotiating = True
                    self.send(f"NICK {self._nick_attempt}", priority=0)
                    self.send(f"USER {NICKNAME} 0 * :{REALNAME}", priority=0)

                data = self.sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    raise ConnectionResetError("Server closed connection")

                buf += data
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    log.debug(f"<< {line}")
                    self._process(line)

                    if "376" in line or "422" in line:
                        if not identified:
                            # If CAP negotiation is still in progress (server
                            # sent motd before CAP END was processed), close it
                            if self._cap_negotiating:
                                self.send("CAP END", priority=0)
                                self._cap_negotiating = False
                            if NICKSERV_PW:
                                self.send(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PW}")
                                time.sleep(1)
                            if OPER_NAME and OPER_PW:
                                self.send(f"OPER {OPER_NAME} {OPER_PW}")
                                log.info(f"Sent OPER request for {OPER_NAME}")
                            self.rejoin_saved_channels()
                            identified = True

            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, ssl.SSLError, OSError) as e:
                log.warning(f"Connection lost: {e} — reconnecting in 15s")
                self._stop_sender()
                identified = False
                buf        = ""
                time.sleep(15)
                while True:
                    try:
                        self._connect()
                        break
                    except Exception as ce:
                        log.error(f"Reconnect failed: {ce} — retrying in 30s")
                        time.sleep(30)

            except Exception as e:
                log.error(f"Unexpected error: {e}")
                time.sleep(5)

    def _process(self, line: str):
        if line.startswith("PING"):
            self.send("PONG " + line.split(":", 1)[1], priority=0)
            return

        # Strip IRCv3 message tags (@tag=val :rest) before parsing
        # Tags are informational — we use server-time if present, ignore the rest
        raw_line = line
        if line.startswith("@"):
            parts = line.split(" ", 1)
            line  = parts[1] if len(parts) > 1 else ""
        _ = raw_line  # keep reference in case we want tags later

        # CAP — IRCv3 capability negotiation
        cap_m = re.match(r"(?::\S+ )?CAP \S+ (\S+)(?: :?(.*))?", line)
        if cap_m:
            sub, params = cap_m.group(1).upper(), (cap_m.group(2) or "").strip()
            if sub == "LS":
                # Server listed its caps — request the ones we want
                server_caps = set(re.split(r"[\s=][^\s]*", params))
                wanted = DESIRED_CAPS & server_caps
                if wanted:
                    self.send(f"CAP REQ :{' '.join(sorted(wanted))}", priority=0)
                else:
                    self.send("CAP END", priority=0)
                    self._cap_negotiating = False
            elif sub == "ACK":
                self._caps_acked = set(params.split())
                log.info(f"IRCv3 caps acknowledged: {self._caps_acked}")
                self.send("CAP END", priority=0)
                self._cap_negotiating = False
            elif sub == "NAK":
                log.info(f"IRCv3 caps denied: {params}")
                self.send("CAP END", priority=0)
                self._cap_negotiating = False
            elif sub == "NEW":
                # cap-notify: new caps available — request any we want
                new_caps = set(re.split(r"[\s=][^\s]*", params)) & DESIRED_CAPS
                if new_caps:
                    self.send(f"CAP REQ :{' '.join(sorted(new_caps))}", priority=0)
            return

        # 421 — unknown command: old server doesn't support CAP at all
        # Close cap negotiation and let registration proceed
        if re.match(r":\S+ 421 \S+ CAP ", line):
            if self._cap_negotiating:
                self._cap_negotiating = False
                log.info("Server does not support CAP — proceeding without IRCv3")
            return

        # 451 — not registered yet (some servers send this before CAP END)
        if re.match(r":\S+ 451 ", line):
            if self._cap_negotiating:
                self.send("CAP END", priority=0)
                self._cap_negotiating = False
            return

        # 433 — nickname already in use, try with _ appended
        if re.match(r":\S+ 433 ", line):
            self._nick_attempt = self._nick_attempt.rstrip("_") + "_"
            self.send(f"NICK {self._nick_attempt}", priority=0)
            log.warning(f"Nick in use — trying {self._nick_attempt!r}")
            return

        # CHGHOST — IRCv3: user's host changed without a quit/rejoin
        chghost_m = re.match(r":([^!]+)![^@]+@\S+ CHGHOST (\S+) (\S+)", line)
        if chghost_m:
            ch_nick, ch_user, ch_host = chghost_m.groups()
            new_host = f"{ch_user}@{ch_host}"
            user_rename(ch_nick, ch_nick, new_host)
            return

        # ACCOUNT — IRCv3 account-notify
        account_m = re.match(r":([^!]+)![^@]+@\S+ ACCOUNT (\S+)", line)
        if account_m:
            # We note it but don't currently do anything with account names
            log.debug(f"ACCOUNT: {account_m.group(1)} -> {account_m.group(2)}")
            return

        # INVITE
        inv = re.match(r":([^!]+)![^@]+@\S+ INVITE \S+ :?(\S+)", line)
        if inv:
            self._on_invite(inv.group(1), inv.group(2))
            return

        # JOIN
        # JOIN — supports extended-join (IRCv3): ":nick!user@host JOIN #chan account :realname"
        join_m = re.match(r":([^!]+)![^@]+@(\S+) JOIN :?(\S+)(?:\s+(\S+))?", line)
        if join_m:
            j_nick, j_host, j_chan = join_m.group(1), join_m.group(2), join_m.group(3)
            if j_nick.lower() == NICKNAME.lower():
                self._on_bot_join(j_chan)
            else:
                user_join(j_chan, j_nick, j_host)
            return

        # PART
        part_m = re.match(r":([^!]+)![^@]+@(\S+) PART :?(\S+)", line)
        if part_m:
            p_nick, p_host, p_chan = part_m.groups()
            if p_nick.lower() == NICKNAME.lower():
                self._on_bot_part(p_chan)
            else:
                user_part(p_chan, p_nick)
            return

        # KICK
        kick_m = re.match(r":\S+ KICK (\S+) (\S+)", line)
        if kick_m:
            k_chan, k_nick = kick_m.group(1), kick_m.group(2)
            if k_nick.lower() == NICKNAME.lower():
                self._on_bot_part(k_chan)
                log.info(f"Kicked from {k_chan}")
            else:
                user_part(k_chan, k_nick)
            return

        # QUIT
        quit_m = re.match(r":([^!]+)![^@]+@\S+ QUIT", line)
        if quit_m:
            user_quit(quit_m.group(1))
            return

        # NICK change
        nick_m = re.match(r":([^!]+)![^@]+@(\S+) NICK :?(\S+)", line)
        if nick_m:
            user_rename(nick_m.group(1), nick_m.group(3), nick_m.group(2))
            return

        # PRIVMSG
        m = re.match(r":([^!]+)![^@]+@(\S+) PRIVMSG (\S+) :(.*)", line)
        if not m:
            return

        nick, hostmask, target, text = m.groups()
        text     = text.strip()
        is_pm    = target.lower() == NICKNAME.lower()
        reply_to = nick if is_pm else target

        if not is_pm and target.lower() in self.active_channels:
            user_join(target, nick, hostmask)

        all_cmds = set(self._CORE_COMMANDS) | set(self._commands)
        cmd, arg = None, None

        if text.startswith(CMD_PREFIX):
            parts = text[len(CMD_PREFIX):].split(None, 1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else None
        elif is_pm:
            parts     = text.split(None, 1)
            candidate = parts[0].lower()
            if candidate in all_cmds:
                cmd = candidate
                arg = parts[1].strip() if len(parts) > 1 else None

        if cmd and cmd in all_cmds:
            log.info(
                f"cmd={cmd!r} arg={arg!r} from {nick}!{hostmask} "
                f"{'(PM)' if is_pm else 'in ' + reply_to}"
            )
            self.dispatch(nick, reply_to, cmd, arg, is_pm)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = IRCBot()
    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            sys.exit(0)
        except Exception as e:
            log.error(f"Crash: {e} — restarting in 30s")
            time.sleep(30)
