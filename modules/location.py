from __future__ import annotations

import logging
from .base    import BotModule
from .geocode import geocode

log = logging.getLogger("internets.location")


class LocationModule(BotModule):
    COMMANDS: dict[str, str] = {
        "regloc":            "cmd_regloc",
        "register_location": "cmd_regloc",
        "myloc":             "cmd_myloc",
        "delloc":            "cmd_delloc",
    }

    def on_load(self) -> None:
        self._ua: str = self.bot.cfg["weather"]["user_agent"]

    async def cmd_regloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}regloc <zip or city name>")
            return
        geo = await geocode(arg, self._ua)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: location not found: '{arg}'")
            return
        _, _, display, _ = geo
        self.bot.loc_set(nick, arg)
        self.bot.privmsg(reply_to, f"{nick}: location set to {display}")
        log.info(f"regloc {nick} -> {arg!r} ({display})")

    async def cmd_myloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        raw = self.bot.loc_get(nick)
        if raw:
            geo     = await geocode(raw, self._ua)
            display = geo[2] if geo else raw
            self.bot.privmsg(reply_to, f"{nick}: saved location is {display} ({raw!r})")
        else:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: no location saved — use {p}regloc <zip or city>")

    async def cmd_delloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        if self.bot.loc_del(nick):
            self.bot.privmsg(reply_to, f"{nick}: saved location removed.")
        else:
            self.bot.privmsg(reply_to, f"{nick}: no saved location.")

    def help_lines(self, prefix: str) -> list[str]:
        return [
            f"  {prefix}regloc/.register_location <zip|city>   Save your default location",
            f"  {prefix}myloc                                   Show your saved location",
            f"  {prefix}delloc                                  Remove your saved location",
        ]


def setup(bot: object) -> LocationModule:
    return LocationModule(bot)  # type: ignore[arg-type]
