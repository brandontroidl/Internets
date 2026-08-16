# Configuration reference

Internets 5.0.0. Every setting the code reads, audited from the parsing code
rather than from the shipped template. For each key: where it is read, its type,
whether it is required, its exact default, what values are accepted, whether it
is a secret, its environment override, when a change takes effect, and what
happens when the value is missing or invalid.

Two facts govern everything below.

**`config.py` parses only a small subset.** It resolves `[irc]`, five `[bot]`
keys, and `[logging]` into module-level constants at import. Every other key is
read lazily by the core (`internets.py`), by `admin_cmds.py`, or by an individual
module off `bot.cfg`. Keys are documented here by their **actual reader**.

**`config.ini` is one file holding both settings and secrets.** It is gitignored
and must be mode 0600 or `secret_store` refuses to read it. `config.ini.example`
is the committed credential-free template. `config.local.ini` is an optional
gitignored overlay for non-secret personal overrides.

## Load order and file resolution

`config.py` builds one `configparser.ConfigParser(inline_comment_prefixes=(";", "#"))`
and `reload_config()` reads, in order:

1. `config.ini`, resolved as `Path("config.ini").resolve()` - relative to the
   **current working directory**, not to the source tree.
2. `config.local.ini`, if it exists, overlaid on top.

Both reads pin `encoding="utf-8"`. That is load-bearing: the template's section
banners contain box-drawing characters, and without the explicit encoding
`configparser` falls back to the platform locale and raises `UnicodeDecodeError`
on Windows.

`reload_config()` is the only sanctioned reload path - startup, SIGHUP,
`.rehash`, and `botlog.get_hash()` all route through it. `configparser.read()`
overrides only keys present in the file being re-read, so re-reading `config.ini`
alone would clobber a `password_hash` set only in `config.local.ini` with the
template's empty placeholder.

If neither file is readable, `config.py` raises `SystemExit` naming the resolved
path and pointing at `python -m secret_store init`, rather than letting a bare
`KeyError: 'irc'` surface later.

## When a change takes effect

Three tiers, and the distinction matters operationally.

**Live at use-time.** The value is read from `cfg` each time it is needed, so a
`reload_config()` (SIGHUP or `.rehash`) is enough:

- `[bot] command_prefix` via `IRCBot._cmd_prefix()` and every module's help text.
- `[admin] password_hash` via `botlog.get_hash()`, which calls `reload_config()`
  itself before reading.
- `[irc] ssl` and `[irc] ssl_verify`, re-read on every `_connect()` - so a change
  applies on the next reconnect without a restart.
- `[logging] level`, re-read by `.rehash` and applied to the live filter.

**Live at module load.** The value is read in a module's `on_load()`, so
`.reload <module>` picks it up but a bare `.rehash` does not: `[weather]
default_country`, `[weather_providers] provider_priority`, `[bot] services_nick`
as seen by `channels`, and every module state-file path.

**Frozen at import.** `.rehash` and SIGHUP do **not** refresh these. They require
a process restart:

| Constant | Key | Consequence of a rehash |
|---|---|---|
| `SERVER`, `PORT` | `[irc] server`, `port` | Reconnects still use the old endpoint |
| `NICKNAME`, `REALNAME` | `[irc] nickname`, `realname` | Unchanged until restart |
| `NS_PW`, `SERVER_PW`, `OPER_PW`, `OPER_N` | `[irc]` credentials | On-wire credentials are **not** reloaded |
| `USER_MODES`, `OPER_MODES`, `OPER_SNOMASK` | `[irc]` mode strings | Unchanged until restart |
| `CMD_PREFIX` | `[bot] command_prefix` | Superseded by the live read, see below |
| `API_CD`, `FLOOD_CD` | `[bot] cooldowns` | `RateLimiter` is constructed once with these; the live limits do not change |
| `MODULES_DIR`, `AUTO_LOAD` | `[bot]` | Only consulted at startup / `.load` |
| `LOG_FILE`, `LOG_MAX`, `LOG_BACKUPS`, `LOG_DEBUG` | `[logging]` | Handlers are built once at import |

`.rehash` clears every admin session precisely because the credentials it
appears to reload were not in fact reloaded; the code comment says so
explicitly.

### The empty-prefix hazard on reload

`config.py` refuses to start with an empty `command_prefix`, because an empty
prefix makes every channel message a command. That check runs **at import
only**. `reload_config()` does not repeat it.

`IRCBot._cmd_prefix()` reads `self.cfg["bot"].get("command_prefix", CMD_PREFIX)`.
The fallback fires only when the key is **absent**. A key that is present but set
to an empty string returns `""`, and the frozen constant is never consulted. So
editing `config.ini` to blank the prefix and then issuing `.rehash` puts the bot
into the exact state the startup guard exists to prevent, with no warning. The
same gap applies to any other import-time validation: the mode-string regex check
and `_validate_hash()` also run at import only, though `.rehash` re-checks the
hash prefix independently.

## Environment variables

Secrets use `INTERNETS_<NAME_UPPER>` for any name in `KNOWN_SECRETS`
(`secret_store.ENV_PREFIX`), and always win over the file tier. Beyond those,
five non-secret variables change behavior:

| Variable | Read by | Effect |
|---|---|---|
| `INTERNETS_ALLOW_ROOT` | `internets._entry()` | `=1` permits starting as euid 0; anything else exits 1 with a CRITICAL log |
| `INTERNETS_ALLOW_TLS12` | `IRCBot._connect()` | `=1` lowers the TLS floor from 1.3 to 1.2 and logs a warning |
| `INTERNETS_ARGON2_MEM_MIB` | `hashpw._argon2_params()` | Default 128, clamped to 19..4096 |
| `INTERNETS_ARGON2_TIME` | `hashpw._argon2_params()` | Default 3, clamped to 1..20 |
| `INTERNETS_BCRYPT_ROUNDS` | `hashpw._bcrypt_rounds()` | Default 13, clamped to 10..16 |

The three `hashpw` variables are read through `_env_int()`, which logs and falls
back to the default on a non-integer, and logs and clamps on an out-of-range
value. Neither case is fatal. They affect hashing only, so changing one does not
invalidate an existing stored hash.

## CLI flags

Parsed by `argparse` at `config.py` import time, which means the parser runs
during import and `--help` or a bad flag exits from inside the import.

| Flag | Overrides | Notes |
|---|---|---|
| `--version` | - | Prints `Internets 5.0.0` and exits |
| `--debug [SUBSYSTEM ...]` | - | No args enables global debug; with args, per-subsystem |
| `--loglevel LEVEL` | `[logging] level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--debug-file PATH` | `[logging] debug_file` | Separate DEBUG capture file |
| `--no-console` | - | Disables the stdin console; required for daemonized runs |

The `--debug` epilog implies multiple subsystems can be named, and the parser
does accept `nargs="*"`. The runtime helper `botlog.apply_debug()` that the
console and `.debug` command use takes exactly one subsystem per call.

## `[irc]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `server` | str | none, required | restart |
| `port` | int | none, required | restart |
| `nickname` | str | none, required | restart |
| `realname` | str | none, required | restart |
| `ssl` | bool | `true` | next reconnect |
| `ssl_verify` | bool | `true` | next reconnect |
| `oper_name` | str | `""` | restart |
| `user_modes` | str | `""` | restart |
| `oper_modes` | str | `""` | restart |
| `oper_snomask` | str | `""` | restart |
| `nickserv_password` | secret | `""` | restart |
| `server_password` | secret | `""` | restart |
| `oper_password` | secret | `""` | restart |

`server`, `port`, `nickname`, and `realname` use hard subscripts
(`cfg["irc"]["server"]`). A missing key or a missing `[irc]` section raises
`KeyError` during import - an unhandled traceback, not a clean message. `port`
is `int()`-cast with no range check; a non-numeric value raises `ValueError` at
import.

`ssl` and `ssl_verify` are read by `IRCBot._connect()` with
`getboolean(..., fallback=True)`, accepting configparser's boolean vocabulary
(`1/yes/true/on` and `0/no/false/off`). A value outside that vocabulary raises
`ValueError` inside the connect path. `ssl = false` selects plain TCP, normally
port 6667. `ssl_verify = false` sets `check_hostname = False` and
`verify_mode = CERT_NONE`, and logs a warning on every reconnect so the state
cannot silently persist.

Security: `_tls_or_refuse()` gates every credential send on TLS being active. On
`ssl = false` the bot logs CRITICAL and sends no NickServ, SASL, server, or oper
credential at all - the connection succeeds unauthenticated rather than leaking
the password. See [the security model](security-model.md#6-network-security).

`user_modes`, `oper_modes`, and `oper_snomask` are validated at import against
`^[a-zA-Z+\- ]*$`. A non-matching value logs CRITICAL and calls `sys.exit(1)`.
`oper_snomask` requires `+s` to be present in `oper_modes` to have any effect.

The three credential keys resolve through `config._secret_or_cfg()`:
`secret_store` first, `[irc] <key>` only as a legacy fallback for upgrades from
versions that stored plaintext there. The supported location is `[secrets]` or
the `INTERNETS_*` environment variable.

## `[bot]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `command_prefix` | str | none, required | live (with the hazard above) |
| `api_cooldown` | int seconds | none, required | restart |
| `flood_cooldown` | int seconds | `3` | restart |
| `modules_dir` | path | `modules` | restart |
| `autoload` | comma list | `[]` | restart |
| `locations_file` | path | `locations.json` | restart |
| `channels_file` | path | `channels.json` | restart |
| `users_file` | path | `users.json` | restart |
| `user_max_age_days` | int days | `90` | restart |
| `services_nick` | str | `ChanServ` | module reload |
| `shadow_bans_file` | path | `shadow_bans.json` | restart |

`command_prefix` is a hard subscript. Empty is fatal at import with an explicit
`SystemExit` message; absent raises `KeyError`. There is no length or character
validation - a multi-character prefix works, and so does a prefix that collides
with normal chat.

`api_cooldown` is a hard subscript wrapped in `max(1, int(...))`. `flood_cooldown`
has default `"3"` and the same floor. The floor is deliberate: `now - ts < cd` is
never true for a non-positive `cd`, so a zero would silently disable the limiter.
`store.RateLimiter.__init__` applies the same floor independently. A non-numeric
value raises `ValueError` at import. Both values are captured once into
`RateLimiter` at bot construction, so a rehash does not change the enforced rate
even though `modules/weather.py` re-reads `api_cooldown` at its own load and will
quote the new number in its cooldown message.

`autoload` is comma-split, whitespace-stripped, with empty entries dropped. A
named module that does not exist logs a load failure and is skipped; a module
whose API key is absent loads but hides its commands from `.help` via
`is_configured()`.

`user_max_age_days` is `int(...)` with a `"90"` string default, read once into
`Store`. A non-numeric value raises `ValueError` during bot construction. It is
the prune age for per-channel user-tracking rows, which hold nick, hostmask, and
timestamps - the retention control for the bot's most sensitive dataset.

`shadow_bans_file` names the JSON file of silently-dropped nicks. It is a
security control with no integrity envelope: a corrupt file loads as empty with a
warning, so an unclean restart silently un-bans everyone.

## `[admin]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `password_hash` | str | `""` | live |

Read by `botlog.get_hash()`, which calls `reload_config()` first so the
`config.local.ini` overlay is honored on every check rather than frozen at
import. This is the only authentication credential in the file.

Accepted values: a string beginning `scrypt$`, `bcrypt$`, or `argon2$`. Generate
with `python hashpw.py --algo argon2`.

Failure behavior is asymmetric on purpose:

- Empty: not fatal. `botlog._validate_hash()` logs a warning and the bot runs
  with admin auth disabled. Intended for first run.
- Any other prefix: `log.critical` then `sys.exit(1)` at import. An unrecognised
  prefix would make `verify_password` raise on every attempt, silently disabling
  every admin command; refusing to start surfaces it immediately. The invalid
  value is never echoed back to the log.
- `.rehash` re-checks the prefix independently and refuses to report success on a
  bad one, without echoing it.

Put the hash in `config.local.ini` so it is never committed. It is a verify-only
credential and is correctly hashed; contrast with the recoverable credentials in
`[secrets]`, which must not be hashed. See
[the security model](security-model.md#5-secrets).

## `[weather]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `default_country` | ISO 3166-1 alpha-2 | `us` | module reload |
| `user_agent` | secret fallback | none | module reload |
| `units` | - | - | **unread** |

`default_country` is read by `modules/weather.py` and `modules/location.py` at
`on_load()`. It disambiguates bare numeric postal codes shared across countries:
with `us`, `.w 43812` resolves to Ohio and `.w 08000` to Barcelona. Format-unique
codes (Canadian, UK, ZIP+4) pin their own country and ignore it. An invalid value
falls back to `us` inside `geocode()` rather than failing.

`user_agent` is **not** the supported location for the User-Agent. It exists only
as the `cred()` legacy fallback for `weather_user_agent`; the template defines no
`[weather] user_agent`. Its effect is larger than the section name suggests: 49
of 75 module files read `weather_user_agent` as the bot-wide HTTP User-Agent, and
`geocode._ua_has_contact()` **fail-closes** geocoding entirely if the UA contains
neither an `@domain` nor an `http(s)://` prefix. An unset or contact-free UA
therefore disables `.w`, `.regloc`, and `.myloc` with only a log warning to
explain it.

`units = us` is shipped in the template and documented there as the default unit
system. **No code reads it.** Verified across the tree: every `units` reference is
either a per-provider HTTP query parameter or the `modules/units.py` import. Unit
selection is not driven by this key. It is harmless but misleading.

## `[weather_providers]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `provider_priority` | comma list | `""` | module reload |
| `priority` | comma list | `""` | legacy alias |

Read by `weather_providers.configure()`, called from `modules/weather.py -
on_load()`. `provider_priority` is checked first and falls back to `priority`.

It is an **ordering preference and dispatch tie-breaker, not an allowlist**.
After parsing the list, `configure()` appends every other known provider ID after
the named ones, so a provider omitted from the list still registers and simply
sorts last. This is deliberate: a stale list written before the air-quality,
wildfire, space-weather, and tide providers existed would otherwise silently
disable whole capabilities. An unknown name logs
`Unknown weather provider ... - skipping` and is ignored.

Of the 32 registered providers, 20 are API-key-gated and return `None` from their
factory when the key is absent. Eleven are keyless. One - `pollendotcom` -
registers unconditionally but needs `weather_user_agent` for its Nominatim
reverse-geocode step. If no provider registers at all, `configure()` falls back
to Open-Meteo and logs a warning.

The provider API keys are documented in the template as comments under this
section, but the runtime reads them from `[secrets]` or the environment.
`CONFIG_LOCATIONS` maps each provider secret to `("weather_providers", key)` as
its **migration source** - where `python -m secret_store migrate` scrapes legacy
plaintext from - not as a runtime read location.

## `[logging]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `level` | str | none, required | live via `.rehash` |
| `log_file` | path | none, required | restart |
| `max_bytes` | int | `5242880` | restart |
| `backup_count` | int | `3` | restart |
| `debug_file` | path | `""` | restart |

`level` and `log_file` are hard subscripts; a missing `[logging]` section raises
`KeyError` at import. `level` is uppercased and overridden by `--loglevel`.

Accepted levels are `DEBUG`, `INFO`, `WARNING`, `ERROR` (`botlog.VALID_LEVELS`).
Failure behavior differs by path, and the rehash path has a defect:

- At import, `getattr(logging, LOG_LEVEL, logging.INFO)` silently degrades an
  unrecognised level to INFO.
- In `.rehash`, `lvl = getattr(logging, new_level, None)` followed by `if lvl:`
  means an invalid level - **and also a literal `NOTSET`, whose value is 0 and
  therefore falsy** - skips the entire logging reset *and* the confirmation
  reply. The operator sees nothing and the old level stays in force.

`max_bytes` and `backup_count` configure `RotatingFileHandler`. The handler is
constructed once at import, so changes need a restart. `debug_file` blank
disables the separate DEBUG capture; `--debug-file` overrides it.

Security: the main log receives whatever umask the process has - typically 0644 -
with no permission check, while `config.ini` is fail-closed at 0600 and
`audit.log` is chmod'd to 0600 after every write. The main log holds PII
(`linktitle` URLs per channel, `regloc` nick-to-location pairs), and `.forgetme`
cannot reach it. See
[the security model](security-model.md#12-known-limitations).

## `[metrics]`

| Key | Type | Default | Reload |
|---|---|---|---|
| `enable` | bool | `false` | restart |
| `host` | str | `127.0.0.1` | restart |
| `port` | int | `9779` | restart |

The exporter starts only if the section exists **and** `enable` is true. The
`getboolean` call sits outside the surrounding `try`, so a non-boolean `enable`
raises `ValueError` and aborts startup. `host` and `port` are read inside the
`try`, so a bad `port` logs `event=metrics_start_failed` and the bot continues
without metrics.

`metrics.MetricRegistry.expose()` refuses any unspecified-address bind
(`0.0.0.0`, `::`, and whitespace variants) with a `ValueError`. A specific
non-loopback address is accepted. `/metrics` is unauthenticated: anything that
can reach the bound address reads every counter. The exporter is single-threaded,
so a stalled scraper blocks it.

## Per-module sections

Every section below is read by exactly one module at `on_load()` via
`cfg[section]` guarded by an `in` test, so an absent section falls back to the
in-code default. All of them are now in `config.ini.example`, each carrying the
same value as the in-code default, so templating them changed no behavior.

| Section | Key | Default | Templated |
|---|---|---|---|
| `[tell]` | `file` | `tells.json` | yes |
| `[notes]` | `file` | `notes.json` | yes |
| `[remind]` | `file` | `reminders.json` | yes |
| `[seen]` | `file` | `seen.json` | yes |
| `[seen]` | `max_age_days` | `180` | yes |
| `[steam]` | `steamids_file` | `steamids.json` | yes |
| `[idlerpg]` | `api_url` | `http://idlerpg.rizon.net/xml.php` | yes |
| `[qdb]` | `api_url` | `https://bash-org-archive.com` | yes |

`[seen] max_age_days` is `int()`-cast inside a `try`; a non-numeric value falls
back to 180 rather than raising. Zero disables pruning.

`[idlerpg] api_url` defaults to a **plaintext HTTP** endpoint. Override it for a
non-Rizon network, or to a TLS endpoint if the network offers one.

`[qdb] api_url` uses `sect.get("api_url", "").strip() or _DEFAULT_URL`, so blank
means "use the built-in default", not "disable". `qdb.is_configured()` always
returns True, so `.qdb` is visible out of the box regardless.

`admin_cmds._state_file(cfg, section, default)` implements the same
`[section] file` convention generically for the admin-side state inspection
commands; the literal key name `file` is hardcoded inside the helper.

## Untemplated per-module credential sections

These sections are read only as the `cred()` legacy fallback - the runtime path
is `[secrets]` or `INTERNETS_*`. None of them is defined in
`config.ini.example`, so **every one of these ini fallbacks is unreachable on a
fresh install**:

`[imdb] omdb_key`, `[lastfm] lastfm_key`, `[youtube] youtube_key`,
`[stocks] finnhub_key`/`alphavantage_key`/`twelvedata_key`,
`[twitch] twitch_client_id`/`twitch_client_secret`, `[search] brave_key`,
`[ipintel] abuseipdb_key`, `[satpass] n2yo_api_key`, `[apod] api_key`.

That is not a functional gap - the supported path works - but it means the
`(section, key)` pairs in `CONFIG_LOCATIONS` have nothing to migrate from on a
fresh install, and an operator who writes a key into one of those sections by
hand gets a section the template never mentions.

## Secret model

`secret_store.get(name)` resolves in three tiers, first non-placeholder hit wins:

1. Environment variable `INTERNETS_<NAME_UPPER>`.
2. `config.ini` `[secrets]`, only if `perms_ok()` passes.
3. The caller's default, normally `""`.

Both tiers apply the same `_PLACEHOLDERS` filter - about thirty template markers
(`changeme`, `your-key-here`, `todo`, `test`, `n/a`, `placeholder`,
`set-via-secret-store`, and variants), matched case-insensitively. A placeholder
reads as unset, which is why a freshly-initialised config leaves keyed modules
hidden from `.help` rather than sending garbage upstream.

`perms_ok()` fails closed on anything other than exactly 0600. `get()` logs
`REFUSING to read` and returns the default; `set_value()` and `delete()` raise
`PermissionError`. The check is exact equality, so a **stricter** mode such as
0400 also fails and silently falls through to defaults. On Windows the check
returns OK and relies on ACLs.

`config._secret_or_cfg()` and `modules/base.py - cred()` layer the legacy
`config.ini` fallback under the store. `cred()` additionally filters
`_PLACEHOLDER_MARKERS` (`changeme`, `your-key`, `placeholder`,
`set-in-secret-store`, `<your-`, `you@example`, `example.com`) out of the ini
tier, so a template placeholder can never reach an outbound HTTP request.

### Secret inventory

`KNOWN_SECRETS` holds 41 names. Membership is what makes a name visible to
`secret_store list`, `status`, and `migrate`. `CONFIG_LOCATIONS` holds 40 of them
with a legacy `(section, key)` migration source.

| Group | Names |
|---|---|
| IRC auth | `nickserv_password`, `sasl_password`, `server_password`, `oper_password` |
| Contact identifier | `weather_user_agent` |
| Weather providers (25) | `weatherapi_key`, `tomorrowio_key`, `openweathermap_key`, `visualcrossing_key`, `pirateweather_key`, `weatherstack_key`, `accuweather_key`, `worldweatheronline_key`, `weatherbit_key`, `stormglass_key`, `meteomatics_username`, `meteomatics_password`, `weatherkit_team_id`, `weatherkit_service_id`, `weatherkit_key_id`, `weatherkit_key_file`, `airnow_key`, `purpleair_key`, `waqi_token`, `openaq_key`, `iqair_key`, `tidecheck_key`, `firms_key`, `google_pollen_key`, `n2yo_api_key` |
| Other modules (11) | `omdb_key`, `lastfm_key`, `youtube_key`, `finnhub_key`, `alphavantage_key`, `twelvedata_key`, `steam_key`, `twitch_client_id`, `twitch_client_secret`, `brave_key`, `abuseipdb_key` |

`weatherkit_key_file` stores a filesystem **path** to the Apple `.p8` private
key, not the key material. That file's own permissions are the operator's
responsibility.

`sasl_password` is the one name in `KNOWN_SECRETS` with no `CONFIG_LOCATIONS`
entry, so `migrate` never sweeps it. It also has no runtime consumer: the SASL
PLAIN path calls `sasl_plain_payload(self._nick, NS_PW)`, so SASL always uses
`nickserv_password` and a distinct `sasl_password` is silently ignored. The
template used to promise a fallback the other way round; its comment now
records the key as inert.

### `nasa_api_key` is unregistered

`modules/apod.py` and `modules/astro2.py` both read a secret named
`nasa_api_key`, with an ini fallback of `[apod] api_key` and a final default of
`DEMO_KEY`. The name appears in **neither** `KNOWN_SECRETS` nor
`CONFIG_LOCATIONS`. Verified.

Consequences: `secret_store.get("nasa_api_key")` works, and
`INTERNETS_NASA_API_KEY` works, but `secret_store list` and `status` never show
it, and `migrate` will not relocate it out of a plaintext `[apod] api_key`. It is
invisible to the tooling that exists to inventory secrets. `config.ini.example`
carries the key in `[secrets]` with that caveat in its comment, which makes it
discoverable to a reader but not to the tooling.

### secret_store CLI

| Command | Effect |
|---|---|
| `status` | File path, existence, permission verdict, env prefix |
| `list` | Per-secret backend: `env`, `file`, or `(unset)`. Never prints values |
| `get <name>` | Prints `(set, N chars, backend=...)`; exit 1 if unset. No flag prints the value |
| `set <name> [--value V]` | Writes `[secrets]`; omit `--value` for a `getpass` prompt |
| `delete <name>` | Removes from `[secrets]`; raises on bad perms rather than reporting "not found" |
| `init [--force]` | Creates `config.ini` from the template at 0600 via `O_EXCL` |
| `migrate [--config P] [--no-scrub]` | Moves plaintext from `CONFIG_LOCATIONS` sources into `[secrets]`, then blanks the sources |

`set` rejects a value containing CR or LF, because the file backend writes
`name = value` as one line and an embedded newline would inject a fake key or
section. The **name** is not checked for CR or LF.

`set` and `delete` are targeted line edits on the `[secrets]` block, not a
`configparser` round-trip, so every comment and every other section survives
byte-for-byte. Writes go through `_atomic_write_text()`: `os.open` with mode
0600, write, `os.replace`, re-chmod - no window in which the file is
world-readable.

`migrate` exempts the `[secrets]` section from its scrub, since source and
destination are now the same file. It prints a rotate-everything warning: the
scrubbed values are still in git history if the file was ever committed.

## Failure summary

| Condition | Behavior |
|---|---|
| `config.ini` and `config.local.ini` both unreadable | `SystemExit` naming the path and `secret_store init` |
| Missing `[irc]`, `[bot]`, or `[logging]` section | `KeyError` traceback during import |
| Missing required key in one of those sections | `KeyError` traceback during import |
| Non-numeric `port`, `api_cooldown`, `flood_cooldown` | `ValueError` traceback during import |
| Empty `command_prefix` at import | `SystemExit` with an explicit message |
| Empty `command_prefix` after `.rehash` | **No check.** Every message becomes a command |
| Invalid mode string | `log.critical` then `sys.exit(1)` |
| `password_hash` with unknown prefix | `log.critical` then `sys.exit(1)` |
| `password_hash` empty | Warning; auth disabled |
| Invalid `[logging] level` at import | Silently degraded to INFO |
| Invalid `[logging] level` at `.rehash` | Whole logging reset and the reply are skipped, silently |
| Non-boolean `[metrics] enable` | `ValueError` at startup |
| Bad `[metrics] host`/`port` | `event=metrics_start_failed`; bot continues |
| `config.ini` not 0600 | Every secret reads as unset; `set`/`delete` raise |
| Unknown provider in `provider_priority` | Warning; skipped |
| Non-numeric `[seen] max_age_days` | Falls back to 180 |
| Corrupt module state JSON | Loaded as empty, warning, overwritten on next save |
| Corrupt `store.py` dataset | Quarantined to `.corrupt.<epoch>`, not overwritten |

## Cross-references

- [Security model](security-model.md) - what these settings defend and where they fail
- [Deployment](deployment.md) - first-run setup and the `secret_store init` flow
- [config internals](internals/config.md) - the parser itself, line by line
- [secret_store internals](internals/secret_store.md) - the file backend and permission gate
- [Weather providers](providers.md) - which provider each key activates
