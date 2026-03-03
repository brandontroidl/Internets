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

logging.basicConfig(
    level=getattr(logging, cfg["logging"]["level"]),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(cfg["logging"]["log_file"]),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("internets")


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
    }

    def __init__(self):
        self.sock            = None
        self.cfg             = cfg
        self.active_channels = set()
        self._modules        = {}
        self._commands       = {}
        self._mod_lock       = threading.Lock()
        self._authed         = set()
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
            self.preply(nick, reply_to, f"{nick}: auth first — /MSG {NICKNAME} AUTH <pw>")
            return False
        return True

    def cmd_auth(self, nick, reply_to, arg):
        h = _get_hash()
        if not h:
            self.preply(nick, reply_to, f"{nick}: no password_hash configured — run hashpw.py")
            return
        if not arg:
            self.preply(nick, reply_to, f"{nick}: /MSG {NICKNAME} AUTH <password>")
            return
        try:
            ok = verify_password(arg.strip(), h)
        except ValueError as e:
            self.preply(nick, reply_to, f"{nick}: config error — {e}")
            return
        if ok:
            self._authed.add(nick)
            self.preply(nick, reply_to, f"{nick}: authenticated.")
            log.info(f"Auth granted: {nick}")
        else:
            self.preply(nick, reply_to, f"{nick}: wrong password.")
            log.warning(f"Failed auth: {nick}")

    def cmd_deauth(self, nick, reply_to, arg):
        if nick in self._authed:
            self._authed.discard(nick)
            self.preply(nick, reply_to, f"{nick}: session ended.")
        else:
            self.preply(nick, reply_to, f"{nick}: not authenticated.")

    def cmd_help(self, nick, reply_to, arg):
        p     = CMD_PREFIX
        lines = [
            f"── {NICKNAME} ──────────────────────────────────────────────────",
            f"  {p}help  {p}modules  {p}auth <pw>",
        ]
        if self.is_admin(nick):
            lines += [
                f"  {p}deauth  {p}load/unload/reload <mod>  {p}reloadall",
                f"  {p}restart  {p}rehash                        [admin]",
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

    def dispatch(self, nick, reply_to, cmd, arg, is_pm):
        if cmd in ("auth", "deauth") and not is_pm:
            self.privmsg(reply_to, f"{nick}: {CMD_PREFIX}{cmd} must be used in PM.")
            return
        if self.flood_limited(nick):
            log.debug(f"Flood drop: {cmd!r} from {nick}")
            return

        def run(fn, *a):
            threading.Thread(target=fn, args=a, daemon=True).start()

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
                    log.debug(f"<< {line}")
                    self._process(line)

                    if not identified and re.match(r":\S+ (376|422) ", line):
                        if self._cap_busy:
                            self.send("CAP END", priority=0)
                            self._cap_busy = False
                        if NS_PW:
                            self.send(f"PRIVMSG NickServ :IDENTIFY {NS_PW}")
                            time.sleep(1)
                        if OPER_N and OPER_PW:
                            self.send(f"OPER {OPER_N} {OPER_PW}")
                        self._rejoin_channels()
                        identified = True

            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, ssl.SSLError, OSError) as e:
                log.warning(f"Lost connection: {e} — reconnect in 15s")
                self._sender.stop()
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

        # 433 = "Nickname in use"
        if re.match(r":\S+ 433 ", line):
            self._nick = self._nick + "_"
            self.send(f"NICK {self._nick}", priority=0)
            log.warning(f"Nick in use — trying {self._nick!r}")
            return

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
            self._store.user_rename(old_nick, new_nick, host)
            # Migrate admin session to new nick so the old nick can't be hijacked
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
            parts = text[len(CMD_PREFIX):].split(None, 1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else None
        elif is_pm:
            parts = text.split(None, 1)
            if parts[0].lower() in all_cmds:
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else None

        if cmd and cmd in all_cmds:
            log.info(f"cmd={cmd!r} arg={arg!r} from {nick}!{host} "
                     f"{'(PM)' if is_pm else 'in ' + reply_to}")
            self.dispatch(nick, reply_to, cmd, arg, is_pm)


if __name__ == "__main__":
    import signal
    bot = IRCBot()

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down.")
        try:
            bot.send("QUIT :Shutting down", priority=0)
            time.sleep(2)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            _shutdown(2, None)
        except Exception as e:
            log.error(f"Crash: {e} — restart in 30s")
            time.sleep(30)
