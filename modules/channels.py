import re
import time
import threading
import logging
from .base import BotModule

log = logging.getLogger("internets.channels")

_CHAN_RE = re.compile(r"^[#&+!][^\s,\x07]{1,49}$")

# ── Services response patterns ──────────────────────────────────────────
# These cover Anope, Atheme, Epona, X2, X3, and most forks.
#
# Founder/owner line examples:
#   Anope:   "        Founder: someuser"
#   Atheme:  "Founder    : someuser"
#   Epona:   "     Founder: someuser"
#   X2/X3:   "Owner:       someuser"
#   Some:    "Founder: someuser (someaccount)"
_FOUNDER_RE = re.compile(r"^\s*(?:Founder|Owner)\s*:\s*(\S+)", re.IGNORECASE)

# "Channel #foo is not registered" / "#foo is not registered" / etc.
_NOT_REG_RE = re.compile(r"not\s+registered", re.IGNORECASE)

# Timeout for the entire WHOIS + services verification round-trip.
_VERIFY_TIMEOUT = 15


class _PendingJoin:
    """State for an in-flight ownership verification."""
    __slots__ = ("nick", "channel", "reply_to", "created",
                 "account", "founder", "whois_done", "info_failed", "action")

    def __init__(self, nick, channel, reply_to, action="join"):
        self.nick        = nick
        self.channel     = channel
        self.reply_to    = reply_to
        self.action      = action
        self.created     = time.time()
        self.account     = None   # NickServ account from WHOIS 330
        self.founder     = None   # Channel founder from services INFO
        self.whois_done  = False  # True once we receive WHOIS 318 (end)
        self.info_failed = False  # True if channel is not registered


class ChannelsModule(BotModule):
    """
    Join/part management and user roster queries.

    .join and .part require the user to be either:
      - authenticated as a bot admin (.auth), or
      - the registered channel founder, verified via IRC services

    Founder verification flow:
      1. WHOIS the requesting user -> extract NickServ account (330 numeric)
      2. Query services INFO on target channel -> extract founder name
      3. Compare account == founder (case-insensitive)
      4. Join/part on match, deny on mismatch, timeout after 15s

    The services bot nick is configurable via ``services_nick`` in config.ini
    (default: ChanServ).  Tested patterns cover Anope, Atheme, Epona, X2, X3.

    /INVITE is handled by the core and is always accepted -- IRC servers
    enforce their own permission model for INVITE.
    """

    COMMANDS = {
        "join":  "cmd_join",
        "part":  "cmd_part",
        "users": "cmd_users",
    }

    def on_load(self):
        self._services = self.bot.cfg["bot"].get("services_nick", "ChanServ").strip()
        self._pending  = {}   # channel_lower -> _PendingJoin
        self._svc_ctx  = {}   # channel_lower -> timestamp  (tracks which channel
                              # a multi-line services response is about)
        self._lock     = threading.Lock()

        # Background thread reaps timed-out verifications.
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="chan-verify-gc")
        self._cleanup_thread.start()
        log.info(f"Services nick for ownership checks: {self._services}")

    def on_unload(self):
        self._cleanup_stop.set()

    # ── Cleanup ──────────────────────────────────────────────────────────

    def _cleanup_loop(self):
        while not self._cleanup_stop.wait(timeout=5):
            now = time.time()
            with self._lock:
                expired = [k for k, p in self._pending.items()
                           if now - p.created > _VERIFY_TIMEOUT]
                for k in expired:
                    p = self._pending.pop(k)
                    self.bot.privmsg(
                        p.reply_to,
                        f"{p.nick}: ownership verification timed out for {p.channel} "
                        f"— try /INVITE or ask a bot admin.")
                    log.info(f"Verify timeout: {p.nick} -> {p.channel}")
                # Clear stale context entries.
                self._svc_ctx = {k: v for k, v in self._svc_ctx.items()
                                 if now - v < _VERIFY_TIMEOUT}

    # ── Commands ─────────────────────────────────────────────────────────

    def cmd_join(self, nick, reply_to, arg):
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}join <#channel>")
            return

        chan = arg.strip()
        if chan.lower() in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: already in {chan}")
            return

        # Bot admins bypass verification.
        if self.bot.is_admin(nick):
            self.bot.send(f"JOIN {chan}")
            self.bot.privmsg(reply_to, f"{nick}: joining {chan} ...")
            log.info(f"Admin {nick} requested JOIN {chan}")
            return

        self._start_verify(nick, chan, reply_to, action="join")

    def cmd_part(self, nick, reply_to, arg):
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}part <#channel>")
            return

        chan = arg.strip()
        if chan.lower() not in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: not in {chan}")
            return

        # Bot admins bypass verification.
        if self.bot.is_admin(nick):
            self.bot.send(f"PART {chan} :Parting on request from {nick}")
            if chan.lower() != reply_to.lower():
                self.bot.privmsg(reply_to, f"{nick}: left {chan}")
            log.info(f"Admin {nick} requested PART {chan}")
            return

        self._start_verify(nick, chan, reply_to, action="part")

    def cmd_users(self, nick, reply_to, arg):
        if arg and arg.startswith(("#", "&", "+", "!")):
            channel = arg.strip()
        elif reply_to.startswith(("#", "&", "+", "!")):
            channel = reply_to
        else:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.preply(nick, reply_to, f"{nick}: {p}users [#channel]")
            return

        users = self.bot.channel_users(channel)
        if not users:
            self.bot.preply(nick, reply_to, f"No user data for {channel} yet.")
            return

        self.bot.preply(nick, reply_to, f"Known users in {channel} ({len(users)}):")
        for u in sorted(users.values(), key=lambda u: u.get("last_seen", ""), reverse=True):
            last  = u.get("last_seen",  "?")[:19].replace("T", " ")
            first = u.get("first_seen", "?")[:19].replace("T", " ")
            self.bot.notice(nick, f"  {u['nick']}!{u.get('hostmask','?')}  "
                                  f"first: {first}  last: {last}")

    # ── Verification machinery ───────────────────────────────────────────

    def _start_verify(self, nick, channel, reply_to, action="join"):
        """Kick off the async founder verification flow."""
        key = channel.lower()

        with self._lock:
            if key in self._pending:
                self.bot.privmsg(reply_to,
                    f"{nick}: verification already in progress for {channel}")
                return
            self._pending[key] = _PendingJoin(nick, channel, reply_to, action)

        # Step 1: WHOIS the user to get their NickServ account (330 numeric).
        self.bot.send(f"WHOIS {nick}", priority=1)
        # Step 2: Ask services for the channel founder.
        self.bot.send(f"PRIVMSG {self._services} :INFO {channel}", priority=1)
        self.bot.privmsg(reply_to,
            f"{nick}: verifying channel ownership for {channel} ...")
        log.info(f"Verify started ({action}): {nick} -> {channel}")

    def on_raw(self, line):
        """
        Intercept WHOIS numerics and services NOTICEs to complete pending
        founder verifications.

        330 = RPL_WHOISACCOUNT  -- user's NickServ account
        318 = RPL_ENDOFWHOIS    -- no more WHOIS data coming
        NOTICE from services    -- parse founder / not-registered
        """
        # Fast bail if nothing is pending.
        with self._lock:
            if not self._pending:
                return

        # ── WHOIS 330: user has a NickServ account ───────────────────
        # :server 330 botnick target account :is logged in as
        m = re.match(r":\S+ 330 \S+ (\S+) (\S+)", line)
        if m:
            target_lower = m.group(1).lower()
            account      = m.group(2)
            with self._lock:
                for p in self._pending.values():
                    if p.nick.lower() == target_lower:
                        p.account = account
                        log.debug(f"WHOIS account: {p.nick} = {account}")
            return

        # ── WHOIS 318: end of WHOIS ──────────────────────────────────
        # :server 318 botnick target :End of /WHOIS list.
        m = re.match(r":\S+ 318 \S+ (\S+)", line)
        if m:
            target_lower = m.group(1).lower()
            with self._lock:
                for p in list(self._pending.values()):
                    if p.nick.lower() == target_lower:
                        p.whois_done = True
                        self._try_complete(p)
            return

        # ── NOTICE from the configured services bot ──────────────────
        m = re.match(r":([^!]+)!\S+ NOTICE \S+ :(.*)", line)
        if not m:
            return
        if m.group(1).lower() != self._services.lower():
            return
        text = m.group(2)

        with self._lock:
            # Track which channel this multi-line response is about.
            # Services INFO responses always include the channel name in the
            # header line before any founder data. Match it against pending
            # requests to establish context.
            #
            # Header examples:
            #   "Information for channel #test:"          (Anope)
            #   "Information on #test:"                   (Atheme)
            #   "#test Information"                       (X2/X3)
            #   "Channel: #test"                          (X3 variant)
            for ch_match in re.finditer(r"(#[^\s,\x07]+)", text):
                ch = ch_match.group(1).rstrip(":.")  # strip trailing punctuation
                if ch.lower() in self._pending:
                    self._svc_ctx[ch.lower()] = time.time()
                    break

            # Check for founder/owner line.
            fm = _FOUNDER_RE.search(text)
            if fm:
                founder = fm.group(1)
                # Associate with the correct pending request via context.
                # Use most-recently-seen context first (handles serial queries).
                for ch in self._ctx_by_recency():
                    if ch in self._pending:
                        self._pending[ch].founder = founder
                        log.debug(f"Services founder for {ch}: {founder}")
                        self._try_complete(self._pending[ch])
                        self._svc_ctx.pop(ch, None)
                        break
                return

            # Check for "not registered" error.
            if _NOT_REG_RE.search(text):
                target_ch = None
                # Try to find the channel directly in the error text.
                for ch_match in re.finditer(r"(#[^\s,\x07]+)", text):
                    ch = ch_match.group(1).rstrip(":.")
                    if ch.lower() in self._pending:
                        target_ch = ch.lower()
                        break
                # Fall back to the most recent context.
                if target_ch is None:
                    for ch in self._ctx_by_recency():
                        if ch in self._pending:
                            target_ch = ch
                            break
                if target_ch and target_ch in self._pending:
                    self._pending[target_ch].info_failed = True
                    self._try_complete(self._pending[target_ch])
                    self._svc_ctx.pop(target_ch, None)
                return

    def _ctx_by_recency(self):
        """Return context channel keys ordered by most recently seen.  Under lock."""
        return [k for k, _ in sorted(self._svc_ctx.items(),
                                     key=lambda x: x[1], reverse=True)]

    def _try_complete(self, p):
        """
        Attempt to resolve a pending verification.  Called under self._lock.

        Resolution matrix:
          info_failed                        -> deny  (channel not registered)
          whois_done + no account            -> deny  (not identified)
          account + founder + match          -> approve
          account + founder + mismatch       -> deny
          otherwise                          -> wait  (still collecting data)
        """
        # Channel not registered with services.
        if p.info_failed:
            self._resolve(p, False,
                f"{p.channel} is not registered with {self._services} "
                f"— cannot verify ownership. Try /INVITE or ask a bot admin.")
            return

        # WHOIS done but user had no NickServ account.
        if p.whois_done and p.account is None:
            self._resolve(p, False,
                "you must be identified with NickServ to use this command "
                "— try /INVITE or ask a bot admin.")
            return

        # Have both account and founder — compare.
        if p.account is not None and p.founder is not None:
            if p.account.lower() == p.founder.lower():
                self._resolve(p, True, None)
            else:
                self._resolve(p, False,
                    f"your NickServ account ({p.account}) does not match the "
                    f"founder of {p.channel} ({p.founder}). "
                    f"Try /INVITE or ask a bot admin.")
            return

        # Still waiting for more data — will be called again when new info arrives,
        # or the cleanup thread will time it out.

    def _resolve(self, p, approved, reason):
        """Complete a pending verification.  Called under self._lock."""
        self._pending.pop(p.channel.lower(), None)

        if approved:
            if p.action == "part":
                self.bot.send(f"PART {p.channel} :Parting on request from {p.nick}")
                self.bot.privmsg(p.reply_to,
                    f"{p.nick}: ownership verified — leaving {p.channel}")
                log.info(f"Part approved (founder): "
                         f"{p.nick} ({p.account}) -> {p.channel}")
            else:
                self.bot.send(f"JOIN {p.channel}")
                self.bot.privmsg(p.reply_to,
                    f"{p.nick}: ownership verified — joining {p.channel}")
                log.info(f"Join approved (founder): "
                         f"{p.nick} ({p.account}) -> {p.channel}")
        else:
            self.bot.privmsg(p.reply_to, f"{p.nick}: {reason}")
            log.info(f"{p.action.title()} denied: {p.nick} -> {p.channel}")

    # ── Help ─────────────────────────────────────────────────────────────

    def help_lines(self, prefix):
        return [
            f"  {prefix}join  <#channel>   Invite the bot     [channel founder / admin]",
            f"  {prefix}part  <#channel>   Remove the bot     [channel founder / admin]",
            f"  {prefix}users [#channel]   Show known users in a channel",
        ]


def setup(bot):
    return ChannelsModule(bot)
