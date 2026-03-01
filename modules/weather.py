"""
Weather module — current conditions and forecasts via weather.gov API.
Commands: .weather (.w), .forecast (.f)
"""

import re
import requests
import logging
from .base import BotModule

log = logging.getLogger("internets.weather")

NWS_HEADERS = {}   # populated on load from bot config
NOM_HEADERS = {}

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

WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSW","SW","WSW","W","WNW","NW","NNW"]

# ── Unit helpers ──────────────────────────────────────────────────────────────

def cf(c) -> str:
    if c is None: return "N/A"
    return f"{c:.1f}C / {c*9/5+32:.1f}F"

def kph_mph(mps) -> str:
    if mps is None: return "N/A"
    return f"{mps*3.6:.1f}km/h / {mps*2.237:.1f} mph"

def km_mi(m) -> str:
    if m is None: return "N/A"
    return f"{m/1000:.1f}km / {m/1609.344:.1f}mi"

def mb_in(pa) -> str:
    if pa is None: return "N/A"
    return f"{pa/100:.0f}mb / {pa/3386.39:.2f}in"

def deg_to_card(deg) -> str:
    if deg is None: return ""
    return WIND_DIRS[round(deg / 22.5) % 16]

# ── Geocoding ─────────────────────────────────────────────────────────────────

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
                    "addressdetails": 1, "countrycodes": "us"},
            headers={"User-Agent": user_agent}, timeout=10
        )
        results = r.json()
        if not results: return None
        hit   = results[0]
        lat   = float(hit["lat"])
        lon   = float(hit["lon"])
        addr  = hit.get("address", {})
        city  = (addr.get("city") or addr.get("town") or
                 addr.get("village") or addr.get("county") or "")
        state = STATE_ABBR.get(addr.get("state",""), addr.get("state",""))
        display = f"{city}, {state}".strip(", ") if city or state else hit["display_name"]
        return lat, lon, display
    except Exception as e:
        log.warning(f"Geocode error '{query}': {e}")
    return None

# ── NWS API ───────────────────────────────────────────────────────────────────

def get_gridpoint(lat, lon, headers):
    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers=headers, timeout=10
        )
        r.raise_for_status()
        return r.json().get("properties")
    except Exception as e:
        log.warning(f"Gridpoint error: {e}")
    return None

def get_current(lat, lon, grid, headers) -> str:
    from datetime import datetime
    try:
        r    = requests.get(grid["observationStations"], headers=headers, timeout=10)
        feat = r.json()["features"][0]["properties"]
        r2   = requests.get(
            f"https://api.weather.gov/stations/{feat['stationIdentifier']}/observations/latest",
            headers=headers, timeout=10
        )
        obs  = r2.json()["properties"]
        temp_c  = obs.get("temperature",       {}).get("value")
        dewpt_c = obs.get("dewpoint",           {}).get("value")
        hi_c    = obs.get("heatIndex",          {}).get("value")
        humidity= obs.get("relativeHumidity",   {}).get("value")
        wind_ms = obs.get("windSpeed",          {}).get("value")
        wind_deg= obs.get("windDirection",      {}).get("value")
        pressure= obs.get("barometricPressure", {}).get("value")
        visib   = obs.get("visibility",         {}).get("value")
        desc    = obs.get("textDescription", "N/A") or "N/A"
        ts_raw  = obs.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z","+00:00"))
            updated = dt.strftime("%B %d, %I:%M %p %Z")
        except Exception:
            updated = ts_raw or "N/A"
        if wind_ms is not None and wind_ms < 0.5:
            wind_str = "Calm"
        elif wind_ms is not None:
            card = deg_to_card(wind_deg)
            wind_str = f"from {card} at {kph_mph(wind_ms)}" if card else kph_mph(wind_ms)
        else:
            wind_str = "N/A"
        parts = [f"Conditions {desc}", f"Temperature {cf(temp_c)}"]
        if hi_c is not None:
            parts.append(f"Heat index {cf(hi_c)}")
        parts += [
            f"Dew point {cf(dewpt_c)}",
            f"Pressure {mb_in(pressure)}",
            f"Humidity {f'{humidity:.0f}%' if humidity is not None else 'N/A'}",
            f"Visibility {km_mi(visib)}",
            f"Wind {wind_str}",
            f"Last Updated on {updated}",
        ]
        return " :: ".join(parts)
    except Exception as e:
        log.warning(f"Observation error: {e}")
    return None

def get_forecast_line(grid, headers) -> str:
    try:
        r = requests.get(grid["forecast"], headers=headers, timeout=10)
        periods = r.json()["properties"]["periods"]
        days, i = [], 0
        while i < len(periods) and len(days) < 4:
            p = periods[i]
            if p["isDaytime"]:
                high_c = (p["temperature"]-32)*5/9 if p["temperatureUnit"]=="F" else p["temperature"]
                low_c  = None
                if i+1 < len(periods) and not periods[i+1]["isDaytime"]:
                    nt    = periods[i+1]
                    low_c = (nt["temperature"]-32)*5/9 if nt["temperatureUnit"]=="F" else nt["temperature"]
                    i += 2
                else:
                    i += 1
                days.append((p["name"], p.get("shortForecast",""), high_c, low_c))
            else:
                i += 1
        if not days: return None
        return " :: ".join(
            f"{n} {c} {cf(h)} {cf(l) if l is not None else 'N/A'}"
            for n, c, h, l in days
        )
    except Exception as e:
        log.warning(f"Forecast error: {e}")
    return None

# ── Module class ──────────────────────────────────────────────────────────────

class WeatherModule(BotModule):
    COMMANDS = {
        "weather":  "cmd_weather",
        "w":        "cmd_weather",
        "forecast": "cmd_forecast",
        "f":        "cmd_forecast",
    }

    def on_load(self):
        ua = self.bot.cfg["weather"]["user_agent"]
        self.nws_headers = {"User-Agent": ua, "Accept": "application/geo+json"}
        self.user_agent  = ua
        self.cooldown    = int(self.bot.cfg["bot"]["api_cooldown"])
        log.info("WeatherModule loaded")

    def _resolve_arg(self, nick: str, arg):
        """Returns (raw_location | None, error_str)."""
        if arg:
            arg = arg.strip()
            m = re.match(r"^-n\s+(\S+)$", arg, re.IGNORECASE)
            if m:
                target = m.group(1)
                saved  = self.bot.loc_get(target)
                if saved: return saved, ""
                return None, f"{target} hasn't registered a location."
            return arg, ""
        saved = self.bot.loc_get(nick)
        if saved: return saved, ""
        p = self.bot.cfg["bot"]["command_prefix"]
        return None, f"{nick}: no location saved — try {p}regloc <zip or city> first."

    def _weather_or_forecast(self, nick, reply_to, arg, mode):
        if self.bot.rate_limited(nick):
            self.bot.privmsg(reply_to, f"{nick}: slow down! ({self.cooldown}s cooldown)")
            return
        raw, err = self._resolve_arg(nick, arg)
        if raw is None:
            self.bot.privmsg(reply_to, err); return
        geo = geocode(raw, self.user_agent)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: couldn't find '{raw}' (US locations only)."); return
        lat, lon, display = geo
        grid = get_gridpoint(lat, lon, self.nws_headers)
        if grid is None:
            self.bot.privmsg(reply_to, f"{nick}: weather.gov has no data for that location."); return
        if mode == "weather":
            body = get_current(lat, lon, grid, self.nws_headers)
        else:
            body = get_forecast_line(grid, self.nws_headers)
        if body:
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: couldn't fetch data right now.")

    def cmd_weather(self, nick, reply_to, arg):
        self._weather_or_forecast(nick, reply_to, arg, "weather")

    def cmd_forecast(self, nick, reply_to, arg):
        self._weather_or_forecast(nick, reply_to, arg, "forecast")

    def help_lines(self, prefix):
        return [
            f"  {prefix}weather  [zip|city|-n nick]   Current conditions",
            f"  {prefix}w        [zip|city|-n nick]   Alias for {prefix}weather",
            f"  {prefix}forecast [zip|city|-n nick]   4-day forecast",
            f"  {prefix}f        [zip|city|-n nick]   Alias for {prefix}forecast",
        ]


def setup(bot):
    return WeatherModule(bot)
