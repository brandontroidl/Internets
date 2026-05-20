from __future__ import annotations

import asyncio
import re
import time
import logging
from .base import BotModule

log = logging.getLogger("internets.channels")

_CHAN_RE = re.compile(r"^[#&+!][^\s,\x07]{1,49}$")

_FOUNDER_RE = re.compile(r"^\s*(?:Founder|Owner)\s*:\s*(\S+)", re.IGNORECASE)
_NOT_REG_RE = re.compile(r"not\s+registered", re.IGNORECASE)
_VERIFY_TIMEOUT = 15


class _PendingJoin:
    __slots__ = ("nick", "channel", "reply_to", "created",
                 "account", "founder", "whois_done", "info_failed", "action")

    def __init__(self, nick: str, channel: str, reply_to: str, action: str = "join") -> None:
        self.nick        = nick
        self.channel     = channel
        self.reply_to    = reply_to
        self.action      = action
        self.created     = time.time()
        self.account: str | None  = None
        self.founder: str | None  = None
        self.whois_done  = False
        self.info_failed = False


class ChannelsModule(BotModule):
    """
    Join/part management and user roster queries.

    .join and .part require the user to be either an authenticated admin
    or the registered channel founder.  Founder verification is async:
    WHOIS for the user's NickServ account, INFO on the channel via
    services, then compare.  Times out after 15s.
    """

    COMMANDS: dict[str, str] = {
        "join":  "cmd_join",
        "part":  "cmd_part",
        "users": "cmd_users",
    }

    def on_load(self) -> None:
        """Initialize verification state and start cleanup task."""
        self._services: str = self.bot.cfg["bot"].get("services_nick", "ChanServ").strip()
        self._pending: dict[str, _PendingJoin] = {}
        self._svc_ctx: dict[str, float] = {}
        # Lock is a threading.Lock because on_raw() is called from the event
        # loop thread synchronously, while command handlers may run concurrently.
        import threading
        self._lock = threading.Lock()
        # Start the async cleanup task.
        self._cleanup_task = asyncio.get_running_loop().create_task(
            self._cleanup_loop(), name="chan-verify-gc")
        log.info(f"Services nick for ownership checks: {self._services}")

    def on_unload(self) -> None:
        """Cancel the cleanup task."""
        if hasattr(self, "_cleanup_task") and self._cleanup_task:
            self._cleanup_task.cancel()

    # ── Cleanup ──────────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Periodically expire stale verification requests."""
        try:
            while True:
                await asyncio.sleep(5)
                now = time.time()
                try:
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
                        self._svc_ctx = {k: v for k, v in self._svc_ctx.items()
                                         if now - v < _VERIFY_TIMEOUT}
                except Exception as e:
                    log.warning(f"Verify cleanup error: {e}")
        except asyncio.CancelledError:
            pass

    # ── Commands ─────────────────────────────────────────────────────────

    async def cmd_join(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Request the bot to join a channel.  Requires founder or admin."""
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}join <#channel>")
            return

        chan = arg.strip()
        if chan.lower() in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: already in {chan}")
            return

        if self.bot.is_admin(nick):
            self.bot.send(f"JOIN {chan}")
            self.bot.privmsg(reply_to, f"{nick}: joining {chan} ...")
            log.info(f"Admin {nick} requested JOIN {chan}")
            return

        self._start_verify(nick, chan, reply_to, action="join")

    async def cmd_part(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Request the bot to leave a channel.  Requires founder or admin."""
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}part <#channel>")
            return

        chan = arg.strip()
        if chan.lower() not in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: not in {chan}")
            return

        if self.bot.is_admin(nick):
            self.bot.send(f"PART {chan} :Parting on request from {nick}")
            if chan.lower() != reply_to.lower():
                self.bot.privmsg(reply_to, f"{nick}: left {chan}")
            log.info(f"Admin {nick} requested PART {chan}")
            return

        self._start_verify(nick, chan, reply_to, action="part")

    async def cmd_users(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Show tracked users in a channel."""
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

    def _start_verify(self, nick: str, channel: str, reply_to: str,
                      action: str = "join") -> None:
        key = channel.lower()

        with self._lock:
            if key in self._pending:
                self.bot.privmsg(reply_to,
                    f"{nick}: verification already in progress for {channel}")
                return
            self._pending[key] = _PendingJoin(nick, channel, reply_to, action)

        self.bot.send(f"WHOIS {nick}", priority=1)
        self.bot.send(f"PRIVMSG {self._services} :INFO {channel}", priority=1)
        self.bot.privmsg(reply_to,
            f"{nick}: verifying channel ownership for {channel} ...")
        log.info(f"Verify started ({action}): {nick} -> {channel}")

    def on_raw(self, line: str) -> None:
        """Process WHOIS and services responses for ownership verification."""
        with self._lock:
            if not self._pending:
                return

        # WHOIS 330: user has a NickServ account
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

        # WHOIS 318: end of WHOIS
        m = re.match(r":\S+ 318 \S+ (\S+)", line)
        if m:
            target_lower = m.group(1).lower()
            with self._lock:
                for p in list(self._pending.values()):
                    if p.nick.lower() == target_lower:
                        p.whois_done = True
                        self._try_complete(p)
            return

        # NOTICE from the configured services bot
        m = re.match(r":([^!]+)!\S+ NOTICE \S+ :(.*)", line)
        if not m:
            return
        if m.group(1).lower() != self._services.lower():
            return
        text = m.group(2)

        with self._lock:
            for ch_match in re.finditer(r"(#[^\s,\x07]+)", text):
                ch = ch_match.group(1).rstrip(":.")
                if ch.lower() in self._pending:
                    self._svc_ctx[ch.lower()] = time.time()
                    break

            fm = _FOUNDER_RE.search(text)
            if fm:
                founder = fm.group(1)
                for ch in self._ctx_by_recency():
                    if ch in self._pending:
                        self._pending[ch].founder = founder
                        log.debug(f"Services founder for {ch}: {founder}")
                        self._try_complete(self._pending[ch])
                        self._svc_ctx.pop(ch, None)
                        break
                return

            if _NOT_REG_RE.search(text):
                target_ch: str | None = None
                for ch_match in re.finditer(r"(#[^\s,\x07]+)", text):
                    ch = ch_match.group(1).rstrip(":.")
                    if ch.lower() in self._pending:
                        target_ch = ch.lower()
                        break
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

    def _ctx_by_recency(self) -> list[str]:
        return [k for k, _ in sorted(self._svc_ctx.items(),
                                     key=lambda x: x[1], reverse=True)]

    def _try_complete(self, p: _PendingJoin) -> None:
        if p.info_failed:
            self._resolve(p, False,
                f"{p.channel} is not registered with {self._services} "
                f"— cannot verify ownership. Try /INVITE or ask a bot admin.")
            return

        if p.whois_done and p.account is None:
            self._resolve(p, False,
                "you must be identified with NickServ to use this command "
                "— try /INVITE or ask a bot admin.")
            return

        if p.account is not None and p.founder is not None:
            if p.account.lower() == p.founder.lower():
                self._resolve(p, True, None)
            else:
                self._resolve(p, False,
                    f"your NickServ account ({p.account}) does not match the "
                    f"founder of {p.channel} ({p.founder}). "
                    f"Try /INVITE or ask a bot admin.")
            return

    def _resolve(self, p: _PendingJoin, approved: bool, reason: str | None) -> None:
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

    def help_lines(self, prefix: str) -> list[str]:
        """Return channel management help text."""
        return [
            f"  {prefix}join  <#channel>   Invite the bot     [channel founder / admin]",
            f"  {prefix}part  <#channel>   Remove the bot     [channel founder / admin]",
            f"  {prefix}users [#channel]   Show known users in a channel",
        ]


def setup(bot: object) -> ChannelsModule:
    """Module entry point — returns a ChannelsModule instance."""
    return ChannelsModule(bot)  # type: ignore[arg-type]
