import re
import logging
from .base import BotModule

log = logging.getLogger("internets.channels")

_CHAN_RE = re.compile(r"^[#&+!][^\s,\x07]{1,49}$")


class ChannelsModule(BotModule):
    """
    Join/part management and user roster queries.

    The bot is invite-only by default — it joins on INVITE or via .join.
    Channel state is persisted by the core and restored on reconnect.
    """

    COMMANDS = {
        "join":  "cmd_join",
        "part":  "cmd_part",
        "users": "cmd_users",
    }

    def cmd_join(self, nick, reply_to, arg):
        if not self.bot.is_admin(nick):
            self.bot.privmsg(reply_to, f"{nick}: admin auth required — or /INVITE the bot")
            return
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}join <#channel>")
            return
        if arg.lower() in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: already in {arg}")
        else:
            self.bot.send(f"JOIN {arg}")
            self.bot.privmsg(reply_to, f"{nick}: joining {arg} ...")
            log.info(f"{nick} requested JOIN {arg}")

    def cmd_part(self, nick, reply_to, arg):
        if not self.bot.is_admin(nick):
            self.bot.privmsg(reply_to, f"{nick}: admin auth required")
            return
        if not arg or not _CHAN_RE.match(arg):
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}part <#channel>")
            return
        if arg.lower() not in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: not in {arg}")
        else:
            self.bot.send(f"PART {arg} :Parting on request from {nick}")
            if arg.lower() != reply_to.lower():
                self.bot.privmsg(reply_to, f"{nick}: left {arg}")
            log.info(f"{nick} requested PART {arg}")

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

    def help_lines(self, prefix):
        return [
            f"  {prefix}join  <#channel>   Join a channel (or /INVITE the bot)",
            f"  {prefix}part  <#channel>   Leave a channel",
            f"  {prefix}users [#channel]   Show known users in a channel",
        ]


def setup(bot):
    return ChannelsModule(bot)
