# Internets 🌤️

A modular IRC bot with worldwide weather, calculator, dice roller, Urban Dictionary, translation, and a live hot-reload system so you never have to take it offline to update.

---

## Features

- **Worldwide weather** — US locations use weather.gov (NWS), everywhere else uses Open-Meteo. No API keys required for either.
- **Full NWS feature set for US** — current conditions, 4-day forecast, 8-hour hourly, active alerts with severity, and formatted forecaster discussion (AFD)
- **Smart response routing** — regular command output goes to the channel; help and privileged command responses come back as NOTICE to the requesting user only; everything in PM stays as PRIVMSG
- **Two-tier flood protection** — a global per-nick flood gate silently drops commands sent too fast; a separate API cooldown rate-limits expensive weather lookups; authed admins bypass the flood gate but not the API cooldown
- **IRCv3 compliant** — full `CAP LS 302` negotiation; requests `multi-prefix`, `away-notify`, `account-notify`, `chghost`, `extended-join`, `server-time`, `message-tags`; degrades gracefully on servers that support none of it
- **Flood-safe outbound queue** — all sends go through a token bucket (5 burst, 1 per 1.5s refill); PONG, CAP, and registration messages bypass it as immediate priority so keepalive and negotiation are never blocked behind queued output
- **Invite-only** — no channels in config; bot joins via `/INVITE` and remembers channels across restarts in `channels.json`
- **Per-channel user registry** — tracks joins, parts, quits, nick changes, and last seen timestamps for every user
- **Dynamic module system** — load, unload, and reload individual modules without restarting
- **Hot reload** — `.reloadall` reloads all modules in-place; `.restart` does a full process restart, all from IRC
- **Live config rehash** — `.rehash` reloads `config.ini` and activates a new admin password without restarting
- **Hashed admin passwords** — scrypt, bcrypt, or argon2; plaintext passwords are rejected at startup
- **Admin-aware `.help`** — non-authed users see only user commands; authed admins see the full list
- Server connection password (`PASS`) for networks and bouncers that require one
- IRC operator (`OPER`) support, sent automatically after connect
- NickServ identification support
- Nick collision recovery — appends `_` and retries automatically on 433
- SSL with optional cert verification bypass for self-signed certs
- Plain TCP support for non-SSL servers
- Auto-reconnect on disconnect with keepalive ping thread
- City name, zip code, or raw `lat,lon` supported — works globally
- `-n nick` flag on weather commands to look up another user's registered location

---

## Requirements

```
Python 3.10+
requests
```

```bash
pip install requests
```

Optional stronger password hashing (scrypt is built-in and works without any extras):
```bash
pip install bcrypt          # alternative
pip install argon2-cffi     # strongest option
```

---

## Setup

**1. Generate an admin password hash:**
```bash
python hashpw.py --algo scrypt    # default, no extra packages needed
python hashpw.py --algo bcrypt    # pip install bcrypt
python hashpw.py --algo argon2    # pip install argon2-cffi
```
Paste the output into `config.ini` under `[admin] password_hash`.

**2. Fill in `config.ini`** — server, nickname, user_agent (required by weather.gov ToS), and any optional fields.

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

; NickServ identification — leave blank if not registered
nickserv_password =

; Server/bouncer connection password — sent as PASS before NICK/USER
; Leave blank if not needed
server_password =

; IRC operator access — sent as OPER <name> <password> after motd
; Leave both blank if not needed
oper_name =
oper_password =

[bot]
command_prefix = .
api_cooldown = 10          ; seconds between weather/api commands per nick
flood_cooldown = 3         ; seconds between ANY commands per nick (global flood gate)
locations_file = locations.json
channels_file = channels.json
users_file = users.json
modules_dir = modules
autoload = weather,location,calc,dice,urbandictionary,translate,channels

[admin]
; Run python hashpw.py to generate — plaintext passwords are rejected at startup
password_hash =

[weather]
; Required by weather.gov API Terms of Service
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

### Connection Sequence

On every connect the bot follows this sequence:

1. `PASS <server_password>` — only if `server_password` is set
2. `CAP LS 302` — begin IRCv3 capability negotiation, pausing registration
3. `NICK <nickname>`
4. `USER <nickname> 0 * :<realname>`
5. Server replies with `CAP LS` listing its available caps
6. Bot sends `CAP REQ` for any desired caps the server supports
7. Server replies `CAP ACK` or `CAP NAK`
8. Bot sends `CAP END` to resume registration
9. *(after motd)* `PRIVMSG NickServ :IDENTIFY <password>` — only if `nickserv_password` is set
10. *(after motd)* `OPER <oper_name> <oper_password>` — only if both oper fields are set

If the server doesn't support `CAP` at all it replies `421 Unknown command` — the bot detects this and proceeds with registration immediately, no caps. If the nickname is taken (433) the bot appends `_` and retries until a free nick is found.

### IRCv3 Capabilities

The bot requests these on every connect. All are optional — it works normally if the server supports none of them:

| Capability | What it enables |
|---|---|
| `multi-prefix` | See all channel prefixes on a user (`@+nick` instead of just `@nick`) |
| `away-notify` | Receive `AWAY` notifications for users in shared channels |
| `account-notify` | Receive `ACCOUNT` messages when a user's services account changes |
| `chghost` | Receive `CHGHOST` when a user's host changes — no spurious quit/rejoin |
| `extended-join` | `JOIN` messages include the user's account name and realname |
| `server-time` | Messages include a `time=` tag with the server's timestamp |
| `message-tags` | General message tag support (required for `server-time`) |

Incoming lines with message tags (`@key=val :nick!...`) are stripped before parsing so tagged messages never break the command parser.

### Outbound Flood Protection

All outgoing lines go through a **token bucket queue** on a dedicated sender thread:

| | Value |
|---|---|
| Burst capacity | 5 tokens |
| Refill rate | 1 token per 1.5 seconds (~40 msg/min sustained) |
| Immediate (no token cost) | `PONG`, all `CAP` messages, `PASS`/`NICK`/`USER`, `QUIT` |
| Normal (token bucket) | `PRIVMSG`, `NOTICE`, `JOIN`, `OPER`, NickServ identify |

This keeps the bot well under every major network's flood kill threshold while still being able to burst several weather results back-to-back.

### Rate Limiting

| Tier | Config key | Default | Scope | Admin bypass | On trigger |
|---|---|---|---|---|---|
| Flood gate | `flood_cooldown` | 3s | Every command | ✅ Yes | Silently dropped — no response |
| API cooldown | `api_cooldown` | 10s | Weather commands only | ❌ No | User is notified |

The flood gate silently discards commands sent within `flood_cooldown` seconds of the last one. Authed admins bypass it entirely so management commands are never dropped.

The API cooldown is always enforced regardless of admin status — it exists to respect weather.gov's Terms of Service, not to limit local users. Admins are trusted, but the upstream API still has limits.

---

## Response Routing

How the bot responds depends on where the command came from and what type it is:

| Context | Regular commands | Privileged commands |
|---|---|---|
| In a channel | `PRIVMSG` → channel | `NOTICE` → requesting user only |
| Via `/MSG` (PM) | `PRIVMSG` → you | `PRIVMSG` → you |

**Privileged commands** (routed privately): `.help`, `.auth`, `.deauth`, `.modules`, `.load`, `.unload`, `.reload`, `.reloadall`, `.restart`, `.rehash`, `.users`

**Regular commands** (output in channel): `.weather`, `.forecast`, `.hourly`, `.alerts`, `.discuss`, `.regloc`, `.myloc`, `.delloc`, `.cc`, `.d`, `.t`, `.u`, `.join`, `.part`

---

## Commands

All commands use `.` as the prefix by default (configurable in `config.ini`). In a private message the prefix is optional — `WEATHER 90210` works the same as `.weather 90210`.

### Weather

US locations route to **weather.gov (NWS)**. All other locations route to **Open-Meteo**. Both are free and require no API keys.

| Command | Alias | Description |
|---|---|---|
| `.weather [location]` | `.w` | Current conditions — worldwide |
| `.forecast [location]` | `.f` | 4-day forecast — worldwide |
| `.hourly [location]` | `.fh` | Next 8-hour forecast — US only (NWS) |
| `.alerts [location]` | `.wx` | Active NWS weather alerts — US only |
| `.discuss [location]` | `.disc` | NWS forecaster's discussion (AFD) — US only |

`[location]` accepts: zip code, city name, `city, state`, `city, country`, or raw `lat,lon`. Omit to use your registered location. Use `-n nick` to look up another user's registered location.

```
<brandon> .w 90210
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 29.1C / 84.3F :: Dew point 17.0C / 62.6F :: Pressure 1013mb / 29.92in :: Humidity 47% :: Visibility 16.1km / 10.0mi :: Wind Calm :: Last Updated on August 26, 11:24 AM UTC ::

<brandon> .w stockholm, sweden
<Internets> :: Stockholm, Sweden :: Conditions Partly Cloudy :: Temperature -2.1C / 28.2F :: Feels like -7.3C / 18.9F :: Dew point -5.0C / 23.0F :: Pressure 1021mb / 30.15in :: Humidity 78% :: Wind from NW at 18.0km/h / 11.2 mph :: Last Updated on March 01, 02:00 PM UTC ::

<brandon> .fh tampa, fl
<Internets> :: Tampa, FL — Next 8 Hours :: 2PM Partly Cloudy 28.3C / 82.9F :: 3PM Mostly Cloudy 27.1C / 80.8F 30%🌧 :: ...

<brandon> .wx tampa, fl
<Internets> :: Tampa, FL :: 2 active alert(s) ::
<Internets> ⚠ Flood Watch [Moderate/Watch] | 02 PM → 08 PM :: Flood Watch in effect for low-lying areas ...
<Internets> 🌀 Tropical Storm Warning [Severe/Immediate] | expires 08 PM :: Tropical Storm Warning in effect ...

<brandon> .disc
<Internets> :: San Dimas, CA :: NWS LOX Forecast Discussion ::
<Internets> [SYNOPSIS] Gusty northwest to northeast winds will continue through the week. Clear skies and above normal temperatures expected, peaking this weekend ...
<Internets> [SHORT TERM (TDY-WED)] Another surge of offshore flow expected tonight through Tuesday, with gusts to 50 mph possible in the mountains ...
<Internets> [LONG TERM (THU-MON)] High pressure dominant through the extended period. Temperatures warming significantly by the weekend ...
```

US-only features require a US location — non-US queries get a friendly error. NWS grid gaps (some US territories) automatically fall back to Open-Meteo for `.weather` and `.forecast`.

### Location Registration

| Command | Alias | Description |
|---|---|---|
| `.regloc <location>` | `.register_location` | Save your default location — worldwide |
| `.myloc` | | Show your saved location |
| `.delloc` | | Remove your saved location |

Once registered, all weather commands work without a location argument. Other users can look up your location with `-n yournick`.

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

The bot is invite-only — no channels in `config.ini`. Joined channels are saved to `channels.json` and rejoined automatically on restart.

| Command | Description |
|---|---|
| `.join <#channel>` | Ask bot to join a channel (or just `/INVITE` it) |
| `.part <#channel>` | Ask bot to leave a channel |
| `.users [#channel]` | Show known users in a channel — **[admin]**, response via NOTICE |

The user registry is updated on every JOIN, PART, QUIT, KICK, and NICK change event. Last seen is also updated on every message. The current channel is assumed if `#channel` is omitted from `.users`.

```
<brandon> .users #chatnplay
-Internets- Known users in #chatnplay (2):
-Internets-   brandon!brandon@host  first: 2026-03-01 11:39  last: 2026-03-01 14:22
-Internets-   KnownSyntax!ks@host   first: 2026-02-28 09:00  last: 2026-03-01 11:58
```

### Calculator

| Command | Description |
|---|---|
| `.cc <expr>` | Evaluate a math expression |

```
<brandon> .cc 2pi
<Internets> [calc] 2pi = 6.2831853

<brandon> .cc sqrt(144) + 3^2
<Internets> [calc] sqrt(144) + 3^2 = 21

<brandon> .cc sin(pi/2)
<Internets> [calc] sin(pi/2) = 1
```

Sandboxed eval using Python's `math` module — no builtins, no arbitrary code execution. Implicit multiplication works (`2pi` → `2*pi`, `3e` → `3*e`).

### Dice Roller

| Command | Description |
|---|---|
| `.d <expr>` | Roll dice |

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

| Command | Alias | Description |
|---|---|---|
| `.u <term>` | `.urbandictionary` | Look up a term |
| `.u <term> /N` | | Get the Nth definition |

```
<brandon> .u jason
<Internets> [1/7] the only name that can be spelled through 5 months of the year ...

<brandon> .u jason /4
<Internets> [4/7] Leader of the Argonauts ...
```

Uses the official Urban Dictionary API — no key needed.

### Translation

| Command | Alias | Description |
|---|---|---|
| `.t <to> <text>` | `.translate` | Translate text (auto-detect source) |
| `.t <from> <to> <text>` | | Translate with explicit source language |

```
<brandon> .t en es Hello World!
<Internets> [t] [from en] -> ¡Hola Mundo!

<brandon> .t es en ¿Cómo te llamas?
<Internets> [t] [from es] -> What's your name?

<brandon> .t fr What is your name?
<Internets> [t] [from auto] -> Quel est votre nom ?
```

Source language is optional — auto-detected if omitted. Uses the Google Translate `gtx` endpoint, no API key needed. Note this is an unofficial endpoint and could change without notice.

### Admin

`.auth` and `.deauth` only work in a **private message**. All other admin commands work in channels too, but responses always come back as NOTICE to you only.

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
| `.rehash` | Reload `config.ini` live — new password hash active immediately |
| `.modules` | List loaded modules and what's available to load |
| `.users [#channel]` | Show known users in a channel |

Admin sessions are in-memory only — they do not survive `.restart` or `.rehash`. Re-authenticate after either.

### Help

`.help` is a privileged command. In a channel the output comes back as a NOTICE to you only. In PM it's a normal PRIVMSG. What's shown depends on whether you're authenticated:

**Regular users** see user-facing commands and the `.auth` prompt.

**Authed admins** additionally see all `[admin]` commands.

### Typical Update Workflow

```bash
# Edit a module file on the server, then from IRC:
/MSG Internets AUTH yourpassword
/MSG Internets RELOADALL            # picks up module changes instantly, no disconnect

# Edited internets.py itself:
/MSG Internets RESTART              # brief disconnect, bot rejoins automatically

# Changed password in config.ini:
# 1. Run: python hashpw.py  and paste the new hash into config.ini
/MSG Internets REHASH               # new hash active, all admin sessions cleared
/MSG Internets AUTH yournewpassword
```

---

## Module System

Modules live in `modules/`. Each file needs a `setup(bot)` function that returns a `BotModule` instance. The `autoload` key in `config.ini` controls what loads on startup. Everything else can be loaded/unloaded live.

```python
from modules.base import BotModule

class HelloModule(BotModule):
    COMMANDS = {"hello": "cmd_hello", "hi": "cmd_hello"}

    def on_load(self):
        pass  # optional setup

    def on_unload(self):
        pass  # optional cleanup

    def cmd_hello(self, nick, reply_to, arg):
        self.bot.privmsg(reply_to, f"Hello, {nick}!")

    def help_lines(self, prefix):
        return [f"  {prefix}hello   Say hello"]

def setup(bot):
    return HelloModule(bot)
```

Drop it in `modules/hello.py` and load without restarting:
```
/MSG Internets LOAD hello
```

### Bot API Reference

Methods available on `self.bot` inside modules:

| Method | Description |
|---|---|
| `privmsg(target, msg)` | Send a PRIVMSG to a channel or nick |
| `notice(target, msg)` | Send a NOTICE to a channel or nick |
| `reply(nick, reply_to, msg, privileged=False)` | Route-aware reply — PM→PRIVMSG, channel regular→PRIVMSG to channel, channel privileged→NOTICE to nick |
| `preply(nick, reply_to, msg)` | Shortcut for `reply(..., privileged=True)` |
| `send(raw, priority=1)` | Send a raw IRC line; use `priority=0` for immediate sends that bypass the token bucket |
| `rate_limited(nick)` | Returns True and records the call if nick is within `api_cooldown`; use for expensive API calls |
| `flood_limited(nick)` | Returns True if nick is within `flood_cooldown`; authed admins always return False |
| `loc_get(nick)` | Get a nick's saved location string |
| `loc_set(nick, raw)` | Save a location string for a nick |
| `loc_del(nick)` | Delete a nick's saved location |
| `channel_users(channel)` | Returns the user registry dict for a channel |
| `is_admin(nick)` | Returns True if nick is currently authenticated as admin |
| `cfg` | The live `ConfigParser` instance |

All `privmsg`/`notice` output is automatically chunked to 400 bytes per line (byte-safe, not char-safe) to stay well under the 512-byte IRC line limit. Long messages are split transparently.

---

## Admin Password Setup

Plaintext passwords are **rejected at startup**. Always generate a hash first:

```bash
python hashpw.py --algo scrypt    # recommended — no extra packages
python hashpw.py --algo bcrypt    # pip install bcrypt
python hashpw.py --algo argon2    # pip install argon2-cffi (strongest)
```

Paste into `config.ini`:
```ini
[admin]
password_hash = scrypt$16384$8$2$<salt>$<hash>
```

| Algorithm | Extra package | Notes |
|---|---|---|
| `scrypt` | none | Auto-probes for strongest params your OpenSSL build allows. Arch/Fedora (OpenSSL 3.x with 32MB cap) uses N=16384 r=8 p=2; most other systems use higher values. |
| `bcrypt` | `pip install bcrypt` | cost=12 |
| `argon2` | `pip install argon2-cffi` | Strongest; 64MB memory + time hardened |

---

## APIs Used

| API | Key Required | Used For |
|---|---|---|
| [weather.gov (NWS)](https://www.weather.gov/documentation/services-web-api) | No | Current, forecast, hourly, alerts, discussion — US locations |
| [Open-Meteo](https://open-meteo.com) | No | Current conditions and forecast — non-US locations |
| [Nominatim (OpenStreetMap)](https://nominatim.org/) | No | Geocoding — worldwide |
| [Urban Dictionary](https://api.urbandictionary.com) | No | Dictionary lookups |
| [Google Translate (gtx)](https://translate.googleapis.com) | No | Translation (unofficial endpoint) |

---

## Platform Notes

- Tested on **Linux** (Arch, Ubuntu, Debian), **macOS**, **Windows** (Python 3.10+), and **WSL**
- `scrypt` parameters are auto-detected at runtime — no manual tuning needed
- On Windows with SSL certificate errors: `pip install certifi`
- weather.gov requires a `User-Agent` header with contact info per their [ToS](https://www.weather.gov/documentation/services-web-api) — set `user_agent` in `config.ini`
- The inbound flood gate silently drops commands — abusers get no response at all
- Authed admins bypass the inbound flood gate but **not** the API cooldown
- The outbound token bucket is always active and separate from the inbound flood gate
- IRCv3 caps are requested but never required — the bot degrades gracefully on any server

---