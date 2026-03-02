# Internets 🌤️

A modular IRC bot with worldwide weather, calculator, dice roller, Urban Dictionary, translation, and a hot-reload system so you never have to take it offline to update.

> **Disclaimer:** This entire bot was vibe coded with Claude because I was too lazy to do any actual work. It works though, so whatever.

---

## Features

- **Worldwide weather** — US locations use weather.gov (NWS), everywhere else uses Open-Meteo. No API keys required for either.
- **Full NWS coverage for US** — current conditions, 4-day forecast, 8-hour hourly, active alerts, and forecaster discussion
- **Global flood protection** — all commands are rate-limited per-nick (configurable). Excessive commands are silently dropped.
- Classic IRC output style (`:: City, ST :: Conditions Clear :: Temperature 29.1C / 84.3F :: ...`)
- City name, zip code, or raw `lat,lon` — works globally
- `-n nick` flag to look up another user's registered location
- Per-nick location registration saved to `locations.json`
- Per-channel user registry — tracks joins, parts, quits, nick changes, last seen
- Calculator, dice roller, Urban Dictionary, Google Translate
- **Invite-only** — no channels in config; bot joins when `/INVITE`d and remembers channels across restarts
- **Dynamic module loading** — load, unload, and reload modules without restarting
- **Hot reload** — `.reloadall` reloads all modules in-place; `.restart` does a full process restart, all from IRC
- **Hashed admin passwords** — scrypt, bcrypt, or argon2. Plaintext passwords are rejected at startup.
- SSL with optional cert verification bypass (self-signed cert support)
- Plain TCP support for non-SSL servers
- Auto-reconnect on disconnect with keepalive ping thread

---

## Requirements

```
Python 3.10+
requests
```

```bash
pip install requests
```

Optional stronger password hashing (scrypt works out of the box, no packages needed):
```bash
pip install bcrypt          # alternative
pip install argon2-cffi     # strongest option
```

---

## Setup

**1. Generate an admin password hash:**
```bash
python hashpw.py --algo scrypt    # default, no extra packages
python hashpw.py --algo bcrypt    # pip install bcrypt
python hashpw.py --algo argon2    # pip install argon2-cffi
```
Paste the output into `config.ini` under `[admin]`.

**2. Fill in `config.ini`:**
- Set `server`, `nickname`, `nickserv_password` (if needed)
- Set `user_agent` to something with your email — required by weather.gov ToS
- Set `ssl_verify = false` if your server uses a self-signed cert

**3. Run it:**
```bash
python internets.py
```

**4. Invite it to a channel:**
```
/INVITE Internets #yourchannel
```

---

## Configuration (`config.ini`)

```ini
[irc]
server = irc.example.com
port = 6697
ssl = true
ssl_verify = true          ; set false for self-signed certs
nickname = Internets
realname = IRC Bot
nickserv_password =        ; leave blank if not registered

[bot]
command_prefix = .
api_cooldown = 10          ; seconds between weather/api commands per nick
flood_cooldown = 3         ; seconds between ANY commands per nick (flood gate)
locations_file = locations.json
channels_file = channels.json
users_file = users.json
modules_dir = modules
autoload = weather,location,calc,dice,urbandictionary,translate,channels

[admin]
; Run python hashpw.py to generate this value
; Leave blank to disable module management
password_hash =

[weather]
; REQUIRED by weather.gov API Terms of Service
user_agent = Internets/1.0 (your@email.com)

[logging]
level = INFO
log_file = internets.log
```

### SSL Quick Reference

| Server type | `port` | `ssl` | `ssl_verify` |
|---|---|---|---|
| SSL, public CA cert | 6697 | true | true |
| SSL, self-signed cert | 6697 | true | false |
| Plain TCP | 6667 | false | *(ignored)* |

### Rate Limiting

Two independent tiers:

| Tier | Config key | Default | Scope | On trigger |
|---|---|---|---|---|
| Flood gate | `flood_cooldown` | 3s | All commands | Silently dropped |
| API cooldown | `api_cooldown` | 10s | Weather commands | User notified |

The flood gate prevents command spam from abusers — any command sent within `flood_cooldown` seconds of the last one is silently discarded. The API cooldown is a separate per-nick limit specifically for weather lookups.

---

## Commands

All commands use `.` as the prefix (configurable). In a private message the prefix is optional.

### Weather

US locations use **weather.gov (NWS)**. All other locations use **Open-Meteo**.

| Command | Description |
|---|---|
| `.weather [zip\|city\|-n nick]` | Current conditions — worldwide |
| `.w [zip\|city\|-n nick]` | Alias for `.weather` |
| `.forecast [zip\|city\|-n nick]` | 4-day forecast — worldwide |
| `.f [zip\|city\|-n nick]` | Alias for `.forecast` |
| `.hourly [zip\|city\|-n nick]` | Next 8-hour forecast — US only (NWS) |
| `.fh [zip\|city\|-n nick]` | Alias for `.hourly` |
| `.alerts [zip\|city\|-n nick]` | Active NWS weather alerts — US only |
| `.wx [zip\|city\|-n nick]` | Alias for `.alerts` |
| `.discuss [zip\|city\|-n nick]` | NWS forecaster's discussion — US only |
| `.disc [zip\|city\|-n nick]` | Alias for `.discuss` |

```
<brandon> .w 90210
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 29.1C / 84.3F :: Dew point 17.0C / 62.6F :: Pressure 1013mb / 29.92in :: Humidity 47% :: Visibility 16.1km / 10.0mi :: Wind Calm :: Last Updated on August 26, 11:24 AM UTC ::

<brandon> .w stockholm, sweden
<Internets> :: Stockholm, Sweden :: Conditions Partly Cloudy :: Temperature -2.1C / 28.2F :: Feels like -7.3C / 18.9F :: Dew point -5.0C / 23.0F :: Pressure 1021mb / 30.15in :: Humidity 78% :: Wind from NW at 18.0km/h / 11.2 mph :: Last Updated on March 01, 02:00 PM UTC ::

<brandon> .fh tampa, fl
<Internets> :: Tampa, FL — Next 8 Hours :: 2PM Partly Cloudy 28.3C / 82.9F :: 3PM Mostly Cloudy 27.1C / 80.8F 30%🌧 :: ...

<brandon> .wx tampa, fl
<Internets> :: Tampa, FL :: 2 active alert(s) ::
<Internets> ⚠ Flood Watch [Moderate/Watch] | 02 PM → 08 PM :: Flood Watch in effect ...
<Internets> 🌀 Tropical Storm Warning [Severe/Immediate] | expires 08 PM :: Tropical Storm Warning in effect ...

<brandon> .disc
<Internets> :: Tampa, FL :: NWS TBW Forecast Discussion ::
<Internets> A TROPICAL STORM WARNING REMAINS IN EFFECT FOR THE AREA. Moisture continues to increase ...
```

**US-only features** (`.hourly`, `.alerts`, `.discuss`) use the weather.gov API and require a US location. Non-US queries will get a friendly error.

NWS grid coverage gaps (some US territories like Guam, parts of Puerto Rico) automatically fall back to Open-Meteo for `.weather` and `.forecast`.

### Location Registration

| Command | Description |
|---|---|
| `.regloc <zip\|city>` | Save your default location — worldwide |
| `.register_location <zip\|city>` | Alias for `.regloc` |
| `.myloc` | Show your saved location |
| `.delloc` | Remove your saved location |

Once registered, all weather commands work without an argument. Other users can look up your location with `-n yournick`.

```
<brandon> .regloc panama city, fl
<Internets> brandon: registered location Panama City, FL

<KnownSyntax> .regloc gävle, sweden
<Internets> KnownSyntax: registered location Gävle, Sweden

<brandon> .f -n KnownSyntax
<Internets> :: Gävle, Sweden :: Monday Partly Cloudy -1.0C / 30.2F -8.0C / 17.6F :: ...
```

Locations are stored in `locations.json` and persist across restarts.

### Channel Management

The bot is **invite-only** — no channels in `config.ini`. Invite it with `/INVITE` or `.join`. Joined channels persist in `channels.json` and are rejoined automatically on restart.

| Command | Description |
|---|---|
| `.join <#channel>` | Ask bot to join a channel |
| `.part <#channel>` | Ask bot to leave a channel |
| `.users [#channel]` | Show known users in a channel **[admin]** |

```
/INVITE Internets #yourchannel

<brandon> .users #chatnplay
<Internets> Known users in #chatnplay (2):
<Internets>   brandon!brandon@host  first: 2026-03-01 11:39  last: 2026-03-01 14:22
<Internets>   KnownSyntax!ks@host   first: 2026-02-28 09:00  last: 2026-03-01 11:58
```

The user registry tracks JOIN, PART, QUIT, KICK, and NICK change events. Last seen is also updated on every message.

### Calculator

```
<brandon> .cc 2pi
<Internets> [calc] 2pi = 6.2831853

<brandon> .cc sqrt(144) + 3^2
<Internets> [calc] sqrt(144) + 3^2 = 21

<brandon> .cc sin(pi/2)
<Internets> [calc] sin(pi/2) = 1
```

Uses Python's `math` module in a sandboxed eval — no builtins, no arbitrary code execution. Implicit multiplication works (`2pi` → `2*pi`).

### Dice Roller

```
<brandon> .d 6
<Internets> :: Total 4 / 6 [60%] :: Results [4] ::

<brandon> .d 3d6
<Internets> :: Total 11 / 18 [53%] :: Results [2, 4, 5] ::

<brandon> .d 3d6+6
<Internets> :: Total 17 / 24 [65%] :: Results [4, 5, 2] ::
```

Format: `[count]d<sides>[+/-modifier]`. Limits: 1–100 dice, 2–10000 sides.

### Urban Dictionary

```
<brandon> .u jason
<Internets> [1/7] the only name that can be spelled through 5 months of the year ...

<brandon> .u jason /4
<Internets> [4/7] Leader of the Argonauts ...
```

Append `/N` to get a specific definition. Uses the official Urban Dictionary API — no key needed.

### Translation

```
<brandon> .t en es Hello World!
<Internets> [t] [from en] -> ¡Hola Mundo!

<brandon> .t es en ¿Cómo te llamas?
<Internets> [t] [from es] -> What's your name?

<brandon> .t fr What is your name?
<Internets> [t] [from auto] -> Quel est votre nom ?
```

Uses the Google Translate `gtx` endpoint — no API key needed. Source language is optional (auto-detected if omitted).

### Admin

Authenticate first in a **private message** — auth commands do not work in channels:
```
/MSG Internets AUTH yourpassword
/MSG Internets DEAUTH
```

| Command | Description |
|---|---|
| `.auth <password>` | Authenticate as admin **(PM only)** |
| `.deauth` | End admin session **(PM only)** |
| `.load <module>` | Load a module from `modules/` |
| `.unload <module>` | Unload a loaded module |
| `.reload <module>` | Reload a single module in-place |
| `.reloadall` | Reload every loaded module in-place |
| `.restart` | Full process restart — picks up changes to `internets.py` |
| `.modules` | List loaded modules and what's available to load |
| `.users [#channel]` | Show known users for a channel |

Admin sessions are in-memory only and do not survive a `.restart` — re-authenticate after restarting.

### Typical update workflow

```bash
# Edit a module file, then from IRC:
/MSG Internets AUTH yourpassword
/MSG Internets RELOADALL        # picks up changes to any module instantly

# Edited internets.py itself:
/MSG Internets RESTART          # brief disconnect, rejoins automatically
```

---

## Module System

Modules live in `modules/`. Each file must have a `setup(bot)` function returning a `BotModule` instance. The `autoload` key in `config.ini` controls what loads on startup.

To write a new module, subclass `BotModule` from `modules/base.py`:

```python
from modules.base import BotModule

class HelloModule(BotModule):
    COMMANDS = {"hello": "cmd_hello", "hi": "cmd_hello"}

    def cmd_hello(self, nick, reply_to, arg):
        self.bot.privmsg(reply_to, f"Hello, {nick}!")

    def help_lines(self, prefix):
        return [f"  {prefix}hello   Say hello"]

def setup(bot):
    return HelloModule(bot)
```

Drop it in `modules/hello.py` and load it without restarting:
```
/MSG Internets LOAD hello
```

Methods available on `self.bot`: `privmsg(target, msg)`, `send(raw)`, `rate_limited(nick)`, `loc_get(nick)`, `loc_set(nick, raw)`, `loc_del(nick)`, `channel_users(channel)`, `is_admin(nick)`, `cfg` (ConfigParser).

---

## Admin Password Setup

Plaintext passwords are **rejected at startup**. You must use a hashed password generated by `hashpw.py`.

```bash
python hashpw.py --algo scrypt    # recommended default — no extra packages
python hashpw.py --algo bcrypt    # pip install bcrypt
python hashpw.py --algo argon2    # pip install argon2-cffi (strongest)
```

Paste the output into `config.ini`:
```ini
[admin]
password_hash = scrypt$16384$8$2$<salt>$<hash>
```

| Algorithm | Extra package | Notes |
|---|---|---|
| `scrypt` | none | Auto-detects strongest params OpenSSL allows. On Arch/Fedora (OpenSSL 3.x) uses N=16384 r=8 p=2 due to 32MB cap. |
| `bcrypt` | `pip install bcrypt` | cost=12 |
| `argon2` | `pip install argon2-cffi` | Strongest; 64MB memory + time hardened |

---

## APIs Used

| API | Key Required | Used For |
|---|---|---|
| [weather.gov (NWS)](https://www.weather.gov/documentation/services-web-api) | No | Current, forecast, hourly, alerts, discussion — US |
| [Open-Meteo](https://open-meteo.com) | No | Current conditions and forecast — non-US |
| [Nominatim (OpenStreetMap)](https://nominatim.org/) | No | Geocoding — worldwide |
| [Urban Dictionary](https://api.urbandictionary.com) | No | Dictionary lookups |
| [Google Translate (gtx)](https://translate.googleapis.com) | No | Translation |

---

## Notes

- weather.gov requires a `User-Agent` with contact info per their [ToS](https://www.weather.gov/documentation/services-web-api) — set `user_agent` in `config.ini`
- The Google Translate endpoint is unofficial and could break if Google changes it
- The flood gate silently drops commands — abusers won't get a response at all
- Rate limiting is per-nick; admin commands are not rate-limited

---

*Vibe coded with [Claude](https://claude.ai) because writing IRC bots from scratch is a lot of work and the vibes were immaculate.*
