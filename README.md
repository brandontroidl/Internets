# Internets 🌤️

A modular IRC bot with worldwide weather, calculator, dice roller, Urban Dictionary, translation, and a hot-reload system so you never have to take it offline to update.

> **Disclaimer:** This entire bot was vibe coded with Claude because I was too lazy to do any actual work. It works though, so whatever.

---

## Features

- **Worldwide weather** — US locations use weather.gov (NWS), everywhere else uses Open-Meteo. No API keys required for either.
- Classic IRC output style (`:: City, ST :: Conditions Clear :: Temperature 29.1C / 84.3F :: ...`)
- City name, zip code, or raw `lat,lon` support — works globally
- `-n nick` flag to look up another user's registered location
- Per-nick location registration saved to `locations.json`
- Per-channel user registry — tracks joins, parts, quits, nick changes, last seen
- Calculator, dice roller, Urban Dictionary, and Google Translate built in
- **Invite-only** — no channels in config, bot joins when `/INVITE`d. Joined channels persist across restarts in `channels.json`
- **Dynamic module loading** — load, unload, and reload modules without restarting
- **Hot reload** — `.reloadall` reloads all modules in-place; `.restart` does a full process restart from IRC, no terminal needed
- **Hashed admin passwords** — scrypt, bcrypt, or argon2. Plaintext passwords are rejected at startup
- SSL with optional cert verification bypass (for servers with self-signed certs)
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

Optional stronger password hashing (scrypt is built-in and works out of the box):
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
api_cooldown = 10
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

---

## Commands

All commands use `.` as the prefix by default (configurable). In a private message the prefix is optional — `AUTH yourpassword` works the same as `.auth yourpassword`.

### Weather

US locations use **weather.gov**. All other locations use **Open-Meteo**.

| Command | Description |
|---|---|
| `.weather [zip\|city\|-n nick]` | Current conditions |
| `.w [zip\|city\|-n nick]` | Alias for `.weather` |
| `.forecast [zip\|city\|-n nick]` | 4-day forecast |
| `.f [zip\|city\|-n nick]` | Alias for `.forecast` |

```
<brandon> .w 90210
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 29.1C / 84.3F :: Dew point 17.0C / 62.6F :: Pressure 1013mb / 29.92in :: Humidity 47% :: Visibility 16.1km / 10.0mi :: Wind Calm :: Last Updated on August 26, 11:24 AM UTC ::

<brandon> .w stockholm, sweden
<Internets> :: Stockholm, Sweden :: Conditions Partly Cloudy :: Temperature -2.1C / 28.2F :: Feels like -7.3C / 18.9F :: Dew point -5.0C / 23.0F :: Pressure 1021mb / 30.15in :: Humidity 78% :: Wind from NW at 18.0km/h / 11.2 mph :: Last Updated on March 01, 02:00 PM UTC ::

<brandon> .f -n KnownSyntax
<Internets> :: Gävle, Sweden :: Monday Partly Cloudy -1.0C / 30.2F -8.0C / 17.6F :: Tuesday Clear 2.0C / 35.6F -5.0C / 23.0F :: ...
```

### Location Registration

| Command | Description |
|---|---|
| `.regloc <zip\|city>` | Save your default location (worldwide) |
| `.register_location <zip\|city>` | Alias for `.regloc` |
| `.myloc` | Show your saved location |
| `.delloc` | Remove your saved location |

Once registered, `.weather` and `.forecast` work with no argument. Other users can look up your location with `-n yournick`.

```
<brandon> .regloc panama city, fl
<Internets> brandon: registered location Panama City, FL

<KnownSyntax> .regloc gävle, sweden
<Internets> KnownSyntax: registered location Gävle, Sweden
```

Locations are stored in `locations.json` and persist across restarts.

### Channel Management

The bot is **invite-only** — there are no channels in `config.ini`. Invite it to a channel and it joins and remembers. Joined channels persist in `channels.json` and are rejoined on restart.

| Command | Description |
|---|---|
| `.join <#channel>` | Ask bot to join a channel (also works with `/INVITE`) |
| `.part <#channel>` | Ask bot to leave a channel |
| `.users [#channel]` | Show known users in a channel **[admin]** |

```
/INVITE Internets #yourchannel

<brandon> .users #chatnplay
<Internets> Known users in #chatnplay (3):
<Internets>   brandon!brandon@host  first: 2026-03-01 11:39  last: 2026-03-01 12:15
<Internets>   KnownSyntax!ks@host   first: 2026-02-28 09:00  last: 2026-03-01 11:58
```

The user registry tracks JOIN, PART, QUIT, KICK, and NICK change events. Last seen is updated on every message.

### Calculator

```
<brandon> .cc 2pi
<Internets> [calc] 2pi = 6.2831853

<brandon> .cc sqrt(144) + 3^2
<Internets> [calc] sqrt(144) + 3^2 = 21

<brandon> .cc sin(pi/2)
<Internets> [calc] sin(pi/2) = 1
```

Uses Python's `math` module in a sandboxed eval — no builtins, no code execution. Implicit multiplication works (`2pi` → `2*pi`, `3e` → `3*e`).

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

Authenticate first in a **private message**:
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
| `.restart` | Full process restart (picks up changes to `internets.py`) |
| `.modules` | List loaded and available modules |
| `.users [#channel]` | Show known users for a channel |

### Typical update workflow

```bash
# Edit a module file on the server, then from IRC:
/MSG Internets AUTH yourpassword
/MSG Internets RELOADALL        # picks up module changes instantly

# If you edited internets.py itself:
/MSG Internets RESTART          # brief disconnect, comes back automatically
```

---

## Module System

Modules live in the `modules/` directory. Each file exposes a `setup(bot)` function returning a `BotModule` instance. The `autoload` key in `config.ini` controls which modules load on startup.

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

---

## Admin Password Setup

Plaintext passwords are **rejected at startup**. You must use a hashed password.

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

| Algorithm | Memory cost | Extra package |
|---|---|---|
| `scrypt` | auto-detected (16–128MB) | none |
| `bcrypt` | time-based, cost=12 | `pip install bcrypt` |
| `argon2` | 64MB + time-based | `pip install argon2-cffi` |

scrypt auto-probes for the strongest parameters your OpenSSL build allows. On Arch/Fedora (OpenSSL 3.x) it will use `N=16384, r=8, p=2` due to a 32MB memory cap; on most other systems it uses higher values.

---

## APIs Used

| API | Key Required | Used For |
|---|---|---|
| [weather.gov (NWS)](https://www.weather.gov/documentation/services-web-api) | No | Weather data — US locations |
| [Open-Meteo](https://open-meteo.com) | No | Weather data — non-US locations |
| [Nominatim (OpenStreetMap)](https://nominatim.org/) | No | Geocoding, worldwide |
| [Urban Dictionary](https://api.urbandictionary.com) | No | Dictionary lookups |
| [Google Translate (gtx)](https://translate.googleapis.com) | No | Translation |

---

## Notes

- weather.gov requires a `User-Agent` with contact info per their [ToS](https://www.weather.gov/documentation/services-web-api) — set `user_agent` in `config.ini`
- US territories without NWS grid coverage (Guam, Puerto Rico, etc.) automatically fall back to Open-Meteo
- The Google Translate endpoint is unofficial and could break if Google changes it
- Rate limiting is per-nick and applies to weather commands only (`api_cooldown` in config)
- Admin sessions are in-memory only and do not persist across restarts — re-authenticate after `.restart`

---

*Vibe coded with [Claude](https://claude.ai) because writing IRC bots from scratch is a lot of work and the vibes were immaculate.*
