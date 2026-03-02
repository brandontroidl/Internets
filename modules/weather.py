"""
Weather module — current conditions and 4-day forecast.

Routing:
  US locations   → weather.gov API (NWS)   — no key required
  Non-US         → Open-Meteo API          — no key required, worldwide

Geocoding via OpenStreetMap Nominatim — worldwide, no key required.

Commands: .weather (.w), .forecast (.f)
"""

import re
import requests
import logging
from datetime import datetime, timezone
from .base import BotModule

log = logging.getLogger("internets.weather")

# ── WMO weather interpretation codes (Open-Meteo) ────────────────────────────

WMO_CODES = {
    0:  "Clear",
    1:  "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    56: "Freezing Drizzle", 57: "Heavy Freezing Drizzle",
    61: "Slight Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Heavy Freezing Rain",
    71: "Slight Snow", 73: "Snow", 75: "Heavy Snow",
    77: "Snow Grains",
    80: "Slight Showers", 81: "Showers", 82: "Violent Showers",
    85: "Snow Showers", 86: "Heavy Snow Showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ Hail", 99: "Thunderstorm w/ Heavy Hail",
}

# ── US state abbreviations ────────────────────────────────────────────────────

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

def kph_mph(kph) -> str:
    if kph is None: return "N/A"
    return f"{kph:.1f}km/h / {kph/1.609:.1f} mph"

def kph_mph_from_ms(mps) -> str:
    if mps is None: return "N/A"
    return f"{mps*3.6:.1f}km/h / {mps*2.237:.1f} mph"

def km_mi(m) -> str:
    if m is None: return "N/A"
    return f"{m/1000:.1f}km / {m/1609.344:.1f}mi"

def mb_in_from_pa(pa) -> str:
    if pa is None: return "N/A"
    return f"{pa/100:.0f}mb / {pa/3386.39:.2f}in"

def mb_in(mb) -> str:
    if mb is None: return "N/A"
    return f"{mb:.0f}mb / {mb/33.864:.2f}in"

def deg_to_card(deg) -> str:
    if deg is None: return ""
    return WIND_DIRS[round(deg / 22.5) % 16]

def fmt_updated(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %I:%M %p %Z")
    except Exception:
        return iso or "N/A"

# ── Geocoding — worldwide ─────────────────────────────────────────────────────

def geocode(query: str, user_agent: str):
    """
    Returns (lat, lon, display_name, country_code) or None.
    country_code is ISO 3166-1 alpha-2 lowercase e.g. 'us', 'gb', 'se'.
    """
    query = query.strip().strip("'\"")
    hdrs  = {"User-Agent": user_agent}

    # Raw lat,lon — reverse geocode for country
    m = re.match(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$", query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        try:
            r  = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
                headers=hdrs, timeout=10
            )
            d    = r.json()
            addr = d.get("address", {})
            cc   = addr.get("country_code", "").lower()
            city = (addr.get("city") or addr.get("town") or
                    addr.get("village") or d.get("display_name", f"{lat:.4f},{lon:.4f}"))
            return lat, lon, city, cc
        except Exception:
            return lat, lon, f"{lat:.4f},{lon:.4f}", ""

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers=hdrs, timeout=10
        )
        results = r.json()
        if not results:
            return None
        hit  = results[0]
        lat  = float(hit["lat"])
        lon  = float(hit["lon"])
        addr = hit.get("address", {})
        cc   = addr.get("country_code", "").lower()
        city = (addr.get("city") or addr.get("town") or
                addr.get("village") or addr.get("county") or "")
        if cc == "us":
            state   = STATE_ABBR.get(addr.get("state", ""), addr.get("state", ""))
            display = f"{city}, {state}".strip(", ") if city or state else hit["display_name"]
        else:
            country = addr.get("country", "")
            display = f"{city}, {country}".strip(", ") if city or country else hit["display_name"]
        return lat, lon, display, cc
    except Exception as e:
        log.warning(f"Geocode error '{query}': {e}")
    return None

# ── weather.gov (NWS) — US only ───────────────────────────────────────────────

def nws_get_gridpoint(lat, lon, headers):
    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers=headers, timeout=10
        )
        r.raise_for_status()
        return r.json().get("properties")
    except Exception as e:
        log.warning(f"NWS gridpoint error: {e}")
    return None

def nws_current(lat, lon, grid, headers) -> str:
    try:
        r    = requests.get(grid["observationStations"], headers=headers, timeout=10)
        feat = r.json()["features"][0]["properties"]
        r2   = requests.get(
            f"https://api.weather.gov/stations/{feat['stationIdentifier']}/observations/latest",
            headers=headers, timeout=10
        )
        obs = r2.json()["properties"]

        temp_c   = obs.get("temperature",       {}).get("value")
        dewpt_c  = obs.get("dewpoint",           {}).get("value")
        hi_c     = obs.get("heatIndex",          {}).get("value")
        humidity = obs.get("relativeHumidity",   {}).get("value")
        wind_ms  = obs.get("windSpeed",          {}).get("value")
        wind_deg = obs.get("windDirection",      {}).get("value")
        pressure = obs.get("barometricPressure", {}).get("value")
        visib    = obs.get("visibility",         {}).get("value")
        desc     = obs.get("textDescription", "N/A") or "N/A"
        updated  = fmt_updated(obs.get("timestamp", ""))

        if wind_ms is not None and wind_ms < 0.5:
            wind_str = "Calm"
        elif wind_ms is not None:
            card = deg_to_card(wind_deg)
            wind_str = f"from {card} at {kph_mph_from_ms(wind_ms)}" if card else kph_mph_from_ms(wind_ms)
        else:
            wind_str = "N/A"

        parts = [f"Conditions {desc}", f"Temperature {cf(temp_c)}"]
        if hi_c is not None:
            parts.append(f"Heat index {cf(hi_c)}")
        parts += [
            f"Dew point {cf(dewpt_c)}",
            f"Pressure {mb_in_from_pa(pressure)}",
            f"Humidity {f'{humidity:.0f}%' if humidity is not None else 'N/A'}",
            f"Visibility {km_mi(visib)}",
            f"Wind {wind_str}",
            f"Last Updated on {updated}",
        ]
        return " :: ".join(parts)
    except Exception as e:
        log.warning(f"NWS observation error: {e}")
    return None

def nws_forecast(grid, headers) -> str:
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
                days.append((p["name"], p.get("shortForecast", ""), high_c, low_c))
            else:
                i += 1
        if not days:
            return None
        return " :: ".join(
            f"{n} {c} {cf(h)} {cf(l) if l is not None else 'N/A'}"
            for n, c, h, l in days
        )
    except Exception as e:
        log.warning(f"NWS forecast error: {e}")
    return None

# ── Open-Meteo — worldwide (non-US) ──────────────────────────────────────────

OM_BASE = "https://api.open-meteo.com/v1/forecast"

def om_current(lat, lon) -> str:
    try:
        r = requests.get(OM_BASE, params={
            "latitude":        lat,
            "longitude":       lon,
            "current":         ",".join([
                "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                "dew_point_2m", "weather_code", "surface_pressure",
                "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "visibility",
            ]),
            "wind_speed_unit": "kmh",
            "timezone":        "auto",
        }, timeout=10)
        r.raise_for_status()
        cur = r.json().get("current", {})

        temp_c    = cur.get("temperature_2m")
        feels_c   = cur.get("apparent_temperature")
        dewpt_c   = cur.get("dew_point_2m")
        humidity  = cur.get("relative_humidity_2m")
        wcode     = cur.get("weather_code")
        pressure  = cur.get("surface_pressure")
        wind_kph  = cur.get("wind_speed_10m")
        wind_deg  = cur.get("wind_direction_10m")
        gusts_kph = cur.get("wind_gusts_10m")
        visib_m   = cur.get("visibility")
        updated   = fmt_updated(cur.get("time", ""))

        desc = WMO_CODES.get(wcode, f"Code {wcode}") if wcode is not None else "N/A"

        if wind_kph is not None and wind_kph < 1:
            wind_str = "Calm"
        elif wind_kph is not None:
            card = deg_to_card(wind_deg)
            wind_str = f"from {card} at {kph_mph(wind_kph)}" if card else kph_mph(wind_kph)
            if gusts_kph and gusts_kph > wind_kph * 1.3:
                wind_str += f" (gusts {kph_mph(gusts_kph)})"
        else:
            wind_str = "N/A"

        parts = [f"Conditions {desc}", f"Temperature {cf(temp_c)}"]
        if feels_c is not None and abs((feels_c or 0) - (temp_c or 0)) >= 2:
            parts.append(f"Feels like {cf(feels_c)}")
        parts += [
            f"Dew point {cf(dewpt_c)}",
            f"Pressure {mb_in(pressure)}",
            f"Humidity {f'{humidity:.0f}%' if humidity is not None else 'N/A'}",
            f"Visibility {km_mi(visib_m)}",
            f"Wind {wind_str}",
            f"Last Updated on {updated}",
        ]
        return " :: ".join(parts)
    except Exception as e:
        log.warning(f"Open-Meteo current error: {e}")
    return None

def om_forecast(lat, lon) -> str:
    try:
        r = requests.get(OM_BASE, params={
            "latitude":      lat,
            "longitude":     lon,
            "daily":         ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
            ]),
            "timezone":      "auto",
            "forecast_days": 4,
        }, timeout=10)
        r.raise_for_status()
        daily = r.json().get("daily", {})

        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows  = daily.get("temperature_2m_min", [])

        if not dates:
            return None

        chunks = []
        for i in range(min(4, len(dates))):
            try:
                name = datetime.fromisoformat(dates[i]).strftime("%A")
            except Exception:
                name = dates[i]
            code   = codes[i] if i < len(codes) else None
            desc   = WMO_CODES.get(code, "N/A") if code is not None else "N/A"
            high_c = highs[i] if i < len(highs) else None
            low_c  = lows[i]  if i < len(lows)  else None
            chunks.append(f"{name} {desc} {cf(high_c)} {cf(low_c)}")

        return " :: ".join(chunks)
    except Exception as e:
        log.warning(f"Open-Meteo forecast error: {e}")
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

    def on_unload(self):
        log.info("WeatherModule unloaded")

    def _resolve_arg(self, nick: str, arg):
        if arg:
            arg = arg.strip()
            m = re.match(r"^-n\s+(\S+)$", arg, re.IGNORECASE)
            if m:
                target = m.group(1)
                saved  = self.bot.loc_get(target)
                if saved:
                    return saved, ""
                return None, f"{target} hasn't registered a location."
            return arg, ""
        saved = self.bot.loc_get(nick)
        if saved:
            return saved, ""
        p = self.bot.cfg["bot"]["command_prefix"]
        return None, f"{nick}: no location saved — try {p}regloc <city or zip> first."

    def _fetch(self, nick, reply_to, arg, mode):
        if self.bot.rate_limited(nick):
            self.bot.privmsg(reply_to, f"{nick}: slow down! ({self.cooldown}s cooldown)")
            return

        raw, err = self._resolve_arg(nick, arg)
        if raw is None:
            self.bot.privmsg(reply_to, err)
            return

        geo = geocode(raw, self.user_agent)
        if geo is None:
            self.bot.privmsg(reply_to, f"{nick}: couldn't find '{raw}'.")
            return

        lat, lon, display, cc = geo
        is_us = (cc == "us")
        log.info(f"Weather lookup: '{raw}' -> {display} ({cc.upper() or '?'}) [{lat:.4f},{lon:.4f}]")

        body = None
        if is_us:
            # Try weather.gov first; fall back to Open-Meteo for territories without NWS coverage
            grid = nws_get_gridpoint(lat, lon, self.nws_headers)
            if grid:
                body = (nws_current(lat, lon, grid, self.nws_headers)
                        if mode == "weather"
                        else nws_forecast(grid, self.nws_headers))
            else:
                log.info(f"NWS has no grid for {display} — falling back to Open-Meteo")

        if body is None:
            # Non-US or NWS fallback
            body = om_current(lat, lon) if mode == "weather" else om_forecast(lat, lon)

        if body:
            self.bot.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.bot.privmsg(reply_to, f"{nick}: couldn't fetch weather data right now.")

    def cmd_weather(self, nick, reply_to, arg):
        self._fetch(nick, reply_to, arg, "weather")

    def cmd_forecast(self, nick, reply_to, arg):
        self._fetch(nick, reply_to, arg, "forecast")

    def help_lines(self, prefix):
        return [
            f"  {prefix}weather  [zip|city|-n nick]   Current conditions (worldwide)",
            f"  {prefix}w        [zip|city|-n nick]   Alias for {prefix}weather",
            f"  {prefix}forecast [zip|city|-n nick]   4-day forecast (worldwide)",
            f"  {prefix}f        [zip|city|-n nick]   Alias for {prefix}forecast",
        ]


def setup(bot):
    return WeatherModule(bot)
