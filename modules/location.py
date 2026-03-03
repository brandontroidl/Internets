import logging
from .base    import BotModule
from .geocode import geocode

log = logging.getLogger("internets.location")


class LocationModule(BotModule):
    COMMANDS = {
        "regloc":            "cmd_regloc",
        "register_location": "cmd_regloc",
        "myloc":             "cmd_myloc",
        "delloc":            "cmd_delloc",
    }

    def on_load(self):
        self._ua = self.bot.cfg["weather"]["user_agent"]

    def cmd_regloc(self, nick, reply_to, arg):
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}regloc <zip or city name>")
            return
        geo = geocode(arg, self._ua)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: location not found: '{arg}'")
            return
        _, _, display, _ = geo
        self.bot.loc_set(nick, arg)
        self.bot.privmsg(reply_to, f"{nick}: location set to {display}")
        log.info(f"regloc {nick} -> {arg!r} ({display})")

    def cmd_myloc(self, nick, reply_to, arg):
        raw = self.bot.loc_get(nick)
        if raw:
            geo     = geocode(raw, self._ua)
            display = geo[2] if geo else raw
            self.bot.privmsg(reply_to, f"{nick}: saved location is {display} ({raw!r})")
        else:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: no location saved — use {p}regloc <zip or city>")

    def cmd_delloc(self, nick, reply_to, arg):
        if self.bot.loc_del(nick):
            self.bot.privmsg(reply_to, f"{nick}: saved location removed.")
        else:
            self.bot.privmsg(reply_to, f"{nick}: no saved location.")

    def help_lines(self, prefix):
        return [
            f"  {prefix}regloc/.register_location <zip|city>   Save your default location",
            f"  {prefix}myloc                                   Show your saved location",
            f"  {prefix}delloc                                  Remove your saved location",
        ]


def setup(bot):
    return LocationModule(bot)
