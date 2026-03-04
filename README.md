# Internets

**v1.3.0** — Security hardening release (2026-03-03)

A modular IRC bot built on Python's asyncio and RFC 2812. Handles worldwide weather, calculator, dice, translation, and Urban Dictionary lookups. Designed around a plugin architecture with hot-reload so you never take it offline to ship changes.

US weather pulls from weather.gov (NWS API). International weather uses Open-Meteo. Neither requires an API key.

**Platform support:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2.  
**Python:** 3.10+  
**Dependencies:** `requests` (single runtime dependency).  Optional: `bcrypt`, `argon2-cffi` for stronger password hashing.

## Architecture

```
internets.py          Core: asyncio event loop, IRC state machine, command dispatch
protocol.py           Pure protocol helpers (ISUPPORT parsing, MODE parsing, SASL, NAMES)
sender.py             Async outbound queue with token-bucket rate limiting
store.py              In-memory state with periodic disk flush (locations, channels, user tracking)
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

tests/
  run_tests.py        Standalone test suite (no external dependencies)
```

The core (`internets.py`) owns the asyncio event loop, IRC state machine, and command dispatch. Everything else is a module. Modules register commands via a `COMMANDS` dict mapping command names to async method names, receive `(nick, reply_to, arg)` on invocation, and talk back through `bot.privmsg()` / `bot.notice()` / `bot.reply()`. Every command invocation runs as an `asyncio.Task`.

The outbound path goes through `Sender`, an async drain loop over `asyncio.PriorityQueue` that implements a token-bucket (5 burst, ~40 msg/min sustained) to stay under IRC flood limits. Protocol messages (PONG, CAP, NICK) bypass the bucket at priority 0. `Sender.enqueue()` is thread-safe via `loop.call_soon_threadsafe()`.

## Design Decisions

**Async architecture.** The bot runs on a single asyncio event loop. The connection, line reading, command dispatch, send queue, keepalive, and console all run as async tasks or coroutines. Module command handlers are coroutines too — blocking I/O (HTTP via `requests`, password hashing) runs via `asyncio.to_thread()` inside the handler. This keeps the event loop free for protocol processing while still supporting the `requests` library without requiring `aiohttp` as an additional dependency.

**Founder-gated channel control.** `.join` and `.part` require the requesting user to be either a bot admin or the registered channel founder. Founder verification is done asynchronously: the bot WHOIS-es the user for their NickServ account and queries IRC services (`INFO #channel`) for the channel founder, then compares. This works across Anope, Atheme, Epona, X2, X3, and forks — anything that responds with a `Founder:` or `Owner:` line. The services bot nick is configurable via `services_nick` in `config.ini` (default: `ChanServ`). Users who aren't the founder can still bring the bot in via IRC's native `/INVITE`, which is always accepted. Joined channels are persisted to `channels.json` and restored on reconnect.

**Two-tier rate limiting.** A global per-nick flood gate drops commands that arrive faster than `flood_cooldown` seconds. A separate API cooldown rate-limits expensive operations (geocoding + weather API calls). Authed admins bypass the flood gate but not the API cooldown. This is a deliberate split: we don't want a fast-typing admin to trigger weather.gov rate limits, but we also don't want them locked out of `.reload` during an incident.

**NWS-first for US, Open-Meteo for everything else.** NWS provides richer data for US locations (heat index, wind chill, visibility, alerts, AFD discussions). Open-Meteo covers the rest of the world. The weather module attempts NWS first for US coordinates and falls back to Open-Meteo if the grid lookup fails (which happens near borders and territories).

**Response routing.** Regular output goes to the channel. Help text and admin command responses go as `NOTICE` to the requesting user (keeps help spam out of channels). Everything in PM stays as `PRIVMSG`. This is the `reply()` / `preply()` split.

**IRCv3 capability negotiation.** The bot requests `multi-prefix`, `away-notify`, `account-notify`, `chghost`, `extended-join`, `server-time`, `message-tags`, and `sasl`. If the server supports SASL and a NickServ password is configured, the bot authenticates via SASL PLAIN during capability negotiation — before registration completes. This eliminates the timing race between NickServ IDENTIFY and channel joins. If SASL fails, the bot falls back to traditional NickServ IDENTIFY. All capabilities degrade gracefully if the server supports none of them.

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

## Example Session

```
<alice> .w 10001
<Internets> :: New York, NY :: Conditions Partly Cloudy :: Temperature 18.3C / 64.9F ::
             Dew point 12.1C / 53.8F :: Pressure 1018mb / 30.06in :: Humidity 67% ::
             Visibility 16.1km / 10.0mi :: Wind from SW at 11.2km/h / 6.9mph ::
             Updated March 03, 02:51 PM UTC ::

<alice> .f 10001
<Internets> :: New York, NY :: Monday Partly Cloudy 19.2C / 66.6F / 11.8C / 53.2F ::
             Tuesday Rain 14.5C / 58.1F / 9.3C / 48.7F :: Wednesday Mainly Clear
             16.7C / 62.1F / 8.1C / 46.6F :: Thursday Overcast 13.4C / 56.1F /
             7.2C / 45.0F ::

<bob> .cc sqrt(144) + 2pi
<Internets> [calc] sqrt(144) + 2pi = 18.283185

<carol> .d 3d6+2
<Internets> :: Total 14/20 [71%] :: Rolls [4, 5, 3] ::

<dave> .t es Hello, how are you?
<Internets> [t] [en→es] Hola, ¿cómo estás?

<alice> .regloc 90210
<Internets> alice: location set to Beverly Hills, CA

<alice> .w
<Internets> :: Beverly Hills, CA :: Conditions Clear :: Temperature 22.1C / 71.8F :: ...

<alice> .w -n bob
<Internets> :: Chicago, IL :: Conditions Overcast :: Temperature 8.4C / 47.1F :: ...

<alice> .wx
<Internets> :: Beverly Hills, CA :: No active NWS alerts.

<alice> .u yolo /2
<Internets> [2/7] An acronym for "you only live once", used to justify doing ...
```

Admin session (via PM):

```
-> *Internets* AUTH mypassword
<Internets> Authentication successful.

<alice> .modules
<Internets> Loaded: calc, channels, dice, location, translate, urbandictionary, weather
             Available: (none unloaded)

<alice> .reload weather
<Internets> 'weather' unloaded. 'weather' loaded (6 commands).

<alice> .version
<Internets> Internets 1.3.0 — async modular IRC bot  https://github.com/brandontroidl/Internets
```

CLI startup:

```
$ python internets.py --version
Internets 1.3.0

$ python internets.py
2026-03-03 14:00:01 [INFO] internets: Internets v1.3.0 starting
2026-03-03 14:00:01 [INFO] internets: Loaded calc (1 commands)
2026-03-03 14:00:01 [INFO] internets: Loaded weather (10 commands)
...
2026-03-03 14:00:02 [INFO] internets: Connected to irc.libera.chat:6697 (TLS)
2026-03-03 14:00:03 [INFO] internets: SASL authentication successful
2026-03-03 14:00:03 [INFO] internets: Joined #mychannel
> status
  version  = 1.3.0
  nick     = Internets
  channels = #mychannel
  modules  = calc, channels, dice, location, translate, urbandictionary, weather
  admins   = (none)
>
```

## Configuration

The bot reads `config.ini` at startup. Relevant sections:

**`[irc]`** — Server connection. Supports SSL (default on), optional certificate verification bypass for self-signed certs, NickServ identification, server password (for bouncers), and IRC operator credentials. Also configurable: `user_modes` (applied after connect, e.g. `+ix`), `oper_modes` (applied after OPER succeeds, e.g. `+s`), and `oper_snomask` (server notice mask, e.g. `+cCkKoO`).

**`[bot]`** — Command prefix (default `.`), rate limiting (`api_cooldown`, `flood_cooldown`), file paths for persistent storage, modules directory, and autoload list.

**`[admin]`** — Hashed password for admin authentication. Supports `scrypt$`, `bcrypt$`, and `argon2$` prefixes.

**`[weather]`** — User-Agent string (required by weather.gov ToS) and default unit system.

**`[logging]`** — Log level, output file, rotation, and optional debug file.  The
main log is rotated at `max_bytes` (default 5 MB) keeping `backup_count` old
copies (default 3).  Set `debug_file` to a path to capture ALL output at DEBUG
level regardless of the main `level` setting — useful for protocol diagnostics.
Runtime control via `.loglevel` and `.debug` admin commands (see below).

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
| `.loglevel [LEVEL]` | Show or set log output level (DEBUG/INFO/WARNING/ERROR) |
| `.loglevel <logger> <LEVEL>` | Set level for a specific subsystem (e.g. `.loglevel internets.weather DEBUG`) |
| `.debug [on\|off]` | Toggle global debug output |
| `.debug <subsystem> [off]` | Debug a single subsystem (e.g. `.debug weather`) |
| `.shutdown [reason]` / `.die [reason]` | Save state, unload modules, quit cleanly |

### Console Commands

When running interactively (stdin is a TTY), the bot starts a console task.
Type commands at the `>` prompt — no auth required.  Disable with `--no-console`
or when running under a process manager (auto-detected: console is skipped when
stdin is not a TTY).

| Console Command | IRC Equivalent |
|-----------------|----------------|
| `debug [on\|off]` | `.debug [on\|off]` |
| `debug <sub> [off]` | `.debug <sub> [off]` |
| `loglevel [LEVEL]` | `.loglevel [LEVEL]` |
| `loglevel <logger> LEVEL` | `.loglevel <logger> LEVEL` |
| `status` | *(no equivalent — shows nick, channels, modules, admins, log state)* |
| `shutdown [reason]` | `.shutdown [reason]` |
| `help` | *(shows console commands)* |

## Writing a Module

Create a Python file in `modules/`. Implement `setup(bot)` returning a `BotModule` subclass. Define commands in the `COMMANDS` dict.

```python
from __future__ import annotations
from .base import BotModule

class PingModule(BotModule):
    COMMANDS: dict[str, str] = {"ping": "cmd_ping"}

    async def cmd_ping(self, nick: str, reply_to: str, arg: str | None) -> None:
        self.bot.privmsg(reply_to, f"{nick}: pong")

    def help_lines(self, prefix: str) -> list[str]:
        return [f"  {prefix}ping   Pong"]

def setup(bot: object) -> PingModule:
    return PingModule(bot)
```

The bot passes `nick` (who sent the command), `reply_to` (the channel or nick to respond to), and `arg` (everything after the command, or `None`). All command handlers are coroutines (`async def`). Use `self.bot.privmsg()` for public responses, `self.bot.notice()` for private ones, or `self.bot.reply()` / `self.bot.preply()` for automatic routing.

For blocking I/O (HTTP, disk, CPU-heavy work), use `await asyncio.to_thread(...)`:

```python
import asyncio, requests

async def cmd_fetch(self, nick, reply_to, arg):
    resp = await asyncio.to_thread(requests.get, "https://api.example.com/data", timeout=10)
    self.bot.privmsg(reply_to, f"Got: {resp.json()}")
```

Available from `self.bot`: `cfg` (ConfigParser), `loc_get(nick)`, `loc_set(nick, raw)`, `loc_del(nick)`, `rate_limited(nick)`, `flood_limited(nick)`, `is_admin(nick)`, `channel_users(channel)`, `active_channels`, `send(raw_irc, priority)`.

Lifecycle hooks: `on_load()` runs after the module is registered. `on_unload()` runs before it's removed. `on_raw(line)` is called for every incoming IRC line (after IRCv3 tag stripping) and lets modules react to server numerics, NOTICEs, or any other traffic the core doesn't dispatch as a command. Use these for setup, cleanup, and advanced protocol integration.

## Operational Notes

**Nick collision recovery:** If the configured nick is taken, the bot appends `_` and retries.

**Auto-reconnect with exponential backoff.** On disconnect, the bot reconnects with exponential backoff: 15s, 30s, 60s, 120s, 240s, capped at 5 minutes. The attempt counter resets on successful connection. Channel list is restored from `channels.json`. If SASL is available, identification happens during registration. Otherwise, if a NickServ password is configured, the bot waits for identification confirmation (up to 10 seconds) before sending JOINs so that `+R` channels and ChanServ access lists work. If a saved channel is invite-only (`+i`), the bot asks ChanServ to re-invite it. Channels that reject with 471 (full), 474 (banned), or 475 (bad key) are logged and removed from the saved list.

**Keepalive:** An async task sends `PING` every 90 seconds. If the read times out after 300 seconds with no data, the connection is presumed dead and the reconnect logic takes over.

**User tracking.** The bot maintains a per-channel registry of nicks, hostmasks, and first/last seen timestamps in memory, flushed to `users.json` every 30 seconds. Populated from observed JOINs, PARTs, QUITs, NICKs, and channel activity — it is not a complete roster (NAMES replies are not used for the general roster). Entries older than 90 days (configurable via `user_max_age_days` in `config.ini`) are automatically pruned during flushes.

**Channel ownership verification:** When a non-admin user runs `.join` or `.part`, the bot verifies they are the channel founder by WHOIS-ing them for their NickServ account (330 numeric) and querying the configured services bot (`services_nick`, default ChanServ) with `INFO #channel` for the founder name. If the account matches the founder (case-insensitive), the action proceeds. Verification times out after 15 seconds. This covers Anope, Atheme, Epona, X2, X3, and compatible forks. The services bot name is the only thing that varies — set `services_nick = X3` (or `Q`, etc.) in `config.ini` for non-ChanServ networks.

**Module conflicts:** If two modules try to register the same command, the second load is rejected with a conflict error.

## Security

The bot has been through seven audit passes with 84 findings, all resolved. See AUDIT.md for the complete finding inventory.

**Authentication:** Admin passwords are hashed with scrypt (default), bcrypt, or argon2. Constant-time comparison via `hmac.compare_digest`. Brute-force lockout after 5 failures (5-minute cooldown). Sessions cleared on disconnect. Auth commands restricted to PM.

**Transport:** TLS 1.2 minimum enforced. No fallback to TLS 1.0/1.1. Certificate verification enabled by default (configurable for self-signed certs).

**Input validation:** Module names validated against `^[a-z][a-z0-9_]*$`. Channel names validated against IRC format regex. Command arguments capped at 400 characters. PRIVMSG/NOTICE targets validated (no spaces). All user input treated as untrusted.

**Protocol compliance:** Outgoing lines capped at 512 bytes (RFC 2812). Incoming lines limited to 8KB buffer. CRLF/NUL stripped from all outgoing messages. PING payload reflection capped at 400 bytes.

**Injection prevention:** Log injection prevented via `_SafeFormatter` (sanitizes msg and args). No `eval()`/`exec()` anywhere — calculator uses AST walker with strict whitelist. Module loader blocks symlink traversal. Config path resolved to absolute at startup.

**Resource limits:** Concurrent command tasks capped at 50. Sender queue bounded at 200 messages. INVITE acceptance rate-limited (5s cooldown). Store data files capped at 10MB on load. API and flood rate limiters per-nick.

**Information disclosure:** All error messages sent to IRC are generic ("see log for details"). No stack traces, file paths, or internal state exposed. Outgoing credentials (PASS, IDENTIFY, OPER, AUTHENTICATE) redacted in logs.

**Cross-platform:** Config permission check guarded for POSIX. Store I/O uses explicit UTF-8 encoding. Temp file cleanup is exception-safe on Windows. Restart uses subprocess on Windows (os.execv doesn't replace the process). math.cbrt fallback for Python < 3.11.

## Testing

119 automated tests in `tests/run_tests.py`. No external test framework required:

```
python tests/run_tests.py
```

Covers protocol parsing, store operations (CRUD, flush, atomic writes, pruning, type validation), calculator sandboxing and DoS guards, dice, weather data merging, unit conversions, sender injection prevention and line limits, password hashing, thread-safe containers, async architecture verification, and all security hardening fixes from audit passes six and seven. Compatible with pytest.

## Known Limitations

The translation module uses an undocumented Google Translate endpoint (`translate.googleapis.com`). It has no SLA and may break or be rate-limited without notice.

**Persistence:** The store loads all JSON files into memory once at startup. Mutations happen in-memory; a background thread flushes dirty data to disk every 30 seconds. Each dataset (locations, channels, users) has its own lock, so weather lookups never block on user-tracking writes. Worst-case data loss on a hard crash is 30 seconds of user-tracking timestamps — channel list and location changes are also flushed on `.shutdown`, `.restart`, and signal handlers.

The bot does not parse `353` (NAMES reply) for user roster purposes. Users who were already in the channel when the bot joined will not appear in `.users` output until they trigger an observable event (JOIN, PART, QUIT, NICK, or sending a message).

**Atomic writes on Windows:** `os.replace()` is atomic on POSIX but not guaranteed atomic on NTFS. It is the best Python offers cross-platform. Data loss from a crash during the brief write window is unlikely but theoretically possible on Windows.

## License

MIT — see [LICENSE](LICENSE).
