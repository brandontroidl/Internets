"""
Location module — register, view, and delete per-nick default locations.
Commands: .regloc, .register_location, .myloc, .delloc
"""

import re
import logging
import requests
from .base import BotModule

log = logging.getLogger("internets.location")

STATE_ABBR = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
    "Colorado":"CO","Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA",
    "Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA",
    "Kansas":"KS","Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD",
    "Massachusetts":"MA","Michigan":"MI","Minnesota":"MN","Mississippi":"MS",
    "Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV","New Hampshire":"NH",
    "New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
    "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN",
    "Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA",
    "West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY","District of Columbia":"DC",
}

def geocode(query: str, user_agent: str):
    query = query.strip().strip("'\"")
    m = re.match(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$", query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return lat, lon, f"{lat:.4f},{lon:.4f}"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "addressdetails": 1},
            headers={"User-Agent": user_agent}, timeout=10
        )
        results = r.json()
        if not results: return None
        hit   = results[0]
        lat   = float(hit["lat"])
        lon   = float(hit["lon"])
        addr  = hit.get("address", {})
        cc   = addr.get("country_code", "").lower()
        city = (addr.get("city") or addr.get("town") or
                addr.get("village") or addr.get("county") or "")
        if cc == "us":
            state   = STATE_ABBR.get(addr.get("state",""), addr.get("state",""))
            display = f"{city}, {state}".strip(", ") if city or state else hit["display_name"]
        else:
            country = addr.get("country", "")
            display = f"{city}, {country}".strip(", ") if city or country else hit["display_name"]
        return lat, lon, display
    except Exception as e:
        log.warning(f"Geocode error: {e}")
    return None


class LocationModule(BotModule):
    COMMANDS = {
        "register_location": "cmd_regloc",
        "regloc":            "cmd_regloc",
        "myloc":             "cmd_myloc",
        "delloc":            "cmd_delloc",
    }

    def on_load(self):
        self.user_agent = self.bot.cfg["weather"]["user_agent"]
        log.info("LocationModule loaded")

    def cmd_regloc(self, nick, reply_to, arg):
        if not arg:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: usage: {p}regloc <zip or city name>")
            return
        geo = geocode(arg, self.user_agent)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: couldn't find '{arg}'.")
            return
        _, _, display = geo
        self.bot.loc_set(nick, arg)
        self.bot.privmsg(reply_to, f"{nick}: registered location {display}")
        log.info(f"regloc: {nick} -> {arg!r} ({display})")

    def cmd_myloc(self, nick, reply_to, arg):
        raw = self.bot.loc_get(nick)
        if raw:
            geo     = geocode(raw, self.user_agent)
            display = geo[2] if geo else raw
            self.bot.privmsg(reply_to, f"{nick}: your saved location is {display} ({raw!r})")
        else:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: no saved location. Use {p}regloc <zip or city>.")

    def cmd_delloc(self, nick, reply_to, arg):
        if self.bot.loc_del(nick):
            self.bot.privmsg(reply_to, f"{nick}: your saved location has been removed.")
        else:
            self.bot.privmsg(reply_to, f"{nick}: you have no saved location to remove.")

    def help_lines(self, prefix):
        return [
            f"  {prefix}regloc            <zip|city>   Save your default location",
            f"  {prefix}register_location <zip|city>   Alias for {prefix}regloc",
            f"  {prefix}myloc                          Show your saved location",
            f"  {prefix}delloc                         Remove your saved location",
        ]


def setup(bot):
    return LocationModule(bot)
