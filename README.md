# Internets

A modular IRC bot built on raw sockets and RFC 2812. Handles worldwide weather, calculator, dice, translation, and Urban Dictionary lookups. Designed around a plugin architecture with hot-reload so you never take it offline to ship changes.

US weather pulls from weather.gov (NWS API). International weather uses Open-Meteo. Neither requires an API key.

## Architecture

```
internets.py          Core: connection lifecycle, IRC protocol parsing, command dispatch
sender.py             Outbound message queue with token-bucket rate limiting
store.py              Persistent JSON-backed storage for locations, channels, and user tracking
hashpw.py             Password hashing and verification (scrypt/bcrypt/argon2)

modules/
  base.py             BotModule base class — the interface every plugin implements
  geocode.py          Location resolution via Nominatim (supports city names, zip codes, lat/lon)
  nws.py              NWS API client: observations, forecast, hourly, alerts, AFD discussion
  units.py            Temperature, wind, pressure, and distance formatting with dual-unit display
  weather.py          Weather command handler — routes US queries to NWS, international to Open-Meteo
  location.py         User location registration and lookup
  calc.py             Expression evaluator
  dice.py             Dice roller with XdN+M notation
  translate.py        Translation via Google Translate
  urbandictionary.py  Urban Dictionary lookups with result pagination
  channels.py         Join/part management and per-channel user roster queries
```

The core (`internets.py`) owns the socket, IRC state machine, and command dispatch. Everything else is a module. Modules register commands via a `COMMANDS` dict mapping command names to method names, receive `(nick, reply_to, arg)` on invocation, and talk back through `bot.privmsg()` / `bot.notice()` / `bot.reply()`. Every command invocation runs on its own daemon thread.

The outbound path goes through `Sender`, which implements a token-bucket (5 burst, ~40 msg/min sustained) to stay under IRC flood limits. Protocol messages (PONG, CAP, NICK) bypass the bucket at priority 0.

## Design Decisions

**Founder-gated channel control.** `.join` and `.part` require the requesting user to be either a bot admin or the registered channel founder. Founder verification is done asynchronously: the bot WHOIS-es the user for their NickServ account and queries IRC services (`INFO #channel`) for the channel founder, then compares. This works across Anope, Atheme, Epona, X2, X3, and forks — anything that responds with a `Founder:` or `Owner:` line. The services bot nick is configurable via `services_nick` in `config.ini` (default: `ChanServ`). Users who aren't the founder can still bring the bot in via IRC's native `/INVITE`, which is always accepted. Joined channels are persisted to `channels.json` and restored on reconnect.

**Two-tier rate limiting.** A global per-nick flood gate drops commands that arrive faster than `flood_cooldown` seconds. A separate API cooldown rate-limits expensive operations (geocoding + weather API calls). Authed admins bypass the flood gate but not the API cooldown. This is a deliberate split: we don't want a fast-typing admin to trigger weather.gov rate limits, but we also don't want them locked out of `.reload` during an incident.

**NWS-first for US, Open-Meteo for everything else.** NWS provides richer data for US locations (heat index, wind chill, visibility, alerts, AFD discussions). Open-Meteo covers the rest of the world. The weather module attempts NWS first for US coordinates and falls back to Open-Meteo if the grid lookup fails (which happens near borders and territories).

**Response routing.** Regular output goes to the channel. Help text and admin command responses go as `NOTICE` to the requesting user (keeps help spam out of channels). Everything in PM stays as `PRIVMSG`. This is the `reply()` / `preply()` split.

**IRCv3 capability negotiation.** The bot requests `multi-prefix`, `away-notify`, `account-notify`, `chghost`, `extended-join`, `server-time`, and `message-tags`. It degrades gracefully if the server supports none of them. CAP negotiation happens before registration completes, following the IRCv3 spec.

## Requirements

Python 3.10 or later. One runtime dependency:

```
pip install requests
```

For password hashing, `scrypt` is built into Python's `hashlib` — no extra packages needed. If you want stronger options:

```
pip install bcrypt          # alternative
pip install argon2-cffi     # strongest option
```

## Setup

**Generate an admin password hash:**

```
python hashpw.py                    # defaults to scrypt
python hashpw.py --algo bcrypt
python hashpw.py --algo argon2
```

Paste the output into `config.ini` under `[admin] password_hash`. Plaintext passwords are rejected at startup.

**Configure `config.ini`:**

Set `server`, `port`, `nickname`, and `user_agent` (required by weather.gov's Terms of Service — use a real contact email). Everything else has sane defaults.

**Run:**

```
python internets.py
```

**Add to a channel:**

Anyone can invite the bot via IRC's native INVITE (the server enforces permissions):

```
/INVITE Internets #yourchannel
```

Or the registered channel founder can use `.join` from any channel or PM:

```
.join #yourchannel
```

The bot verifies ownership by checking the user's NickServ account against the channel founder registered with IRC services (ChanServ, X3, etc.). Bot admins bypass this check. The bot remembers channels across restarts.

## Configuration

The bot reads `config.ini` at startup. Relevant sections:

**`[irc]`** — Server connection. Supports SSL (default on), optional certificate verification bypass for self-signed certs, NickServ identification, server password (for bouncers), and IRC operator credentials. Also configurable: `user_modes` (applied after connect, e.g. `+ix`), `oper_modes` (applied after OPER succeeds, e.g. `+s`), and `oper_snomask` (server notice mask, e.g. `+cCkKoO`).

**`[bot]`** — Command prefix (default `.`), rate limiting (`api_cooldown`, `flood_cooldown`), file paths for persistent storage, modules directory, and autoload list.

**`[admin]`** — Hashed password for admin authentication. Supports `scrypt$`, `bcrypt$`, and `argon2$` prefixes.

**`[weather]`** — User-Agent string (required by weather.gov ToS) and default unit system.

**`[logging]`** — Log level and output file.

Config can be reloaded at runtime with `.rehash`, which also invalidates all active admin sessions.

## Commands

### User Commands

| Command | Description |
|---------|-------------|
| `.help` | Show available commands (admin commands visible only when authed) |
| `.modules` | List loaded and available modules |
| `.weather` / `.w [location]` | Current conditions — worldwide |
| `.forecast` / `.f [location]` | 4-day forecast — worldwide |
| `.hourly` / `.fh [location]` | Next 8 hours — US only (NWS) |
| `.alerts` / `.wx [location]` | Active NWS alerts — US only |
| `.discuss` / `.disc [location]` | NWS area forecast discussion — US only |
| `.regloc <location>` | Save your default location |
| `.myloc` | Show your saved location |
| `.delloc` | Delete your saved location |
| `.cc <expression>` | Calculator (supports math functions, implicit multiplication) |
| `.d [X]dN[+/-M]` | Dice roller |
| `.t [src] <tgt> <text>` | Translate text |
| `.u <word> [/N]` | Urban Dictionary lookup |
| `.join <#channel>` | Invite the bot — requires channel founder or admin |
| `.part <#channel>` | Remove the bot — requires channel founder or admin |
| `.users [#channel]` | Show known users in a channel |

All weather commands accept city names, zip codes, raw `lat,lon` pairs, or `-n nick` to look up another user's registered location.

In PM, the `.` prefix is optional — `weather 10001` works the same as `.weather 10001`.

### Admin Commands

Authenticate first: `/MSG Internets AUTH <password>`

| Command | Description |
|---------|-------------|
| `.auth <password>` | Authenticate (PM only) |
| `.deauth` | End admin session (PM only) |
| `.load <module>` | Load a module |
| `.unload <module>` | Unload a module |
| `.reload <module>` | Reload a module |
| `.reloadall` | Reload all loaded modules |
| `.restart` | Full process restart via `execv` |
| `.rehash` | Reload `config.ini` and clear admin sessions |
| `.mode <+/-modes>` | Set bot user modes (e.g. `.mode +ix`) |
| `.snomask <+/-flags>` | Set server notice mask (e.g. `.snomask +cCkK`) |
| `.shutdown [reason]` / `.die [reason]` | Save state, unload modules, quit cleanly |

## Writing a Module

Create a Python file in `modules/`. Implement `setup(bot)` returning a `BotModule` subclass. Define commands in the `COMMANDS` dict.

```python
from .base import BotModule

class PingModule(BotModule):
    COMMANDS = {"ping": "cmd_ping"}

    def cmd_ping(self, nick, reply_to, arg):
        self.bot.privmsg(reply_to, f"{nick}: pong")

    def help_lines(self, prefix):
        return [f"  {prefix}ping   Pong"]

def setup(bot):
    return PingModule(bot)
```

The bot passes `nick` (who sent the command), `reply_to` (the channel or nick to respond to), and `arg` (everything after the command, or `None`). Use `self.bot.privmsg()` for public responses, `self.bot.notice()` for private ones, or `self.bot.reply()` / `self.bot.preply()` for automatic routing.

Available from `self.bot`: `cfg` (ConfigParser), `loc_get(nick)`, `loc_set(nick, raw)`, `loc_del(nick)`, `rate_limited(nick)`, `flood_limited(nick)`, `is_admin(nick)`, `channel_users(channel)`, `active_channels`, `send(raw_irc, priority)`.

Lifecycle hooks: `on_load()` runs after the module is registered. `on_unload()` runs before it's removed. `on_raw(line)` is called for every incoming IRC line (after IRCv3 tag stripping) and lets modules react to server numerics, NOTICEs, or any other traffic the core doesn't dispatch as a command. Use these for setup, cleanup, and advanced protocol integration.

## Operational Notes

**Nick collision recovery:** If the configured nick is taken, the bot appends `_` and retries.

**Auto-reconnect:** On disconnect, the bot waits 15 seconds and reconnects. Channel list is restored from `channels.json`. If NickServ password is configured, the bot waits for identification confirmation (up to 10 seconds) before sending JOINs so that `+R` channels and ChanServ access lists work. If a saved channel is invite-only (`+i`), the bot asks ChanServ to re-invite it. Channels that reject with 471 (full), 474 (banned), or 475 (bad key) are logged and removed from the saved list.

**Keepalive:** A background thread sends `PING` every 90 seconds. If the socket is dead, the reconnect logic takes over.

**User tracking:** The bot maintains a per-channel registry of nicks, hostmasks, and first/last seen timestamps. Data is persisted to `users.json`. This is populated from observed JOINs, PARTs, QUITs, NICKs, and channel activity — it is not a complete roster (NAMES replies are not used for the general roster).

**Channel ownership verification:** When a non-admin user runs `.join` or `.part`, the bot verifies they are the channel founder by WHOIS-ing them for their NickServ account (330 numeric) and querying the configured services bot (`services_nick`, default ChanServ) with `INFO #channel` for the founder name. If the account matches the founder (case-insensitive), the action proceeds. Verification times out after 15 seconds. This covers Anope, Atheme, Epona, X2, X3, and compatible forks. The services bot name is the only thing that varies — set `services_nick = X3` (or `Q`, etc.) in `config.ini` for non-ChanServ networks.

**Module conflicts:** If two modules try to register the same command, the second load is rejected with a conflict error.

## Security

**Admin auth brute-force protection:** After 5 failed password attempts, the nick is locked out for 5 minutes. Counter resets after the lockout expires or on successful auth.

**Admin sessions cleared on reconnect:** When the bot loses connection, all `_authed` sessions are wiped. Nicks may belong to different people on a new connection, so sessions cannot safely persist.

**Credential redaction in logs:** Outgoing `PASS`, `IDENTIFY`, and `OPER` commands are redacted in the sender's debug log. Incoming `AUTH` messages are redacted in the main loop debug log. The command dispatch log also redacts auth arguments.

**IRC injection prevention:** All outgoing messages have `\r` and `\n` stripped before writing to the socket. This prevents CRLF injection of arbitrary IRC protocol commands.

**Module path traversal prevention:** Module names are validated against `^[a-z][a-z0-9_]*$` before loading. Path components like `..`, `/`, and `.` in module names are rejected.

**Calculator sandboxing:** The calculator uses a recursive AST walker with a strict whitelist of operators and functions. No `eval()`, no `exec()`, no attribute access, no list comprehensions. Exponent inputs are capped at 10,000. Factorial inputs are capped at 170. Expression nesting depth is limited to 50.

**Atomic file writes:** All JSON persistence (locations, channels, users) uses write-to-temp-then-`os.replace()`. A crash during write cannot corrupt the data file.

## Known Limitations

The translation module uses an undocumented Google Translate endpoint (`translate.googleapis.com`). It has no SLA and may break or be rate-limited without notice.

The persistent store (`store.py`) reads and writes the full JSON file on every operation. This is adequate for low-traffic use but will become a bottleneck on busy networks. A future improvement would be in-memory caching with periodic disk flushes, or a migration to SQLite.

The bot does not parse `353` (NAMES reply) for user roster purposes. Users who were already in the channel when the bot joined will not appear in `.users` output until they trigger an observable event (JOIN, PART, QUIT, NICK, or sending a message).

## License

None specified. Add one.
