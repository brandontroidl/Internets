from __future__ import annotations

import logging
from .base    import BotModule, help_row, strip_ctrl
from .geocode import geocode

log = logging.getLogger("internets.location")


class LocationModule(BotModule):
    """User location registration and lookup.

    Privacy note: every command in this module acts on the *invoker's
    own* saved location (keyed by ``nick``).  There is no cross-user
    access path here, so the per-user opt-out flag does not need to be
    consulted; an opted-out user can still set/view/delete their own
    location.  Cross-user lookups (e.g. ``.w -n othernick``) live in
    modules/weather.py, where the opt-out check is enforced.
    """
    COMMANDS: dict[str, str] = {
        "regloc":            "cmd_regloc",
        "register_location": "cmd_regloc",
        "myloc":             "cmd_myloc",
        "delloc":            "cmd_delloc",
    }

    def on_load(self) -> None:
        """Load geocoding user agent - secret_store overrides config."""
        from .base import cred
        # cred(): secret_store first, then [weather].user_agent, else "" - never
        # a bare KeyError (the template defines no [weather].user_agent), the
        # same fix weather.py uses.  A blank UA disables geocoding gracefully.
        self._ua: str = cred(self.bot.cfg, "weather_user_agent", "weather", "user_agent")
        # Home country for bare cross-country postal codes (see geocode()).
        self._default_country: str = self.bot.cfg["weather"].get("default_country", "us")

    async def cmd_regloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Save a default location for the requesting user."""
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}regloc <zip or city name>")
            return
        geo = await geocode(arg, self._ua, default_country=self._default_country)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: location not found: '{strip_ctrl(arg)}'")
            return
        _, _, display, _ = geo
        self.bot.loc_set(nick, arg)
        self.bot.privmsg(reply_to, f"{nick}: location set to {strip_ctrl(display)}")
        log.info(f"regloc {nick} -> {arg!r} ({display})")

    async def cmd_myloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Display the user's saved location."""
        raw = self.bot.loc_get(nick)
        if raw:
            geo     = await geocode(raw, self._ua, default_country=self._default_country)
            display = geo[2] if geo else raw
            self.bot.privmsg(reply_to, f"{nick}: saved location is {strip_ctrl(display)} ({strip_ctrl(raw)!r})")
        else:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: no location saved - use {p}regloc <zip or city>")

    async def cmd_delloc(self, nick: str, reply_to: str, arg: str | None) -> None:
        """Delete the user's saved location."""
        if self.bot.loc_del(nick):
            self.bot.privmsg(reply_to, f"{nick}: saved location removed.")
        else:
            self.bot.privmsg(reply_to, f"{nick}: no saved location.")

    def help_lines(self, prefix: str) -> list[str]:
        """Return location help text."""
        return [
            help_row(prefix, "regloc/.register_location <zip|city>", "Save your default location"),
            help_row(prefix, "myloc", "Show your saved location"),
            help_row(prefix, "delloc", "Remove your saved location"),
        ]


def setup(bot: object) -> LocationModule:
    """Module entry point - returns a LocationModule instance."""
    return LocationModule(bot)  # type: ignore[arg-type]
