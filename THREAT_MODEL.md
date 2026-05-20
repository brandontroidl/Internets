# Risk model — Internets IRC Bot

Defensive risk register following Microsoft STRIDE, MITRE ATT&CK for
Enterprise terminology, and NIST SP 800-30 r1 conventions. Each row is
framed *Asset → Risk → Existing Mitigation (file:line) → Residual Risk →
Recommendation*. Maintainer's 15-minute reference for evaluating new
features against the existing in-tree defences.

---

## 1. System overview

- Single-process Python `asyncio` service that connects to one IRC
  network as a normal user. Entry point `internets._entry`
  (`internets.py:1036`).
- Accepts commands from any IRC user in any joined channel; admin
  commands gated by `AdminCommandsMixin._require_admin`
  (`admin_cmds.py:55`).
- Calls out to ~20+ third-party HTTP APIs (every endpoint enumerated in
  §5).
- Persists state to disk via `Store` (`store.py:94`): `locations.json`,
  `channels.json`, `users.json`; credentials via `secret_store`
  (`secret_store.py:181`).
- Operator is a single individual on their own host; no
  multi-tenant trust boundary.

---

## 2. Data flow (text-form diagram)

Dashed `- - -` lines mark trust boundaries.

```
IRC server (TLS)                                ATT&CK: T1071.001
    |
    | raw line   --- trust boundary (untrusted) --- - - -
    v
internets._process (internets.py:589)
    |--> strip_tags / _handle_cap / _handle_numeric / _handle_membership
    |
    v
internets._handle_privmsg (internets.py:777)
    | parse cmd + arg, _MAX_ARG_LEN, log redacts AUTH (internets.py:881)
    v
internets._dispatch (internets.py:462)
    | flood/api rate limit, _MAX_TASKS cap (internets.py:473)
    v
core handler (admin_cmds.py) OR module handler (modules/*.py)
    |
    |-- credential read --> modules.base.cred (modules/base.py:16)
    |                           |
    |                           v
    |                       secret_store.get (secret_store.py:181)
    |                           | env -> keyring -> 0600 secrets.ini
    |                           - - - trust boundary (local FS) - - -
    |
    |-- outbound HTTP --> requests / aiohttp - - - third-party API - - -
    |                       (SSRF guard for user-supplied URLs:
    |                        modules/urls.py:101 _url_is_safe)
    v
IRCBot.preply / privmsg / notice  (internets.py:264..286)
    |
    v
Sender.enqueue (sender.py:100, thread-safe)
    | token bucket, MAX_QUEUE=200 (sender.py:30)
    v
Sender._write_line (sender.py:146)
    | strip CR/LF/NUL, _REDACT_OUT scrub (sender.py:113..141)
    | 512-byte cap (sender.py:144)
    v
StreamWriter --> TLS --> IRC server

Admin auth side-channel:
    /MSG bot AUTH <pw> -> _handle_privmsg -> _dispatch
        -> AdminCommandsMixin.cmd_auth (admin_cmds.py:63)
        -> asyncio.to_thread(verify_password)  (hashpw.py:211)
        -> _authed[nick] = hostmask           (admin_cmds.py:137)
        -> revalidated by is_admin            (internets.py:304)

Persistence:
    Store mutators (loc_set / user_join / channels_save / ...)
        -> dirty flag set under per-dataset lock
        -> _flush_loop thread (store.py:195) every 30 s
        -> _write: tempfile.mkstemp -> json.dump(_wrap_v2)
           -> os.replace                       (store.py:167..191)
        -> v2 envelope w/ SHA-256 checksum     (store.py:50..91)

Console (local-host privilege):
    stdin --- trust boundary (local user) - - -
        -> console.run_console (console.py:31)
        -> bot.request_shutdown / apply_debug / apply_loglevel
```

---

## 3. Sensitive assets table

| Asset | Confidentiality | Integrity | Availability | Where stored |
|-------|-----|-----|-----|-----|
| Admin password hash | high | high | low | `config.local.ini` `[admin] password_hash` (`config.py:40`, `botlog.py:164`) |
| NickServ / SASL / server / oper passwords | high | high | low | `secret_store` (`secret_store.py:51..73`) |
| Weather + module API keys (omdb / lastfm / youtube / finnhub / brave / twitch / steam / weatherkit `.p8`, etc.) | high | low | medium | `secret_store` (`secret_store.py:KNOWN_SECRETS`) |
| User-Agent contact email | medium | low | low | `secret_store` key `weather_user_agent` (`secret_store.py:58`) |
| `users.json` channel-membership PII (nick, hostmask, last_seen) | medium | low | low | local JSON, 0600 inode perms; opt-out at `store.py:351` |
| `locations.json` user-supplied ZIPs | medium | low | low | local JSON; tied to nick (`store.py:255`) |
| Bot process uptime | low | low | medium | runtime — protected by Sender queue cap (`sender.py:30`) |
| Operator host | high | high | high | the machine itself — see §6 |

---

## 4. Trust boundaries

- **Operator ↔ IRC server.** TLS link with TLS 1.2 minimum and default
  hostname/cert validation (`internets.py:521`). `ssl_verify=false`
  downgrade path (`internets.py:523`) is for self-signed networks; see
  residual §19.
- **Operator ↔ unauthenticated IRC users.** Every IRC command crosses
  this boundary; rate-limit + arg-length checks in `_dispatch`
  (`internets.py:462..496`).
- **Operator ↔ authenticated admins (`.auth`).** Argon2/scrypt/bcrypt
  hash verified via `hashpw.verify_password` (`hashpw.py:211`); admin
  session bound to nick+hostmask and revalidated per call
  (`internets.py:304`).
- **Bot process ↔ external HTTP APIs.** Outbound credentials sent;
  inbound JSON treated as untrusted (`modules/ipinfo.py:84`,
  `modules/weather.py:_sanitize` per test SEC-WP-004).
- **Bot process ↔ local filesystem.** Secrets stored 0600 with
  fail-closed perm check (`secret_store.py:162`); state files written
  atomically (`store.py:167`).
- **Bot process ↔ console stdin.** Treated as local-host privilege;
  see `--no-console` flag (`config.py:97`, `internets.py:993`).

---

## 5. Network egress matrix

Every host the bot can reach. Useful as a basis for egress firewalling.

| Host | Port | Scheme | Auth | Credential | Capability |
|------|------|--------|------|-----------|-----------|
| `<config.irc.server>` | configurable | TCP+TLS | SASL PLAIN / NickServ / OPER | `nickserv_password` / `server_password` / `oper_password` (`secret_store.py:53..56`) | IRC connection (`internets.py:528`) |
| `api.open-meteo.com` / `marine-api.open-meteo.com` / `archive-api.open-meteo.com` | 443 | https | none | — | weather (`weather_providers/openmeteo/*.py`) |
| `api.weather.gov` | 443 | https | none (UA required) | `weather_user_agent` | NWS weather (`weather_providers/nws/current.py:15`) |
| `api.weatherapi.com` | 443 | https | query key | `weatherapi_key` | weather (`weather_providers/weatherapi/current.py:5`) |
| `api.tomorrow.io` | 443 | https | query key | `tomorrowio_key` | weather (`weather_providers/tomorrowio/current.py:6`) |
| `api.openweathermap.org` | 443 | https | query key | `openweathermap_key` | weather (`weather_providers/openweathermap/current.py:6`) |
| `weather.visualcrossing.com` | 443 | https | query key | `visualcrossing_key` | weather (`weather_providers/visualcrossing/current.py:10`) |
| `api.pirateweather.net` | 443 | https | path key | `pirateweather_key` | weather (`weather_providers/pirateweather/current.py:8`) |
| `api.weatherstack.com` | 443 | https | query key | `weatherstack_key` | weather; HTTPS was a recent uplift, see residual §19 (`weather_providers/weatherstack/current.py:5`) |
| `dataservice.accuweather.com` | 443 | https | query key | `accuweather_key` | weather (`weather_providers/accuweather/current.py:6`) |
| `api.worldweatheronline.com` | 443 | https | query key | `worldweatheronline_key` | weather (`weather_providers/worldweatheronline/current.py:10`) |
| `api.weatherbit.io` | 443 | https | query key | `weatherbit_key` | weather (`weather_providers/weatherbit/current.py:7`) |
| `api.stormglass.io` | 443 | https | header key | `stormglass_key` | weather (`weather_providers/stormglass/current.py:7`) |
| `api.meteomatics.com` | 443 | https | HTTP Basic | `meteomatics_username` / `meteomatics_password` | weather (`weather_providers/meteomatics/current.py:7`) |
| `weatherkit.apple.com` | 443 | https | JWT ES256 (PyJWT + cryptography) | `weatherkit_key_file` `.p8`, `weatherkit_*_id` | weather (`weather_providers/weatherkit/__init__.py:122`) |
| `nominatim.openstreetmap.org` | 443 | https | UA required | `weather_user_agent` | geocoding (`modules/geocode.py:480`, `:513`) |
| `ip-api.com` | 80 | **http** | none | — | IP geolocation; UA sent (`modules/ipinfo.py:64`) — residual §19 |
| `www.fmylife.com` | 443 | https | none | — | random fortune (`modules/fml.py:30`) |
| `api.dictionaryapi.dev` | 443 | https | none | — | dictionary (`modules/dictionary.py:21`) |
| `is.gd` | 443 | https | none | — | URL shorten (`modules/urls.py:183`) |
| `api.urbandictionary.com` | 443 | https | none | — | UD (`modules/urbandictionary.py:18`) |
| `translate.googleapis.com` | 443 | https | none (unofficial) | — | translate; unofficial endpoint, residual §19 (`modules/translate.py:65`) |
| `html.duckduckgo.com` | 443 | https | none | — | web search (`modules/search.py:47`) |
| `api.search.brave.com` | 443 | https | header key | `brave_key` | web/image search (`modules/search.py:84..112`) |
| `www.omdbapi.com` / `www.imdb.com` | 443 | https | query key | `omdb_key` | IMDb (`modules/imdb.py:15`) |
| `ws.audioscrobbler.com` | 443 | https | query key | `lastfm_key` | last.fm (`modules/lastfm.py:31`) |
| `www.googleapis.com` / `www.youtube.com` | 443 | https | query key | `youtube_key` | YouTube (`modules/youtube.py:34..52`) |
| `finnhub.io` / `www.alphavantage.co` / `api.twelvedata.com` | 443 | https | query key | `finnhub_key` / `alphavantage_key` / `twelvedata_key` | stocks (`modules/stocks.py:48..174`) |
| `api.steampowered.com` | 443 | https | query key | `steam_key` | Steam (`modules/steam.py:43..73`) |
| `id.twitch.tv` / `api.twitch.tv` | 443 | https | OAuth2 client creds | `twitch_client_id`, `twitch_client_secret` | Twitch (`modules/twitch.py:26..51`) |
| `<config.qdb.api_url>` | per-config | per-config | per-config | — | QDB; defunct upstream — must be set by operator (`modules/qdb.py:65`) |
| `<config.idlerpg.api_url>` (default `http://idlerpg.rizon.net`) | 80 | **http** | none | — | IdleRPG XML; HTTP-only (`modules/idlerpg.py:67`) |
| Arbitrary URL via `.expand` / `.shorten` | 80/443 | http(s) | UA only | — | URL expansion behind SSRF guard (`modules/urls.py:119`) |

---

## 6. Untrusted-principal categories

For each, capability / what they can reach / what they cannot:

- **Anonymous IRC user.** Capability: invoke any public command in any
  channel the bot has joined or in PM. Reaches: every public-tier module
  (weather, calc, urls.expand, ipinfo, etc.) and the auth lockout
  counter. Cannot: load modules, change config, read secrets, OPER
  the bot, see admin-only `.help` rows (`admin_cmds.py:165..189`),
  bypass `_MAX_ARG_LEN` / `_MAX_TASKS` / per-nick flood limiter
  (`internets.py:466..477`).
- **Channel founder.** Capability: send the bot an `INVITE`, which is
  honoured subject to the 5 s cooldown and channel-name regex
  (`internets.py:550`). Reaches: join state. Cannot: cause persistent
  state without the bot persisting via `_save_channels`
  (`internets.py:558`).
- **Authenticated admin (`.auth`).** Capability: `.load`/`.unload`/
  `.reload`/`.reloadall`/`.restart`/`.rehash`/`.mode`/`.snomask`/
  `.shutdown`/`.die`/`.loglevel`/`.debug` (`admin_cmds.py:227..360`).
  Reaches: every file under `modules/` for hot-load
  (`internets.py:332..366`). Cannot: read secrets directly via IRC
  (no in-band command for that); change argon2 params live (set at
  hash time, see `hashpw.py:98..110`); persist beyond the bot's own
  data files.
- **IRC server operator.** Capability: see and inject IRC traffic.
  Reaches: anything the bot sends or receives. Mitigation is the
  trust model itself — see §14, "trust the operator's chosen network
  only".
- **Compromised third-party API.** Capability: serve arbitrary JSON /
  redirect chains. Reaches: all module output paths and the
  redirect-following `.expand`. Bounded by SSRF guard
  (`modules/urls.py:67..117`), response-size caps
  (`modules/ipinfo.py:43`, `weather_providers/_http.py:_MAX_RESPONSE_BYTES`
  per test SEC-WP-001), and `_strip_ctrl` of upstream-derived strings
  (`modules/urls.py:159`, `modules/ipinfo.py:33`,
  `modules/weather.py:_sanitize`).
- **Upstream DNS resolver operator.** Capability: return arbitrary IPs
  for our queries. Reaches: SSRF guard re-resolves all A/AAAA at every
  hop (`modules/urls.py:86..98`); TLS hostname verification re-validates
  on connect for HTTPS endpoints. Cannot: redirect TLS endpoints
  silently. Can: drive a DNS TOCTOU between `getaddrinfo` and `connect`
  for the rare HTTP egress (`modules/ipinfo.py`, `modules/idlerpg.py`)
  — residual §19.
- **Local-host user without root privilege.** Capability: same UID, can
  read 0o600 secrets if same user. Cannot: read `secrets.ini` if a
  different user (`secret_store.perms_ok` `secret_store.py:162`)
  rejects non-0o600 modes anyway. Cannot: bypass the process lock
  once it's wired (currently TODO in `process_lock.py:3`).
- **Local-host user with root privilege.** Capability: full system
  control. Out of scope for defence; see §21.
- **Compromised pip dependency.** Capability: code execution at bot
  privilege. Mitigated by pinned versions in `pyproject.toml:32..52`,
  `pip-audit` and `bandit` SARIF in `.github/workflows/security.yml`,
  Dependabot security PRs (`.github/dependabot.yml`).

---

## 7. STRIDE per component

Spoofing / Tampering / Repudiation / Info Disclosure / DoS / Elevation
of Privilege. Each cell names the concrete vector; cited code where
mitigated, §12/§19 cross-references where not.

### 7.1 Connection / parser (`internets.py`)

| | Vector | Mitigation |
|---|---|---|
| S | Server-cert spoof on reconnect | TLS 1.2 minimum + hostname verify, `internets.py:521`. Downgrade only via `ssl_verify=false` — §19 residual. |
| T | CR/LF injection from server line into outbound | `Sender._write_line` strips CR/LF/NUL (`sender.py:149`). |
| R | Lost evidence of who issued a command | `_handle_privmsg` logs nick + hostmask, redacts auth arg (`internets.py:801`). |
| I | Server log captures admin password in `PRIVMSG NickServ` echo | `_RE_AUTH_LOG` redacts in receive path (`internets.py:189`, `:881`); outbound redacted in §7.6. |
| D | Oversized line OOM | `LimitOverrunError` discard path (`internets.py:870`); `limit=8192` (`internets.py:529`). |
| E | Hostmask-spoofed pseudo-admin | `is_admin` re-checks hostmask each call (`internets.py:304..314`). |

### 7.2 Command dispatch (`internets._dispatch`)

| | Vector | Mitigation |
|---|---|---|
| S | Forged dispatch (no path — dispatch only fires from the parser) | n/a |
| T | Arg-length blow-up | `_MAX_ARG_LEN=400` enforced at line 468; long arg notified, dropped. |
| R | Untracked admin action | `log.info` per dispatch (`internets.py:802`); audit_log integration is TODO §19. |
| I | Crash trace leaked to channel | `_run_cmd` returns "internal error — see log" (`internets.py:506..509`). |
| D | Task pile-up | `_MAX_TASKS=50` cap (`internets.py:473`); `_active_cmd_tasks` counter (`internets.py:488..496`). |
| E | `auth` in channel reveals password to channel | Forced to PM at `internets.py:464`. |

### 7.3 Weather dispatcher + providers (`weather_providers/`)

| | Vector | Mitigation |
|---|---|---|
| S | Compromised provider injects IRC formatting | `_sanitize` strips IRC control bytes per test SEC-WP-004 (`modules/weather.py`). |
| T | Oversized response | `_MAX_RESPONSE_BYTES` cap (`weather_providers/_http.py`, test SEC-WP-001). |
| R | Untraceable provider call | `record_failure` is wired (`weather_providers/_dispatch.py:340`); success-side `record_call` is **not** wired — §19/§20. |
| I | API key in URL leaked in exception | `type(e).__name__` only, test SEC-WP-002. |
| D | One slow provider blocks dispatch | Circuit breaker `ProviderHealth.is_callable` (`weather_providers/_health.py:196`) — gate not yet enforced in dispatcher, §20. |
| E | Key from one provider used by another | Provider isolation: each provider takes only its own key constructor (`weather_providers/__init__.py`). |

### 7.4 `secret_store`

| | Vector | Mitigation |
|---|---|---|
| S | Wrong file picked up | `SECRETS_FILE.resolve()` at import (`secret_store.py:46`). |
| T | World-writable `secrets.ini` | `perms_ok` requires exactly 0o600 (`secret_store.py:162..176`). |
| R | Migration not logged | `migrate` reports `stored:/error:/skipped:` per name (`secret_store.py:385..420`). |
| I | Plaintext config.ini residue | `_scrub_config_ini` zeroes values after migrate (`secret_store.py:361..382`). |
| D | Missing secret breaks startup | `cred()` falls through to `default` (`modules/base.py:16..45`); empty value is acceptable. |
| E | Keyring backend trojaned | Fails open to file path; backend lookup wrapped to log type only (`secret_store.py:198..201`). |

### 7.5 `store.Store`

| | Vector | Mitigation |
|---|---|---|
| S | Wrong JSON file loaded | Type assertion against `default` (`store.py:156..161`). |
| T | Tampered `users.json` | v2 envelope SHA-256 check (`store.py:81..88`); checksum-fail rejects file and uses default. |
| R | No history of edits | Out of scope; `audit_log.py` is for admin-action history. |
| I | PII (hostmask) leaks via JSON | Files 0o600 by inode perms; user opt-out flag (`store.py:351`). |
| D | 100-MB JSON DoS | `_MAX_FILE_SIZE = 10 MB` (`store.py:133..146`). |
| E | Path-traversal write | All writes via `tempfile.mkstemp(dir=p.parent)` + `os.replace` (`store.py:167..191`); paths come from config, not from IRC input. |

### 7.6 `Sender`

| | Vector | Mitigation |
|---|---|---|
| S | n/a |  |
| T | CR/LF/NUL injection in body | stripped at `sender.py:149`. |
| R | Outbound secret visible in log | `_REDACT_OUT` prefix list — `PASS`, `OPER`, `NickServ IDENTIFY`, `AUTHENTICATE`, etc. (`sender.py:113..141`). |
| I | Token-bucket reveals attacker timing | Equal-jitter backoff on reconnect (`internets.py:116..125`). |
| D | Send queue OOM | `MAX_QUEUE=200` (`sender.py:30`); pri-0 (PONG) eviction guarantee (`sender.py:58..98`). |
| E | n/a |  |

### 7.7 Admin auth (`admin_cmds.py`)

| | Vector | Mitigation |
|---|---|---|
| S | Nick borrow after legit admin parts | Hostmask binding (`admin_cmds.py:134`); re-check in `is_admin` (`internets.py:304`). |
| T | Hash format tampering | `_validate_hash` exits non-zero on unknown prefix (`botlog.py:173..199`); `cmd_rehash` re-checks (`admin_cmds.py:287..299`). |
| R | Successful + failed auths logged | `log.info`/`log.warning` with nick + hostmask + counter (`admin_cmds.py:139`, `:145`). audit_log call NOT wired — §19/§20. |
| I | Exception message leaks hash fragment | Only `type(e).__name__` logged (`admin_cmds.py:128`). |
| D | Brute-force | Sliding-window lockout after 5 fails (`admin_cmds.py:91..110`). |
| E | Password stored in plain | Argon2id / scrypt / bcrypt verify, constant-time compare (`hashpw.py:269`). |

### 7.8 Console (`console.py`)

| | Vector | Mitigation |
|---|---|---|
| S | Process running someone else's stdin | Treated as local-host equivalent of admin; `--no-console` for daemon mode (`config.py:97`, `internets.py:993`). |
| T | n/a |  |
| R | Console-issued shutdown logged | `log.info(f"Console shutdown: {reason}")` (`console.py:59`). |
| I | Status dump prints admins set | Prints nicks only — no hostmask, no password (`console.py:67..82`). |
| D | EOF on stdin tears down bot | `EOFError`/`KeyboardInterrupt` exits loop (`console.py:36`); bot enters graceful_shutdown via `bot.request_shutdown` path in `_main` (`internets.py:1002`). |
| E | Local user → bot admin | Policy boundary, not code defect — see §12. |

---

## 8. Dependency surface

Versions from `pyproject.toml`; no local `.venv` to introspect. CVE
column reflects known issues against the pinned ranges as of the
document date — verify with `pip-audit` (CI workflow:
`.github/workflows/security.yml`).

| Package | Pinned | License | Notable CVEs / notes | Criticality |
|---------|--------|---------|----------------------|-------------|
| `requests` | `>=2.31.0,<3` | Apache-2.0 | 2.32 fixed verify=False sessions cache (CVE-2024-35195) — covered by pin if upgraded | high (sole sync HTTP path) |
| `aiohttp` | `>=3.9.0,<4` (extra `async`) | Apache-2.0 | 3.9.x sequence of CVEs (request smuggling, DoS); pin permits patched releases | high |
| `cryptography` | `>=41.0.0,<44` (extra `weatherkit`) | Apache-2.0 / BSD | Range straddles CVE-2024-26130 (NULL deref) — recommend `>=42.0.4` floor | high (JWT signing) |
| `PyJWT` | `>=2.0.0,<3` (extra `weatherkit`) | MIT | CVE-2022-29217 fixed in 2.4.0 — bump floor to `>=2.4.0` | high |
| `argon2-cffi` | `>=23.1.0,<24` (extra `argon2`) | MIT | No known criticals in range | high (admin auth) |
| `bcrypt` | `>=4.0.0,<5` (extra `bcrypt`) | Apache-2.0 | n/a in range | medium |
| `keyring` | `>=24.0.0,<26` (extra `keyring`) | MIT | n/a — backend choice carries its own risk surface | medium |
| `setuptools` (build) | `>=68.0` | MIT | CVE-2024-6345 fixed in 70.0.0 — bump build pin | medium |
| `wheel` (build) | unpinned | MIT | n/a | low |

Recommendation: enable Dependabot security-only PRs (already configured
`.github/dependabot.yml`); raise the floor on `cryptography` and `PyJWT`
in the next dep bump cycle.

---

## 9. Privilege model

Required:

- Outbound TCP to IRC server (port in `config.ini`) and TCP/443 to every
  host in §5.
- Filesystem read/write in the working directory: `config.ini`,
  `config.local.ini` (`config.py:40`), `secrets.ini` (`secret_store.py:46`),
  `locations.json` / `channels.json` / `users.json` (`store.py:106`),
  `audit.log` (`audit_log.py:95`), the pending `internets.pid`
  (`process_lock.py:118`), and the log files (`botlog.py:121..140`).
- POSIX signal handlers: SIGINT / SIGTERM (graceful shutdown,
  `internets.py:814..819`), SIGHUP (rehash, `internets.py:820..824`).

Forbidden by design:

- No `exec` / `subprocess` for arbitrary user input. `os.execv` is used
  only on operator-initiated restart with the bot's own argv
  (`internets.py:1033`).
- No kernel module loading.
- No SUID / SGID.
- Should be runnable as a non-root, unprivileged user. The bot binds no
  listening sockets.

---

## 10. Defence-in-depth layers

Top → bottom:

1. **TLS to IRC + APIs.** `ssl.create_default_context()` + TLS 1.2 floor
   (`internets.py:521`); HTTPS for every API in §5 except two HTTP
   residuals.
2. **SASL PLAIN over CAP.** Negotiated in `_handle_cap`
   (`internets.py:605..655`); payload built with `sasl_plain_payload`
   (`protocol.py:105`).
3. **`secret_store` tiered lookup with 0o600 enforcement.**
   `perms_ok` fails closed (`secret_store.py:162..176`); env → keyring →
   file lookup order (`secret_store.py:181..219`).
4. **`cred()` placeholder filter.** Strips template values like
   `"set-in-secret-store"` so they never reach the wire
   (`modules/base.py:10..45`).
5. **Sender output redaction.** `_REDACT_OUT` masks the secret in 14
   command prefixes including `PASS`, `OPER`, `NICKSERV IDENTIFY`,
   `AUTHENTICATE`, `CHANSERV IDENTIFY` (`sender.py:113..141`).
6. **SSRF guard.** Re-resolves at every redirect hop, rejects RFC1918 /
   loopback / link-local / ULA / IPv4-mapped IPv6 / metadata hosts
   (`modules/urls.py:43..117`).
7. **Input validation.** `_TARGET_RE` for IP/host
   (`modules/ipinfo.py:23`); `_LANG_RE` for translate; `_CHAN_RE` for
   channel names (`internets.py:190`).
8. **Per-nick rate limiter.** `RateLimiter.flood_check` /
   `api_check` (`store.py:388..429`); admins bypass flood.
9. **Per-command rate limiter.** `api_cooldown` from config
   (`config.py:63`); applied per nick via `rate_limited`
   (`internets.py:322`).
10. **Command-task cap.** `_MAX_TASKS=50`; O(1) counter check
    (`internets.py:473`).
11. **Circuit breaker on providers.** `ProviderHealth.is_callable`
    exposes consecutive-failure state
    (`weather_providers/_health.py:196`); `record_failure` wired
    (`weather_providers/_dispatch.py:340`); pre-call gate not yet
    consulted by dispatcher — §20.
12. **Atomic JSON writes + v2-schema checksum.**
    `tempfile.mkstemp` + `os.replace` + SHA-256 envelope
    (`store.py:167..191`, `:38..91`).
13. **Process lockfile.** PID + start-time + hostname, stale detection
    via `os.kill(pid, 0)` (`process_lock.py:101..220`). Not yet wired
    — §19/§20.
14. **Audit log of admin actions.** Hash-chained, 0o600,
    `verify()` re-walks the chain (`audit_log.py:141..239`). Wire-up
    to admin handlers is TODO — §19/§20.

---

## 11. Top loss-event scenarios

Each leaf: P(rob) / I(mpact) / current mitigation / residual.

### 11.1 Admin password recovered from disk

```
Root: attacker recovers admin plaintext from config.local.ini
├── Read config.local.ini directly
│   ├── P=med (local user, same UID)
│   ├── I=high (full bot control)
│   ├── Mitigation: hash stored is Argon2id/scrypt/bcrypt
│   │     (hashpw.py:211..229), constant-time verify (hashpw.py:269)
│   └── Residual: GPU offline attack remains; OWASP-2024 params
│        default (hashpw.py:67..71), self-test warns if <50 ms
│        (hashpw.py:348..355)
├── Brute force via .auth
│   ├── P=low
│   ├── I=high
│   ├── Mitigation: 5-fail sliding lockout (admin_cmds.py:91..110)
│   └── Residual: no 2FA — §19
└── Memory scrape of running process
    ├── P=low (requires same UID + ptrace allowed)
    ├── I=high
    └── Mitigation: out of scope — operator host is the trust root
```

### 11.2 Persistence path via module loading

```
Root: attacker plants modules/evil.py and gets it loaded
├── Path traversal in .load arg
│   ├── P=low
│   ├── I=high (RCE at bot UID)
│   ├── Mitigation: re.match name regex + Path.resolve().relative_to
│   │     (internets.py:334..344, test BUG-035)
│   └── Residual: an admin with shell access can drop a file legitimately
│        — §12 policy boundary
├── Symlink into modules/
│   ├── P=low
│   ├── I=high
│   └── Mitigation: relative_to check is performed on resolved path
│        (internets.py:342)
└── Loader exception leaks path detail to IRC
    ├── P=med
    ├── I=low
    └── Mitigation: "see log for details" (internets.py:365, test SEC-008)
```

### 11.3 SSRF abuse path via `.expand` reaching loopback / metadata

```
Root: attacker drives bot into http://169.254.169.254/ via .expand
├── Direct URL
│   ├── P=med
│   ├── I=high (cloud creds)
│   └── Mitigation: _METADATA_HOSTS + RFC1918/loopback/link-local
│        rejection (modules/urls.py:40..117)
├── DNS that returns both public+private answers (rebinding)
│   ├── P=low
│   ├── I=high
│   └── Mitigation: every addrinfo answer checked, fail on any
│        private (modules/urls.py:90..98)
├── Redirect chain to private
│   ├── P=med
│   ├── I=high
│   └── Mitigation: manual redirect walk re-validates per hop
│        (modules/urls.py:119..156); _MAX_REDIRECTS=5
└── DNS TOCTOU between getaddrinfo and connect
    ├── P=low
    ├── I=high
    ├── Mitigation: none in code path today
    └── Residual: §19 — hostname-pinned adapter recommended
```

### 11.4 Credential disclosure via log file

```
Root: a secret ends up in internets.log (rotated to disk, possibly to
log-shipper)
├── Outbound IRC line carrying password
│   ├── P=med (NickServ IDENTIFY fallback path, internets.py:890)
│   ├── I=high
│   └── Mitigation: _REDACT_OUT (sender.py:113..141, test
│        SEC-sender-redaction line 719)
├── Inbound line echoing a .auth attempt
│   ├── P=med
│   ├── I=high
│   └── Mitigation: _RE_AUTH_LOG redacts before debug log
│        (internets.py:881)
├── Exception text from auth backend
│   ├── P=low
│   ├── I=high
│   └── Mitigation: only type(e).__name__ logged
│        (admin_cmds.py:128, secret_store.py:138..146)
└── Control-byte injection that hides log entries
    ├── P=low
    ├── I=med
    └── Mitigation: _SafeFormatter strips C0/DEL/C1
        (botlog.py:28..61, tests SEC-007, BUG-032)
```

### 11.5 Tampered `users.json` causing misattribution

```
Root: attacker edits users.json to bind their hostmask to admin nick
├── Direct edit on disk
│   ├── P=low (requires same UID)
│   ├── I=med (admin session forgery on next admin .auth attempt)
│   └── Mitigation: hostmask is still recomputed from live IRC
│        traffic before admin grant (admin_cmds.py:134); users.json
│        does not feed is_admin
├── Bypass v2 checksum
│   ├── P=low
│   ├── I=med
│   └── Mitigation: SHA-256 over canonical-JSON; mismatch → default
│        (store.py:80..89)
└── Oversized file DoS at startup
    ├── P=low
    ├── I=low
    └── Mitigation: 10 MB cap (store.py:133..146)
```

### 11.6 Unauthenticated channel join via INVITE flood

```
Root: a hostile peer drives the bot into thousands of channels to
exhaust file handles or saturate the IRC link
├── Single attacker rapid INVITEs
│   ├── P=med
│   ├── I=med (op time + disk churn on channels.json)
│   └── Mitigation: 5 s cooldown across all invites
│        (internets.py:545, :553), channel-name regex
│        (internets.py:190, :551)
├── Multi-source coordinated INVITEs
│   ├── P=low
│   ├── I=med
│   └── Residual: 5 s is a global cooldown — covers this too
└── Send-queue OOM from welcome floods on each join
    ├── P=med
    ├── I=med
    └── Mitigation: MAX_QUEUE=200 + drop with log warning
        (sender.py:30, :98)
```

---

## 12. Insider-risk paragraph

An authenticated admin can `.load` any file under `modules/`
(`internets.py:332..366`); the directory-escape check
(`internets.py:342`) blocks path traversal but not a legitimate
admin who has already written a file there. Argon2id (default for new
deployments per `hashpw.py:190..206`) protects only against *offline*
recovery of the admin password — it does not, and cannot, restrain an
operator who is the legitimate possessor of that password. This is a
policy boundary, not a code defect: the operator-trust model is
"operator = full trust on this host". Recommendations:

- Rotate the admin password on a schedule (see `KEY_ROTATION.md`).
- Run the bot on a dedicated machine so a compromise does not pivot to
  other services.
- Do not share the admin password across nicks; revoke sessions with
  `.deauth` or `.rehash` when the trust set changes
  (`admin_cmds.py:147`, `:269..306`).
- For shared-team deployments, consider out-of-band approval for
  `.load`/`.unload` rather than a single shared credential.

---

## 13. Supply-chain considerations

- **Pinned dependencies in `pyproject.toml`** (`pyproject.toml:32..52`).
- **`pip-audit` and `bandit` SARIF** in
  `.github/workflows/security.yml` (jobs `pip-audit`, `bandit`),
  scheduled weekly + on every push/PR.
- **`gitleaks` secret scanning** also in
  `.github/workflows/security.yml`.
- **SBOM** generated by `scripts/sbom.sh`.
- **Dependabot** security-grouped PRs (`.github/dependabot.yml`).

Recommendations: sign releases with sigstore/cosign; commit a
`requirements.lock` with hash pins so reproducible installs cannot be
substituted at install time.

---

## 14. IRC-protocol-specific risks

- **IRC server impersonation.** A compromised server can speak as
  `ChanServ` and authorise `.join` requests. Mitigation: trust the
  operator's chosen network only; the bot has no concept of a
  "trusted services list" beyond `cfg["bot"].services_nick`
  (`internets.py:218`). Document the assumption.
- **Hostmask spoofing on IRCds without identity verification.** Admin
  session is keyed on `(nick, hostmask)` (`admin_cmds.py:134`,
  `internets.py:304..314`); a weak hostmask (no ident, no account-tag)
  means a weak session. Recommendation: require SASL on the bot's
  network; advise admins to authenticate to NickServ before `.auth`.
- **SASL re-auth across reconnect.** `_handle_cap` rebuilds the SASL
  payload from the runtime nick on each connection
  (`internets.py:625..631`); the SASL response is not stored in any
  long-lived field and the connection state resets on reconnect
  (`internets.py:531`). Reconnect always re-CAP-LSes
  (`internets.py:860`).
- **Console process is unauthenticated.** Admin equivalent on the
  local host (`console.py:31..65`). Mitigation: `--no-console` for
  daemon mode (`config.py:85..98`, `internets.py:993`).
- **Module hot-reload is a code-loading path.** Any file under
  `modules/` can be made live by `.load`; the path is sandboxed via
  `MODULES_DIR.resolve()` + `Path.resolve().relative_to(...)` check
  (`internets.py:338..344`, test BUG-035 line 1196).
- **`.rehash` re-reads config and clears admin sessions.** The reload
  path validates the new `password_hash` prefix before continuing
  (`admin_cmds.py:287..299`); argon2 params live inside the hash
  string itself (`hashpw.py:213..217`) and cannot be swapped mid-session.
  Sessions are cleared on rehash (`admin_cmds.py:301..305`) and on
  SIGHUP (`internets.py:979..984`).

---

## 15. Cryptographic agility

- **Argon2 params** live in `hashpw.py:67..110` and can be raised by
  re-hashing the admin password — see `KEY_ROTATION.md`. Self-test
  auto-degrades params if the host can't reach the configured cost
  (`hashpw.py:295..358`).
- **If `argon2-cffi` is pulled** (CVE), fall-back: `python hashpw.py
  --algo scrypt` produces a stdlib-only hash; existing argon2 hashes
  continue to verify until rotated because the algorithm tag is in the
  hash string (`hashpw.py:213..229`).
- **JWT signing** for WeatherKit uses `PyJWT` + `cryptography`
  (`pyproject.toml:40`). If either has a CVE, document the swap path:
  there is no `cryptography`-free EC sign path, so the alternatives are
  (a) migrate WeatherKit to RSA-256 (Apple supports it on the JWS
  envelope) and use a different signing backend, or (b) disable the
  WeatherKit provider via `provider_priority`
  (`weather_providers/__init__.py`, test "configure: ignores unknown
  provider IDs") until the upstream CVE is patched.
- **TLS context** is OS default with TLS 1.2 floor
  (`internets.py:521`); raising to TLS 1.3 floor would break IRCds
  that still negotiate 1.2 — keep current floor.

---

## 16. Logging / forensic readiness

For each §11 scenario, name the log line that catches it.

| Scenario | Catching log | Source |
|----------|--------------|--------|
| 11.1 Brute-force `.auth` | `Failed auth: <nick> (<hm>) N/5` | `admin_cmds.py:145` |
| 11.1 Auth lockout | `Auth lockout: <nick> (<hm>) N failures` | `admin_cmds.py:109` |
| 11.2 Module load | `event=module_loaded name=…` | `internets.py:359` (audit_log integration TODO) |
| 11.2 Module load fail | `event=module_load_failed name=… err=…` | `internets.py:364` |
| 11.3 SSRF block | `URL blocked by SSRF guard: …` | `modules/urls.py:130` |
| 11.4 Outbound secret | redacted → `PASS [REDACTED]`, `AUTHENTICATE [REDACTED]` | `sender.py:160..163` |
| 11.4 Inbound `.auth` | `:srv:*** AUTH [REDACTED] ***` | `internets.py:881` |
| 11.5 Tampered JSON | `Store: checksum mismatch (file=… computed=…) — rejecting` | `store.py:83..87` |
| 11.6 Channel join floods | `Invited to <chan> by <nick>` + `event=connection_lost` | `internets.py:555`, `:925` |
| 11.6 Send queue full | `Send queue full — dropping message` | `sender.py:98` |

Cross-reference: `audit_log.AuditLog.record` (`audit_log.py:141..192`)
should be called from every privileged handler in `admin_cmds.py` —
TODO marker at `audit_log.py:20`, `:18..21`. `_SafeFormatter`
(`botlog.py:28..61`) scrubs control bytes from every log handler at
format time. Recommendation: ship `internets.log` + `audit.log` off-host
nightly via the standard log-shipper of choice; the audit log can be
`verify()`'d in place to detect tamper after retrieval.

---

## 17. Recovery procedures

- **Admin password compromise.** Run `python hashpw.py --algo argon2`
  (`hashpw.py:370`); copy the printed `password_hash = …` line into
  `config.local.ini`; restart the bot (or use `.rehash` from an
  authenticated session, `admin_cmds.py:269`). All admin sessions are
  cleared on rehash (`admin_cmds.py:301..305`) — every admin must
  re-`.auth`.
- **API key leak.** Rotate at the provider; `python -m secret_store set
  <name>` (`secret_store.py:622`); `.reload <module>` to pick up the
  new credential (`admin_cmds.py:241..246`). For provider-side keys
  shipped in URLs see the historical fixes noted in the
  `weather_providers/*/` `_B` constants — every formerly-HTTP path is
  now HTTPS except the residuals in §19.
- **State file corruption.** Restore from operator backup (the bot
  writes atomically via `tempfile.mkstemp` + `os.replace` —
  `store.py:172..180` — so a corrupt file means external tampering or
  disk fault). If no backup: delete the file; the bot will reinitialise
  with the default value (`store.py:118..120`). The v2 envelope's
  checksum mismatch (`store.py:83..89`) is the first signal.
- **WeatherKit `.p8` compromise.** Revoke key ID in Apple Developer
  portal → generate new key → update `weatherkit_key_id` and
  `weatherkit_key_file` in `secret_store` (`secret_store.py:95..97`);
  `.reload weather`.

---

## 18. Test coverage of security guarantees

| Guarantee | Verifying test | File:line |
|-----------|---------------|-----------|
| Sender credential redaction | `sender: credential redaction in logs` | `tests/run_tests.py:719..725` |
| Log scrubbing of control bytes | `SEC-007: _SafeFormatter strips CR/LF/NUL` | `tests/run_tests.py:1016..1026` |
| Log scrubbing of args (not just msg) | `BUG-032: _SafeFormatter sanitizes record.args` | `tests/run_tests.py:1134..1163` |
| Command name validation | `IRCBot._CHAN_RE validates …` + load_module regex | `tests/run_tests.py:1267..1282`, `internets.py:334` |
| Channel name validation | `channels: _CHAN_RE validates IRC channel names` | `tests/run_tests.py:1396..1407` |
| Max-task cap | `BUG-030: _MAX_TASKS constant defined and enforced` | `tests/run_tests.py:1098..1105` |
| Line truncation at 512 bytes | `BUG-026: sender enforces 512-byte IRC line limit` + `BUG-026: sender _write_line truncates` | `tests/run_tests.py:1035..1063` |
| CRLF/NUL injection stripped | `sender: CRLF/NUL injection stripped` | `tests/run_tests.py:698..717` |
| TLS 1.2 minimum | `SEC-009: _connect enforces TLS 1.2 minimum` | `tests/run_tests.py:1028..1033` |
| Symlink-escape blocked | `BUG-028 …` + `BUG-035: symlink check uses Path.relative_to` | `tests/run_tests.py:1087..1091`, `:1196..1202` |
| World-readable config warning | `BUG-029: startup warns about world-readable config` | `tests/run_tests.py:1093..1096` |
| No exception text leak on auth | `SEC-014: cmd_auth does not leak ValueError text to IRC` | `tests/run_tests.py:1189..1194` |
| No exception text leak on rehash | `SEC-013: cmd_rehash does not leak exception text to IRC` | `tests/run_tests.py:1182..1187` |
| Generic error on command crash | `SEC-008: _run_cmd sends generic error` | `tests/run_tests.py:1115..1119` |
| Provider response size cap | `SEC-WP-001: _http has response size limit` | `tests/run_tests.py:571..577` |
| Provider exception scrubbing | `SEC-WP-002: provider exception logging does not leak API keys` | `tests/run_tests.py:578..587` |
| Weather output sanitisation | `SEC-WP-004: weather module sanitizes API strings` | `tests/run_tests.py:597..611` |
| Store type validation | `BUG-051: Store._read validates loaded data type` | `tests/run_tests.py:1325..1334` |
| Sender queue bounded | `BUG-056: sender queue is bounded` | `tests/run_tests.py:1319..1323` |
| PING payload capped | `BUG-050: PING payload capped to prevent oversized PONG` | `tests/run_tests.py:1233..1238` |
| Nick collision uses `secrets` | `SEC-018: nick collision uses secrets, not random` | `tests/run_tests.py:1310..1317` |
| Hash format validation | `hashpw: invalid hash format` / `empty hash` | `tests/run_tests.py:741..755` |
| Argon2/scrypt/bcrypt round-trip | `hashpw: scrypt round-trip` + `hashpw: verify_password rejects wrong password` | `tests/run_tests.py:734..739`, `:1485..1500` |

---

## 19. Known residual risks (ordered by severity)

1. **DNS pinning TOCTOU in `modules/urls.py`.** Between
   `socket.getaddrinfo` (`modules/urls.py:87`) and the eventual
   `requests` `connect()` call (`:133`), a hostile resolver could
   return a different answer. The all-answers check
   (`modules/urls.py:90..98`) closes the multi-answer rebinding case
   but not the single-answer race. Recommendation: hostname-pinned
   `HTTPAdapter` so the IP we validated is the IP we connect to.
2. **`modules/translate.py` uses an unofficial Google Translate
   endpoint.** `translate.googleapis.com/translate_a/single` is not a
   documented public API (`modules/translate.py:65`); rate limits and
   contract may change without notice. No credential leakage path —
   but availability risk is unmanaged.
3. **`weatherstack` free tier was HTTP-only.** The provider URLs are
   now HTTPS (`weather_providers/weatherstack/current.py:6`); the
   comments at `:5`, `:7`, `:7` flag that the *fix* moved them to
   HTTPS. Verify the deployed key is on a tier that accepts HTTPS;
   a downgrade re-introduces the key-in-query-string exposure.
4. **No 2FA on admin authentication.** `cmd_auth`
   (`admin_cmds.py:63..145`) accepts a single secret. Brute-force is
   mitigated by lockout (`:91..110`), but a leaked plaintext bypasses
   all of that. Add TOTP or services-only authentication if the
   threat model warrants it.
5. **`ip-api.com` over plaintext HTTP.** `modules/ipinfo.py:64` —
   the request is bounded (`_TARGET_RE`, `_MAX_BODY_BYTES`,
   `_strip_ctrl`) but on-path observers see the queried IP/host.
6. **`audit_log.py` exists but is not yet called from admin handlers.**
   TODO marker at `audit_log.py:20`; the chain is verified-tamper-evident
   and ready, but no `audit_log.default().record(...)` calls exist in
   `admin_cmds.py` today. Forensic gap on `.load`/`.unload`/`.restart`/
   `.shutdown`/`.rehash`/`.mode`/`.snomask`.
7. **`process_lock.py` exists but is not yet wired into
   `internets.py` startup.** TODO at `process_lock.py:3`. Two
   concurrent bots against the same `users.json` will race and
   silently corrupt state (`store.py:167..191` is atomic per-process
   but cross-process collisions can still clobber).
8. **`ssl_verify=false` is silently accepted by config.** Set in
   `config.ini:30..31`, consumed at `internets.py:515`. The current
   path logs `verify=false` once at connect
   (`internets.py:517..518`) but does not WARN on each reconnect.
   Recommend a `log.warning` per reconnect to keep the regression
   visible.
9. **Console grants admin equivalence locally with no auth.**
   `console.run_console` (`console.py:31..65`) accepts `shutdown`,
   `debug`, `loglevel`, `status` from stdin. Mitigation today is the
   `--no-console` flag (`config.py:97`) — operators running under
   systemd should set it.

---

## 20. Prioritised recommendations

Security-impact-per-effort, in order:

1. **Wire `audit_log.default().record(...)` calls** into every
   privileged handler in `admin_cmds.py`
   (`cmd_load`/`cmd_unload`/`cmd_reload`/`cmd_reloadall`/`cmd_restart`/
   `cmd_rehash`/`cmd_mode`/`cmd_snomask`/`cmd_shutdown`/`cmd_loglevel`/
   `cmd_debug`) — closes residual §19#6. Trivial change, large
   forensic uplift.
2. **Wire `ProcessLock` into `internets._main`** (`internets.py:989`)
   as `with ProcessLock(...)` — closes residual §19#7 and prevents
   the cross-process state-corruption class.
3. **Hostname-pinned `HTTPAdapter` in `modules/urls.py`** to close
   the DNS TOCTOU window (residual §19#1). Use the validated IP from
   `_host_is_safe` (`modules/urls.py:67..98`) directly in the
   `requests` `connect` callback.
4. **Log a `WARNING` per reconnect when `ssl_verify=false`** so the
   downgrade does not become invisible operational debt
   (residual §19#8). Single line addition at `internets.py:523`.
5. **Add `--no-console` defaulting** (or refuse to enable console when
   stdin is not a TTY, which is already partially the case at
   `internets.py:993`) — document for systemd deployments.
6. **Wire `weather_providers.record_call()` into the dispatcher**
   alongside the existing `record_failure`
   (`weather_providers/_dispatch.py:340`). The success-side counter is
   defined but not called.
7. **Add a pre-call hook so the dispatcher consults
   `health.is_callable()`** before invoking a provider — the circuit
   breaker exists (`weather_providers/_health.py:196`) but is not the
   gate.
8. **Raise the `cryptography` floor to `>=42.0.4` and `PyJWT` to
   `>=2.4.0`** in `pyproject.toml:40` to clear historical CVEs from
   the pinned range.
9. **Add a recurring weekly `pip-audit --strict`** as a CI failure
   gate (currently `continue-on-error: true` in
   `.github/workflows/security.yml`).

---

## 21. Out-of-scope

This document explicitly does NOT cover:

- Denial of service via channel flooding (the IRC server's own flood
  protections are the relevant control).
- Physical access to the operator's host.
- The IRC network's own internal security.
- Third-party APIs' own internal security or terms-of-service breach.
- Social-engineering paths against the operator personally.
- Multi-tenant or shared-bot deployments (the design assumes one
  operator).
