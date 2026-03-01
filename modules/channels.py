"""
Channel management module.

The bot is invite-only — it joins channels when invited via IRC INVITE,
or when someone PMs .join #channel.

Commands: .join, .part, .users
"""

import re
import logging
from .base import BotModule

log = logging.getLogger("internets.channels")


class ChannelsModule(BotModule):
    COMMANDS = {
        "join":  "cmd_join",
        "part":  "cmd_part",
        "users": "cmd_users",
    }

    def on_load(self):
        log.info("ChannelsModule loaded")

    def _valid_channel(self, name: str) -> bool:
        return bool(re.match(r"^[#&+!][^\s,\x07]{1,49}$", name))

    def cmd_join(self, nick, reply_to, arg):
        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg:
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}join <#channel>"); return
        if not self._valid_channel(arg):
            self.bot.privmsg(reply_to, f"{nick}: '{arg}' doesn't look like a valid channel name."); return
        if arg.lower() in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: I'm already in {arg}.")
        else:
            self.bot.send(f"JOIN {arg}")
            # _on_bot_join called by _process when server confirms JOIN
            self.bot.privmsg(reply_to, f"{nick}: joining {arg} ...")
            log.info(f"{nick} requested JOIN {arg}")

    def cmd_part(self, nick, reply_to, arg):
        p = self.bot.cfg["bot"]["command_prefix"]
        if not arg:
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}part <#channel>"); return
        if not self._valid_channel(arg):
            self.bot.privmsg(reply_to, f"{nick}: '{arg}' doesn't look like a valid channel name."); return
        if arg.lower() not in self.bot.active_channels:
            self.bot.privmsg(reply_to, f"{nick}: I'm not in {arg}.")
        else:
            self.bot.send(f"PART {arg} :Parting on request from {nick}")
            # _on_bot_part called by _process when server confirms PART
            if arg.lower() != reply_to.lower():
                self.bot.privmsg(reply_to, f"{nick}: left {arg}.")
            log.info(f"{nick} requested PART {arg}")

    def cmd_users(self, nick, reply_to, arg):
        """
        .users [#channel]
        Show all known users for a channel, or the current channel if no arg.
        """
        p = self.bot.cfg["bot"]["command_prefix"]
        # Determine target channel
        if arg and arg.startswith(("#", "&", "+", "!")):
            channel = arg.strip()
        elif reply_to.startswith(("#", "&", "+", "!")):
            channel = reply_to
        else:
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}users [#channel]"); return

        users = self.bot.channel_users(channel)
        if not users:
            self.bot.privmsg(reply_to, f"No user data for {channel} yet.")
            return

        self.bot.privmsg(reply_to, f"Known users in {channel} ({len(users)}):")
        # Sort by last_seen descending
        sorted_users = sorted(
            users.values(),
            key=lambda u: u.get("last_seen", ""),
            reverse=True
        )
        for u in sorted_users:
            last = u.get("last_seen", "?")[:19].replace("T", " ")
            first = u.get("first_seen", "?")[:19].replace("T", " ")
            host  = u.get("hostmask", "?")
            self.bot.privmsg(
                reply_to,
                f"  {u['nick']}!{host}  first: {first}  last: {last}"
            )

    def help_lines(self, prefix):
        return [
            f"  {prefix}join  <#channel>   Ask me to join a channel (or just /INVITE me)",
            f"  {prefix}part  <#channel>   Ask me to leave a channel",
            f"  {prefix}users [#channel]   Show known users in a channel",
        ]


def setup(bot):
    return ChannelsModule(bot)
