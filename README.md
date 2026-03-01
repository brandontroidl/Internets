# Internets 🌤️

An IRC bot does things

> **Disclaimer:** This entire bot was vibe coded because I'm too busy with other projects I'm actually developing myself and needed to spinup something fast. It works though, so whatever.

---

## Features

- Current conditions, hourly forecast, 7-day forecast, and active weather alerts
- Classic IRC bot output style (`:: City, ST :: Conditions Clear :: Temp 72F :: ...`)
- City name **and** zip code support (e.g. `.w panama city, fl` or `.w 90210`)
- `-n nick` flag to look up another user's registered location
- Per-nick location registration saved to `locations.json`
- Calculator, dice roller, Urban Dictionary, and Google Translate built in
- SSL with optional cert verification bypass (for servers with self-signed certs)
- Plain TCP support for non-SSL ports
- Auto-reconnect on disconnect
- Keepalive ping thread to prevent idle timeouts
- Commands work in channels and via `/MSG BotNick` private message
- In PM, you can drop the command prefix entirely

---

## Requirements

```
Python 3.10+
requests
```

```bash
pip install requests
```

---

## Setup

1. Copy `config.ini` and fill in your server details
2. Set your email in `user_agent` — required by the weather.gov API
3. Run it:

```bash
python internets.py
```

---

## Configuration (`config.ini`)

```ini
[irc]
server = irc.chatnplay.org
port   = 6697
ssl    = true

# Set to false if your server has a self-signed cert
ssl_verify = false

nickname = Internets
channels = #chatnplay,#bots

# Optional — leave blank if your nick isn't registered
nickserv_password =

[bot]
command_prefix  = .
api_cooldown    = 10
default_location = 40.7128,-74.0060
locations_file  = locations.json

[weather]
# Your contact info — required by weather.gov API ToS
user_agent = Internets/1.0 (your@email.com)
units = us

[logging]
level    = INFO
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

All commands use `.` as the prefix by default (configurable). In a private message, the prefix is optional.

### Weather

| Command | Description |
|---|---|
| `.weather [zip\|city\|-n nick]` | Current conditions |
| `.w [zip\|city\|-n nick]` | Alias for `.weather` |
| `.forecast [zip\|city\|-n nick]` | 4-day forecast |
| `.f [zip\|city\|-n nick]` | Alias for `.forecast` |

```
<brandon> .weather 90210
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 29.1C / 84.3F :: Dew point 17.0C / 62.6F :: Pressure 1013mb / 29.92in :: Humidity 47% :: Visibility 16.1km / 10.0mi :: Wind Calm :: Last Updated on August 26, 11:24 AM UTC ::

<brandon> .w panama city, fl
<Internets> :: Panama City, FL :: Conditions Partly Cloudy :: Temperature 30.1C / 86.2F :: ...

<brandon> .f -n hell
<Internets> :: London, GB :: Monday Clear 110C / 49F 9C / 48.2F :: ...
```

### Location Registration

| Command | Description |
|---|---|
| `.register_location <zip\|city>` | Save your default location |
| `.regloc <zip\|city>` | Alias for `.register_location` |
| `.myloc` | Show your saved location |
| `.delloc` | Remove your saved location |

Once registered, you can run `.weather` or `.forecast` with no argument and it will use your saved location automatically. Other users can look up your location with `-n yournick`.

```
<brandon> .regloc 90210
<Internets> brandon: registered location Beverly Hills, CA
```

Locations are stored in `locations.json` and persist across restarts.

### Calculator

```
<brandon> .cc 2pi
<Internets> [calc] 2pi = 6.2831853

<brandon> .cc sqrt(144) + 3^2
<Internets> [calc] sqrt(144) + 3^2 = 21
```

Uses Python's `math` module sandboxed with no builtins — no arbitrary code execution. Implicit multiplication works (`2pi` → `2*pi`).

### Dice Roller

```
<brandon> .d 6
<Internets> :: Total 4 / 6 [60%] :: Results [4] ::

<brandon> .d 3d6
<Internets> :: Total 11 / 18 [53%] :: Results [2, 4, 5] ::

<brandon> .d 3d6+6
<Internets> :: Total 17 / 24 [65%] :: Results [4, 5, 2] ::
```

Format: `[count]d<sides>[+/-modifier]`

### Urban Dictionary

```
<brandon> .u jason
<Internets> [1/7] the only name that can be spelled through 5 months of the year ...

<brandon> .u jason /4
<Internets> [4/7] Leader of the Argonauts ...
```

Uses the official Urban Dictionary API — no key needed.

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

### Channel Management

```
/MSG Internets JOIN #newchannel
/MSG Internets PART #somechannel
```

Anyone can ask the bot to join or leave a channel, from any channel or via PM.

### Help

```
<brandon> .help
<brandon> /MSG Internets HELP
```

---

## Private Message Usage

All commands work via `/MSG BotNick` and the prefix is optional in PM:

```
/MSG Internets WEATHER 90210
/MSG Internets FORECAST -n brandon
/MSG Internets SETLOC 85048
/MSG Internets T en ja Good morning
```

---

## APIs Used

| API | Key Required | Used For |
|---|---|---|
| [weather.gov](https://www.weather.gov/documentation/services-web-api) | No | Weather data |
| [Nominatim (OpenStreetMap)](https://nominatim.org/) | No | Geocoding city names & zip codes |
| [Urban Dictionary](https://api.urbandictionary.com) | No | Dictionary lookups |
| [Google Translate (gtx)](https://translate.googleapis.com) | No | Translation |

---

## Notes

- weather.gov only covers US locations
- weather.gov requires a `User-Agent` header with contact info per their [ToS](https://www.weather.gov/documentation/services-web-api) — set this in `config.ini`
- The Google Translate endpoint is unofficial and could break if Google changes it
- Rate limiting is per-nick and applies to weather commands only (configurable via `api_cooldown`)

---