#!/usr/bin/env python3
"""
IRC Bot — SSL, weather.gov API, classic IRC output style

Commands (prefix configurable in config.ini, default: .)
  .weather  [zip|city|-n nick]        Current conditions
  .w        [zip|city|-n nick]        Alias for .weather
  .forecast [zip|city|-n nick]        4-day forecast
  .f        [zip|city|-n nick]        Alias for .forecast
  .register_location <zip|city>       Register your default location
  .regloc            <zip|city>       Alias for .register_location
  .myloc                              Show your saved location
  .delloc                             Remove your saved location
  .cc   <expression>                  Calculator  e.g. .cc 2pi
  .d    [X]dN[+/-M]                   Dice roller  e.g. .d 3d6+2
  .u    <word> [/N]                   Urban Dictionary lookup
  .urbandictionary <word> [/N]        Alias for .u
  .t    [src] <tgt> <text>            Translate  e.g. .t en es Hello
  .translate [src] <tgt> <text>       Alias for .t
  .join <#channel>                    Join a channel (works in PM)
  .part <#channel>                    Leave a channel
  .help                               Command list

  -n nick  Use another user's registered location
  In PM you can drop the prefix — e.g. /MSG BotNick WEATHER 90210

weather.gov API: https://www.weather.gov/documentation/services-web-api
Geocoding: OpenStreetMap Nominatim (no key required)
Translation: MyMemory API (no key required)
"""

import ssl, socket, time, threading, logging, configparser
import requests, sys, re, json, math, random
from pathlib import Path
from datetime import datetime, timezone

# ─── Config ───────────────────────────────────────────────────────────────────

cfg = configparser.ConfigParser()
cfg.read("config.ini")

IRC_SERVER   = cfg["irc"]["server"]
IRC_PORT     = int(cfg["irc"]["port"])
NICKNAME     = cfg["irc"]["nickname"]
REALNAME     = cfg["irc"]["realname"]
CHANNELS     = [c.strip() for c in cfg["irc"]["channels"].split(",")]
NICKSERV_PW  = cfg["irc"].get("nickserv_password", "").strip()

CMD_PREFIX   = cfg["bot"]["command_prefix"]
API_COOLDOWN = int(cfg["bot"]["api_cooldown"])
DEFAULT_LOC  = cfg["bot"]["default_location"]
LOC_FILE     = cfg["bot"].get("locations_file", "locations.json")

USER_AGENT   = cfg["weather"]["user_agent"]

LOG_LEVEL    = cfg["logging"]["level"]
LOG_FILE     = cfg["logging"]["log_file"]

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("internets")

NWS_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
NOM_HEADERS = {"User-Agent": USER_AGENT}

# ─── Location store ───────────────────────────────────────────────────────────

_loc_lock = threading.Lock()

def _load() -> dict:
    try:
        if Path(LOC_FILE).exists():
            return json.loads(Path(LOC_FILE).read_text())
    except Exception as e:
        log.warning(f"Load {LOC_FILE}: {e}")
    return {}

def _save(data: dict):
    try:
        Path(LOC_FILE).write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning(f"Save {LOC_FILE}: {e}")

def loc_get(nick: str):
    with _loc_lock:
        return _load().get(nick.lower())

def loc_set(nick: str, raw: str):
    with _loc_lock:
        d = _load(); d[nick.lower()] = raw; _save(d)

def loc_del(nick: str) -> bool:
    with _loc_lock:
        d = _load()
        if nick.lower() in d:
            del d[nick.lower()]; _save(d); return True
        return False

# ─── Rate limiting ────────────────────────────────────────────────────────────

_last_call: dict = {}

def rate_limited(nick: str) -> bool:
    now = time.time()
    if nick in _last_call and now - _last_call[nick] < API_COOLDOWN:
        return True
    _last_call[nick] = now
    return False

# ─── Unit helpers ─────────────────────────────────────────────────────────────

def cf(c) -> str:
    if c is None: return "N/A"
    f = c * 9 / 5 + 32
    return f"{c:.1f}C / {f:.1f}F"

def kph_mph(mps) -> str:
    if mps is None: return "N/A"
    return f"{mps*3.6:.1f}km/h / {mps*2.237:.1f} mph"

def km_mi(m) -> str:
    if m is None: return "N/A"
    return f"{m/1000:.1f}km / {m/1609.344:.1f}mi"

def mb_in(pa) -> str:
    if pa is None: return "N/A"
    return f"{pa/100:.0f}mb / {pa/3386.39:.2f}in"

WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSW","SW","WSW","W","WNW","NW","NNW"]

def deg_to_card(deg) -> str:
    if deg is None: return ""
    return WIND_DIRS[round(deg / 22.5) % 16]

# ─── Geocoding ────────────────────────────────────────────────────────────────

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

def geocode(query: str):
    """Returns (lat, lon, display_name) or None. Accepts zip, city name, lat,lon."""
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
            headers=NOM_HEADERS, timeout=10
        )
        results = r.json()
        if not results: return None
        hit   = results[0]
        lat   = float(hit["lat"])
        lon   = float(hit["lon"])
        addr  = hit.get("address", {})
        city  = (addr.get("city") or addr.get("town") or
                 addr.get("village") or addr.get("county") or "")
        state = STATE_ABBR.get(addr.get("state", ""), addr.get("state", ""))
        display = f"{city}, {state}".strip(", ") if city or state else hit["display_name"]
        return lat, lon, display
    except Exception as e:
        log.warning(f"Geocode error '{query}': {e}")
    return None

# ─── weather.gov API ──────────────────────────────────────────────────────────

def get_gridpoint(lat: float, lon: float):
    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers=NWS_HEADERS, timeout=10
        )
        r.raise_for_status()
        return r.json().get("properties")
    except Exception as e:
        log.warning(f"Gridpoint error: {e}")
    return None


def get_current(lat: float, lon: float, grid: dict) -> str:
    try:
        r       = requests.get(grid["observationStations"], headers=NWS_HEADERS, timeout=10)
        feat    = r.json()["features"][0]["properties"]
        sta_id  = feat["stationIdentifier"]
        sta_name = feat["name"]

        r2   = requests.get(
            f"https://api.weather.gov/stations/{sta_id}/observations/latest",
            headers=NWS_HEADERS, timeout=10
        )
        obs = r2.json()["properties"]

        temp_c  = obs.get("temperature",       {}).get("value")
        dewpt_c = obs.get("dewpoint",           {}).get("value")
        hi_c    = obs.get("heatIndex",          {}).get("value")
        humidity= obs.get("relativeHumidity",   {}).get("value")
        wind_ms = obs.get("windSpeed",          {}).get("value")
        wind_deg= obs.get("windDirection",      {}).get("value")
        pressure= obs.get("barometricPressure", {}).get("value")
        visib   = obs.get("visibility",         {}).get("value")
        desc    = obs.get("textDescription", "N/A") or "N/A"

        ts_raw = obs.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
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


def get_forecast_line(grid: dict) -> str:
    try:
        r = requests.get(grid["forecast"], headers=NWS_HEADERS, timeout=10)
        periods = r.json()["properties"]["periods"]
        days, i = [], 0
        while i < len(periods) and len(days) < 4:
            p = periods[i]
            if p["isDaytime"]:
                high_c = (p["temperature"] - 32) * 5 / 9 if p["temperatureUnit"] == "F" else p["temperature"]
                low_c  = None
                if i + 1 < len(periods) and not periods[i+1]["isDaytime"]:
                    nt = periods[i+1]
                    low_c = (nt["temperature"] - 32) * 5 / 9 if nt["temperatureUnit"] == "F" else nt["temperature"]
                    i += 2
                else:
                    i += 1
                days.append((p["name"], p.get("shortForecast", ""), high_c, low_c))
            else:
                i += 1
        if not days: return None
        chunks = []
        for name, cond, high_c, low_c in days:
            chunks.append(f"{name} {cond} {cf(high_c)} {cf(low_c) if low_c is not None else 'N/A'}")
        return " :: ".join(chunks)
    except Exception as e:
        log.warning(f"Forecast error: {e}")
    return None

# ─── Calculator ───────────────────────────────────────────────────────────────

_CALC_GLOBALS = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_CALC_GLOBALS.update({"pi": math.pi, "e": math.e, "abs": abs, "round": round})

def safe_calc(expr: str) -> str:
    """Evaluate a math expression safely. No builtins = no code execution."""
    expr = expr.strip()
    # Expand implicit multiplication: 2pi → 2*pi
    expr = re.sub(r"(\d)(\s*)([a-zA-Z])", r"\1*\3", expr)
    expr = re.sub(r"([a-zA-Z])(\s*)(\d)", r"\1*\3", expr)
    try:
        result = eval(expr, {"__builtins__": {}}, _CALC_GLOBALS)
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        if isinstance(result, float):
            return f"{result:.8g}"
        return str(result)
    except ZeroDivisionError:
        return "Error: division by zero"
    except Exception as e:
        return f"Error: {e}"

# ─── Dice roller ──────────────────────────────────────────────────────────────

def roll_dice(expr: str) -> str:
    """
    Parse XdN[+/-M] or just N (single die).
    Returns :: Total X / Y [Z%] :: Results [...] ::
    """
    expr = expr.strip().lower().replace(" ", "")
    m = re.match(r"^(?:(\d+)d)?(\d+)([+-]\d+)?$", expr)
    if not m:
        return "Invalid dice format. Use: N  or  XdN  or  XdN+M"
    count_s, sides_s, mod_s = m.groups()
    count = int(count_s) if count_s else 1
    sides = int(sides_s)
    mod   = int(mod_s) if mod_s else 0
    if count < 1 or count > 100:  return "Dice count must be 1-100."
    if sides < 2 or sides > 10000: return "Sides must be 2-10000."
    rolls   = [random.randint(1, sides) for _ in range(count)]
    total   = sum(rolls) + mod
    maximum = sides * count + mod
    minimum = count + mod
    pct     = round((total - minimum) / max(maximum - minimum, 1) * 100)
    return f":: Total {total} / {maximum} [{pct}%] :: Results {rolls} ::"

# ─── Urban Dictionary ─────────────────────────────────────────────────────────

def urban_lookup(term: str, index: int = 1) -> str:
    try:
        r = requests.get(
            "https://api.urbandictionary.com/v0/define",
            params={"term": term},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        defs = r.json().get("list", [])
        if not defs:
            return f"No Urban Dictionary results for '{term}'."
        total = len(defs)
        idx   = max(1, min(index, total)) - 1
        defn  = defs[idx]["definition"].replace("\r", "").replace("\n", " ").strip()
        if len(defn) > 400:
            defn = defn[:397] + "..."
        return f"[{idx+1}/{total}] {defn}"
    except Exception as e:
        log.warning(f"UD error: {e}")
        return "Urban Dictionary lookup failed."

# ─── Translation ──────────────────────────────────────────────────────────────

SUPPORTED_LANGS = {
    "ar","bg","ca","cs","da","nl","en","et","fi","fr","de","el","hi",
    "hu","id","it","ja","ko","lv","lt","no","fa","pl","pt","ro","ru",
    "sk","sl","es","sv","th","tr","uk","vi",
}

def translate_text(src_lang, tgt_lang: str, text: str) -> str:
    """
    Translate using the unofficial Google Translate gtx endpoint.
    No API key required. src_lang=None means auto-detect.
    """
    sl = src_lang if src_lang else "auto"
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl":     sl,
                "tl":     tgt_lang,
                "dt":     "t",
                "q":      text,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        data = r.json()
        # Response is nested lists: data[0] = list of [translated_chunk, original_chunk, ...]
        translated = "".join(part[0] for part in data[0] if part[0])
        # data[2] = detected source language code
        detected = data[2] if len(data) > 2 and data[2] else sl
        if not translated:
            return "Translation returned empty result."
        return f"[t] [from {detected}] -> {translated}"
    except Exception as e:
        log.warning(f"Translate error: {e}")
        return "Translation failed."

# ─── Help ─────────────────────────────────────────────────────────────────────

def build_help(botnick: str) -> list:
    p = CMD_PREFIX
    return [
        f"── {botnick} Commands ──────────────────────────────────────────────────",
        f"  {p}weather  [zip|city|-n nick]    Current conditions",
        f"  {p}w        [zip|city|-n nick]    Alias for {p}weather",
        f"  {p}forecast [zip|city|-n nick]    4-day forecast",
        f"  {p}f        [zip|city|-n nick]    Alias for {p}forecast",
        f"  {p}register_location <zip|city>   Save your default location",
        f"  {p}regloc            <zip|city>   Alias for {p}register_location",
        f"  {p}myloc                          Show your saved location",
        f"  {p}delloc                         Remove your saved location",
        f"  {p}cc  <expression>               Calculator  e.g. {p}cc 2pi",
        f"  {p}d   [X]dN[+/-M]               Dice roller  e.g. {p}d 3d6+2",
        f"  {p}u   <word> [/N]               Urban Dictionary  e.g. {p}u jason /2",
        f"  {p}urbandictionary <word> [/N]   Alias for {p}u",
        f"  {p}t   [src] <tgt> <text>        Translate  e.g. {p}t en es Hello",
        f"  {p}translate [src] <tgt> <text>  Alias for {p}t",
        f"  {p}join  <#channel>              Ask me to join a channel (works in PM)",
        f"  {p}part  <#channel>              Ask me to leave a channel",
        f"  {p}help                          This message",
        f"────────────────────────────────────────────────────────────────────────",
        f"  -n nick  Use another user's saved location",
        f"  In PM you can drop the '{p}' — e.g.  /MSG {botnick} WEATHER 90210",
        f"────────────────────────────────────────────────────────────────────────",
    ]

# ─── IRC Bot ──────────────────────────────────────────────────────────────────

class IRCBot:
    def __init__(self):
        self.sock = None
        self._lock = threading.Lock()
        self.active_channels: set = set()

    # ── low-level ──────────────────────────────────────────────────────────

    def connect(self):
        use_ssl    = cfg["irc"].getboolean("ssl",        fallback=True)
        ssl_verify = cfg["irc"].getboolean("ssl_verify", fallback=True)
        log.info(
            f"Connecting to {IRC_SERVER}:{IRC_PORT} "
            f"({'SSL' if use_ssl else 'plain'}"
            f"{', no cert verify' if use_ssl and not ssl_verify else ''})"
        )
        raw = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=30)
        if use_ssl:
            ctx = ssl.create_default_context()
            if not ssl_verify:
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(raw, server_hostname=IRC_SERVER)
        else:
            self.sock = raw
        self.sock.settimeout(300)  # 5 min recv window; keepalive pings every 90s
        self._start_keepalive()
        self.send(f"NICK {NICKNAME}")
        self.send(f"USER {NICKNAME} 0 * :{REALNAME}")

    def _start_keepalive(self):
        def _loop():
            while True:
                time.sleep(90)
                try:
                    self.send(f"PING :{IRC_SERVER}")
                except Exception:
                    break
        threading.Thread(target=_loop, daemon=True, name="keepalive").start()

    def send(self, msg: str):
        with self._lock:
            log.debug(f">> {msg}")
            self.sock.sendall((msg + "\r\n").encode("utf-8", errors="replace"))

    def privmsg(self, target: str, msg: str):
        for chunk in [msg[i:i+450] for i in range(0, len(msg), 450)]:
            self.send(f"PRIVMSG {target} :{chunk}")
            time.sleep(0.4)

    def join_channels(self):
        for ch in CHANNELS:
            self.send(f"JOIN {ch}")
            self.active_channels.add(ch.lower())
            log.info(f"Joined {ch}")

    # ── location resolution ────────────────────────────────────────────────

    def resolve_arg(self, nick: str, arg):
        """
        Parse the argument for a weather/forecast command.
        Returns (raw_location_string | None, error_message).
        Handles: -n nick, freeform city/zip, empty (use own saved loc).
        """
        if arg:
            arg = arg.strip()
            m = re.match(r"^-n\s+(\S+)$", arg, re.IGNORECASE)
            if m:
                target_nick = m.group(1)
                saved = loc_get(target_nick)
                if saved:
                    return saved, ""
                return None, f"{target_nick} hasn't registered a location."
            return arg, ""
        saved = loc_get(nick)
        if saved:
            return saved, ""
        return None, (f"{nick}: no location given and none saved. "
                      f"Try {CMD_PREFIX}regloc <zip or city> first.")

    # ── command handlers ───────────────────────────────────────────────────

    def do_weather(self, nick: str, reply_to: str, arg):
        if rate_limited(nick):
            self.privmsg(reply_to, f"{nick}: slow down! ({API_COOLDOWN}s cooldown)")
            return
        raw, err = self.resolve_arg(nick, arg)
        if raw is None:
            self.privmsg(reply_to, err); return
        geo = geocode(raw)
        if geo is None:
            self.privmsg(reply_to, f"{nick}: couldn't find '{raw}' (US locations only)."); return
        lat, lon, display = geo
        grid = get_gridpoint(lat, lon)
        if grid is None:
            self.privmsg(reply_to, f"{nick}: weather.gov has no data for that location."); return
        body = get_current(lat, lon, grid)
        if body:
            self.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.privmsg(reply_to, f"{nick}: couldn't fetch conditions right now.")

    def do_forecast(self, nick: str, reply_to: str, arg):
        if rate_limited(nick):
            self.privmsg(reply_to, f"{nick}: slow down! ({API_COOLDOWN}s cooldown)")
            return
        raw, err = self.resolve_arg(nick, arg)
        if raw is None:
            self.privmsg(reply_to, err); return
        geo = geocode(raw)
        if geo is None:
            self.privmsg(reply_to, f"{nick}: couldn't find '{raw}' (US locations only)."); return
        lat, lon, display = geo
        grid = get_gridpoint(lat, lon)
        if grid is None:
            self.privmsg(reply_to, f"{nick}: weather.gov has no data for that location."); return
        body = get_forecast_line(grid)
        if body:
            self.privmsg(reply_to, f":: {display} :: {body} ::")
        else:
            self.privmsg(reply_to, f"{nick}: couldn't fetch forecast right now.")

    def do_regloc(self, nick: str, reply_to: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}regloc <zip or city name>"); return
        geo = geocode(arg)
        if geo is None:
            self.privmsg(reply_to, f"{nick}: couldn't find '{arg}' (US locations only)."); return
        _, _, display = geo
        loc_set(nick, arg)
        self.privmsg(reply_to, f"{nick}: registered location {display}")
        log.info(f"regloc: {nick} -> {arg!r} ({display})")

    def do_myloc(self, nick: str, reply_to: str):
        raw = loc_get(nick)
        if raw:
            geo     = geocode(raw)
            display = geo[2] if geo else raw
            self.privmsg(reply_to, f"{nick}: your saved location is {display} ({raw!r})")
        else:
            self.privmsg(reply_to, f"{nick}: no saved location. Use {CMD_PREFIX}regloc <zip or city>.")

    def do_delloc(self, nick: str, reply_to: str):
        if loc_del(nick):
            self.privmsg(reply_to, f"{nick}: your saved location has been removed.")
        else:
            self.privmsg(reply_to, f"{nick}: you have no saved location to remove.")

    def do_calc(self, nick: str, reply_to: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}cc <expression>  e.g. {CMD_PREFIX}cc 2pi"); return
        self.privmsg(reply_to, f"[calc] {arg} = {safe_calc(arg)}")

    def do_dice(self, nick: str, reply_to: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}d [X]dN[+/-M]  e.g. {CMD_PREFIX}d 3d6+2"); return
        self.privmsg(reply_to, roll_dice(arg))

    def do_ud(self, nick: str, reply_to: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}u <word> [/N]  e.g. {CMD_PREFIX}u jason /4"); return
        m = re.match(r"^(.+?)\s*/(\d+)$", arg.strip())
        if m:
            term, idx = m.group(1).strip(), int(m.group(2))
        else:
            term, idx = arg.strip(), 1
        self.privmsg(reply_to, urban_lookup(term, idx))

    def do_translate(self, nick: str, reply_to: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}t [src] <tgt> <text>  e.g. {CMD_PREFIX}t en es Hello"); return
        parts   = arg.strip().split(None, 2)
        lang_re = re.compile(r"^[a-z]{2}$")
        if len(parts) >= 3 and lang_re.match(parts[0]) and lang_re.match(parts[1]):
            src, tgt, text = parts[0], parts[1], parts[2]
        elif len(parts) >= 2 and lang_re.match(parts[0]):
            src, tgt, text = None, parts[0], " ".join(parts[1:])
        else:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}t [src] <tgt> <text>"); return
        self.privmsg(reply_to, translate_text(src, tgt, text))

    def do_join_part(self, nick: str, reply_to: str, cmd: str, arg):
        if not arg:
            self.privmsg(reply_to, f"{nick}: usage: {CMD_PREFIX}{cmd} <#channel>"); return
        if not re.match(r"^[#&+!][^\s,\x07]{1,49}$", arg):
            self.privmsg(reply_to, f"{nick}: '{arg}' doesn't look like a valid channel name."); return
        chan_lower = arg.lower()
        if cmd == "join":
            if chan_lower in self.active_channels:
                self.privmsg(reply_to, f"{nick}: I'm already in {arg}.")
            else:
                self.send(f"JOIN {arg}")
                self.active_channels.add(chan_lower)
                self.privmsg(reply_to, f"{nick}: joining {arg} ...")
                log.info(f"{nick} requested JOIN {arg}")
        else:
            if chan_lower not in self.active_channels:
                self.privmsg(reply_to, f"{nick}: I'm not in {arg}.")
            else:
                self.send(f"PART {arg} :Parting on request from {nick}")
                self.active_channels.discard(chan_lower)
                if chan_lower != reply_to.lower():
                    self.privmsg(reply_to, f"{nick}: left {arg}.")
                log.info(f"{nick} requested PART {arg}")

    def do_help(self, nick: str, reply_to: str):
        for line in build_help(NICKNAME):
            self.privmsg(reply_to, line)

    # ── dispatcher ────────────────────────────────────────────────────────

    CMD_ALIASES = {
        "weather":           "weather",
        "w":                 "weather",
        "forecast":          "forecast",
        "f":                 "forecast",
        "register_location": "regloc",
        "regloc":            "regloc",
        "myloc":             "myloc",
        "delloc":            "delloc",
        "cc":                "cc",
        "d":                 "dice",
        "urbandictionary":   "ud",
        "u":                 "ud",
        "translate":         "translate",
        "t":                 "translate",
        "join":              "join",
        "part":              "part",
        "help":              "help",
    }

    def dispatch(self, nick: str, reply_to: str, cmd: str, arg):
        canonical = self.CMD_ALIASES.get(cmd)
        if canonical is None: return

        def run(fn, *a):
            threading.Thread(target=fn, args=a, daemon=True).start()

        if   canonical == "weather":   run(self.do_weather,   nick, reply_to, arg)
        elif canonical == "forecast":  run(self.do_forecast,  nick, reply_to, arg)
        elif canonical == "regloc":    run(self.do_regloc,    nick, reply_to, arg)
        elif canonical == "myloc":     run(self.do_myloc,     nick, reply_to)
        elif canonical == "delloc":    run(self.do_delloc,    nick, reply_to)
        elif canonical == "cc":        run(self.do_calc,      nick, reply_to, arg)
        elif canonical == "dice":      run(self.do_dice,      nick, reply_to, arg)
        elif canonical == "ud":        run(self.do_ud,        nick, reply_to, arg)
        elif canonical == "translate": run(self.do_translate, nick, reply_to, arg)
        elif canonical in ("join","part"): run(self.do_join_part, nick, reply_to, canonical, arg)
        elif canonical == "help":      run(self.do_help,      nick, reply_to)

    # ── main loop ─────────────────────────────────────────────────────────

    def run(self):
        self.connect()
        buf = ""
        identified = False

        while True:
            try:
                data = self.sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    log.warning("Disconnected — reconnecting in 30s ...")
                    time.sleep(30)
                    self.connect()
                    identified = False
                    continue
                buf += data
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    log.debug(f"<< {line}")
                    self._process(line)
                    if "376" in line or "422" in line:
                        if not identified:
                            if NICKSERV_PW:
                                self.send(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PW}")
                                time.sleep(2)
                            self.join_channels()
                            identified = True

            except ssl.SSLError as e:
                log.error(f"SSL error: {e}")
                time.sleep(10)
            except TimeoutError:
                log.debug("recv timeout, continuing")
                continue
            except OSError as e:
                if "timed out" in str(e).lower():
                    log.debug("recv timed out, continuing")
                    continue
                log.error(f"Socket error: {e}")
                time.sleep(10)
            except Exception as e:
                log.error(f"Error: {e}")
                time.sleep(10)

    def _process(self, line: str):
        if line.startswith("PING"):
            self.send("PONG " + line.split(":", 1)[1])
            return

        kick_m = re.match(r":\S+ KICK (\S+) " + re.escape(NICKNAME), line)
        if kick_m:
            self.active_channels.discard(kick_m.group(1).lower())
            log.info(f"Kicked from {kick_m.group(1)}")
            return

        m = re.match(r":([^!]+)![^@]+@(\S+) PRIVMSG (\S+) :(.*)", line)
        if not m: return

        nick, hostmask, target, text = m.groups()
        text     = text.strip()
        is_pm    = target.lower() == NICKNAME.lower()
        reply_to = nick if is_pm else target

        cmd, arg = None, None

        if text.startswith(CMD_PREFIX):
            rest  = text[len(CMD_PREFIX):]
            parts = rest.split(None, 1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else None
        elif is_pm:
            parts     = text.split(None, 1)
            candidate = parts[0].lower()
            if candidate in self.CMD_ALIASES:
                cmd = candidate
                arg = parts[1].strip() if len(parts) > 1 else None

        if cmd and cmd in self.CMD_ALIASES:
            log.info(f"cmd={cmd!r} arg={arg!r} from {nick}!{hostmask} {'(PM)' if is_pm else 'in ' + reply_to}")
            self.dispatch(nick, reply_to, cmd, arg)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = IRCBot()
    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            sys.exit(0)
        except Exception as e:
            log.error(f"Crash: {e} — restarting in 30s")
            time.sleep(30)
