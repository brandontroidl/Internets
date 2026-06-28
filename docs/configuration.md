# Configuration Reference

Internets 4.0.0. Every configuration key and secret, where it is read, its type, default,
bounds, and the audit findings (dead keys, an unused secret, keys read by code but absent
from the template). Grounded in `config.py`, `config.ini.example`, `secret_store.py`, and the
actual consumers cited inline as `file:line`.

Two facts to hold first:

- `config.py` itself parses only a small subset of the file (`[irc]`, four `[bot]` keys, and
  `[logging]`) into module-level constants at import time. Most other keys are read lazily by
  the core (`internets.py`) or by individual modules off `bot.cfg` / the global `cfg`. This doc
  documents every key by its **actual reader**, not by whether `config.py` reads it.
- `config.ini` is the single 0o600 file holding both runtime settings and the `[secrets]`
  section. It is gitignored. `config.ini.example` is the committed credential-free template.

## File resolution and load order

`config.py:36-67` builds one `configparser.ConfigParser(inline_comment_prefixes=(";", "#"))`
and loads, in order:

1. `config.ini` (resolved absolute via `Path("config.ini").resolve()`, `config.py:37`).
2. `config.local.ini` if it exists (`config.py:40,62-63`), overlaid on top.

Both reads are pinned to `encoding="utf-8"` (`config.py:61,63`). This is load-bearing: the
template's section headers use box-drawing characters; without the explicit
encoding `configparser` falls back to the platform locale (cp1252 on Windows) and raises
`UnicodeDecodeError` on the first non-ASCII byte (`config.py:53-57`).

`reload_config()` (`config.py:43-64`) is the **only** sanctioned reload path - startup, SIGHUP,
`.rehash`, and `get_hash()` all route through it. Reason (`config.py:46-51`): `configparser.read()`
only overrides keys present in the file being re-read. Re-reading `config.ini` alone (which carries
empty placeholders for `password_hash` etc.) would silently clobber any value set only in
`config.local.ini`. Re-reading both in order keeps the overlay intact.

If neither file is readable, `read_files` is empty and the bot exits with an actionable message
(`config.py:72-77`) pointing at `python -m secret_store init`, rather than letting a bare
`KeyError: 'irc'` surface later.

`config.local.ini` is the place for personal **non-secret** overrides (e.g. `[admin] password_hash`,
per the template comment at `config.ini.example:81-83`). Secrets do not go there; they go in
`[secrets]` of `config.ini` or in `INTERNETS_*` env vars.

## Keys parsed by config.py (module-level constants)

These are read once at import and frozen into constants. A change requires a process restart
or an explicit `reload_config()` plus re-read of the constant; the constants themselves are not
re-bound by a rehash.

### [irc]

| Key | Constant | Type | Default | Notes |
|---|---|---|---|---|
| `server` | `SERVER` | str | none (required) | `cfg["irc"]["server"]`, hard subscript - missing key/section raises `KeyError`. `config.py:81` |
| `port` | `PORT` | int | none (required) | `int(...)`, no bound check. `config.py:82` |
| `nickname` | `NICKNAME` | str | none (required) | `config.py:83` |
| `realname` | `REALNAME` | str | none (required) | `config.py:84` |
| `oper_name` | `OPER_N` | str | `""` | `.get(...,"").strip()`. `config.py:88` |
| `user_modes` | `USER_MODES` | str | `""` | Set after MOTD, before OPER. `config.py:90` |
| `oper_modes` | `OPER_MODES` | str | `""` | Set after the 381 OPER-success numeric. `config.py:91` |
| `oper_snomask` | `OPER_SNOMASK` | str | `""` | Requires `+s` in `oper_modes`. `config.py:92` |
| `nickserv_password` | `NS_PW` | str | `""` | `_secret_or_cfg(...)`: secret_store wins, `[irc]` is legacy fallback. `config.py:86` |
| `server_password` | `SERVER_PW` | str | `""` | Same fallback chain. `config.py:87` |
| `oper_password` | `OPER_PW` | str | `""` | Same fallback chain. `config.py:89` |

`server`, `port`, `nickname`, `realname` use hard subscripts and have no default - an absent key
or section crashes at import. The credential keys (`nickserv_password`, `server_password`,
`oper_password`) resolve through `_secret_or_cfg(name, "irc", key)` (`config.py:24-31`): secret_store
first, then `cfg[irc][key]` only as a legacy fallback for upgrades from older versions that stored
plaintext in `[irc]`. The shipped template keeps these in `[secrets]`, not `[irc]`.

### [bot]

| Key | Constant | Type | Default | Bound/validation |
|---|---|---|---|---|
| `command_prefix` | `CMD_PREFIX` | str | none (required) | Must be non-empty - `SystemExit` if empty, since an empty prefix makes every message a command. `config.py:96-99` |
| `api_cooldown` | `API_CD` | int (seconds) | none (required) | Floored: `max(1, int(...))`. `config.py:102` |
| `flood_cooldown` | `FLOOD_CD` | int (seconds) | `3` | Floored: `max(1, int(...))`. `config.py:103` |
| `modules_dir` | `MODULES_DIR` | Path | `modules` | `config.py:104` |
| `autoload` | `AUTO_LOAD` | list[str] | `[]` | Comma-split, whitespace-stripped, empties dropped. `config.py:105` |

The `max(1, ...)` floor on the cooldowns is deliberate (`config.py:100-101`): a 0 or negative value
would otherwise disable the per-nick rate limiter. `RateLimiter` also clamps independently
(defence in depth). `command_prefix` is the one key whose emptiness is a fatal misconfiguration.

`AUTO_LOAD` is the startup module list. A loaded module missing its API key shows no commands in
`.help` until the key is present (`config.ini.example:76-77`); it is not an error.

### [logging]

| Key | Constant | Type | Default | Notes |
|---|---|---|---|---|
| `level` | `LOG_LEVEL` | str | none (required) | `cfg["logging"]["level"]`, hard subscript; uppercased. CLI `--loglevel` overrides. `config.py:142` |
| `log_file` | `LOG_FILE` | str | none (required) | Hard subscript. `config.py:143` |
| `max_bytes` | `LOG_MAX` | int | `5242880` (5 MB) | Per-file size before rotation. `config.py:144` |
| `backup_count` | `LOG_BACKUPS` | int | `3` | Rotated copies kept (`.1`/`.2`/`.3`). `config.py:145` |
| `debug_file` | `LOG_DEBUG` | str | `""` | Separate DEBUG/protocol log. CLI `--debug-file` overrides. Blank disables. `config.py:146` |

`level` and `log_file` are hard subscripts (no default - section must exist). `LOG_FMT` is a
hardcoded constant, not configurable (`config.py:147`).

## CLI flags (override config at runtime)

Parsed in `config.py:115-138`. These are not config-file keys but they override the matching
constants.

| Flag | Overrides | Effect |
|---|---|---|
| `--version` | - | Prints `Internets 4.0.0`, exits. |
| `--debug [SUBSYSTEM ...]` | - | No args = global debug; with args = per-subsystem (`--debug weather store`). Default `None`. |
| `--loglevel LEVEL` | `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `--debug-file PATH` | `LOG_DEBUG` | Captures all DEBUG to a separate file. |
| `--no-console` | - | Disables the interactive stdin command loop (for daemons). |

## Keys read by the core and modules (not by config.py)

Every key below appears in `config.ini.example` and is consumed lazily off `bot.cfg` / `cfg`,
not parsed into a `config.py` constant. Each is cited at its real reader.

### [irc] - connection transport

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `ssl` | bool | `true` | `internets.py:705` | `getboolean("ssl", fallback=True)`. `true`=TLS (6697), `false`=plain TCP (6667). |
| `ssl_verify` | bool | `true` | `internets.py:706` | `getboolean("ssl_verify", fallback=True)`. Set `false` for self-signed certs. |

`config.py` never reads `ssl`/`ssl_verify`; the connection layer does. Credential sends are
gated on TLS being live regardless of these (`_tls_or_refuse`, `internets.py:686`): on a plaintext
connection the bot logs CRITICAL and refuses to send NickServ/SASL/server/oper credentials.

### [bot] - state file paths and tracking

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `locations_file` | str | `locations.json` | `internets.py:243` | Store backing file. |
| `channels_file` | str | `channels.json` | `internets.py:244` | Joined channels, rejoined on restart. |
| `users_file` | str | `users.json` | `internets.py:245` | User tracking store. |
| `user_max_age_days` | int | `90` | `internets.py:246` | `int(cfg["bot"].get(...,"90"))`. Prune age for user-tracking entries. |
| `services_nick` | str | `ChanServ` | `internets.py:236`, `modules/channels.py:52` | Services bot used for channel-ownership verification. |
| `shadow_bans_file` | str | `shadow_bans.json` | `internets.py:259` | **Not in the template** - see Audit findings. Nicks whose traffic is silently dropped. |

`config.py` reads only `command_prefix`/`api_cooldown`/`flood_cooldown`/`modules_dir`/`autoload`
from `[bot]`; the paths above are read by the core at construction time. There is no documented
floor on `user_max_age_days` at this call site (the template comment says default 90; the code
default is also `"90"`).

### [admin]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `password_hash` | str | `""` | `botlog.py:174`, re-read in `admin_cmds.py:448` context | Hash gating `.load`/`.unload`/`.reload`. |

`get_hash()` (`botlog.py:168-174`) calls `reload_config()` first, then reads
`cfg["admin"].get("password_hash","").strip()` - so the value is re-pulled live (honoring the
`config.local.ini` overlay) on each check rather than frozen at import. Generate with
`python hashpw.py --algo argon2`; put the line in `config.local.ini` so it is never committed.
Plaintext (non-hash) passwords are rejected at startup: `_validate_hash()` (`botlog.py:180-208`)
does `sys.exit(1)` when the hash prefix is not one of `scrypt`/`bcrypt`/`argon2`, and it runs at
import via the `_validate_hash()` call at `botlog.py:210`. An empty hash is non-fatal (auth
disabled, first-run); a non-prefixed plaintext hash aborts startup.

### [weather]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `default_country` | str | `us` | `modules/weather.py:525`, `modules/location.py:35` | ISO 3166-1 alpha-2 home country for resolving bare numeric postal codes. Invalid values fall back to `us` inside `geocode()`. |
| `units` | str | `us` (in template) | **none** | **Dead key** - see Audit findings. No code reads `[weather] units`. |
| `user_agent` | str | (absent in template) | `cred()` fallback only | The template defines no `[weather] user_agent`; the UA lives in `[secrets].weather_user_agent`. `cred()` reads `[weather].user_agent` only as a legacy fallback. `modules/weather.py:518`, `modules/location.py:33`. |

`default_country` drives bare-numeric postal disambiguation: with `us`, `.w 43812` -> Ohio but
`.w 08000` -> Barcelona; override per-query (`.w 08000 es`). Format-unique codes (Canadian, UK,
ZIP+4) pin their own country and ignore this (`config.ini.example:94-101`).

### [weather_providers]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `provider_priority` | str (comma list) | `""` | `weather_providers/__init__.py:494` | Ordering + dispatch tie-breaker, **not** an allowlist. Also accepts legacy key name `priority` as fallback. |

`configure()` (`weather_providers/__init__.py:490-505`) parses the comma list, lowercases each
entry, then **appends every other known provider after the listed ones** (`__init__.py:504`). A
subset just sorts those first; every supported provider still registers. This is intentional so a
stale list written before the air-quality/wildfire/tides providers existed does not silently
disable whole capabilities. Providers register only when their credentials are present; `nws` and
`openmeteo` are key-free and always available (`config.ini.example:112-113`).

The provider **API keys** the template lists in comments under `[weather_providers]` actually live
in `[secrets]` (see Secrets). `CONFIG_LOCATIONS` in `secret_store.py:88-129` maps each provider
secret to `("weather_providers", key)` as its **migration source** - i.e. where `migrate` scrapes
legacy plaintext from - not where the runtime reads it.

### [steam]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `steamids_file` | str | `steamids.json` | `modules/steam.py:168` | nick -> SteamID JSON cache path. Not a secret. |

### [idlerpg]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `api_url` | str | `http://idlerpg.rizon.net/xml.php` | `modules/idlerpg.py:82` | IdleRPG XML endpoint; override for a non-Rizon network. |

### [qdb]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `api_url` | str | `https://bash-org-archive.com` (`qdb.py:30`) | `modules/qdb.py:126` | `sect.get("api_url","").strip() or _DEFAULT_URL`. Blank/absent falls back to that default and `.qdb` stays **visible** - `is_configured()` always returns True (`qdb.py:128-130`), never hidden by a blank key. `qdb.us` is defunct (2024); the live successor is `bash-org-archive.com`. |

### [metrics]

| Key | Type | Default | Reader | Notes |
|---|---|---|---|---|
| `enable` | bool | `false` | `internets.py:1345` | `getboolean("enable", False)`. Gates the whole exporter. |
| `host` | str | `127.0.0.1` | `internets.py:1349` | Prometheus listener bind host. Do **not** bind `0.0.0.0` - endpoint is unauthenticated. |
| `port` | int | `9779` | `internets.py:1350` | `getint("port", 9779)`. |

The exporter only starts if the `[metrics]` section exists **and** `enable` is true
(`internets.py:1345`). `/metrics` is unauthenticated and exposes internal counters
(`config.ini.example:145-146`).

### [seen]

Not in the template. `modules/seen.py:63-64` reads `[seen] file` (default `seen.json`) if the
section exists, else uses `seen.json`. Retention (`_max_age_days = 180`) is hardcoded in the
module, not configurable (`modules/seen.py:70`). The generic `_state_file(cfg, section, default)`
helper in `admin_cmds.py:1027-1032` follows the same `[section].file` convention for module state
files; the literal key name `file` is hardcoded inside the helper (`cfg[section].get("file", default)`,
`admin_cmds.py:1031`), so it implements the convention without taking the key as a parameter.

## Secret model

`secret_store.py` is a two-tier reversible store. Reversible, not hashed: the bot must send
NickServ/SASL/server/oper credentials and API keys on the wire, so the store provides
encryption-at-rest semantics via file perms, never one-way hashing (`secret_store.py:1-6`).
OS-keyring support was removed in 3.0.0 (`secret_store.py:13-18`): the 0o600 `config.ini[secrets]`
file backend is the only backend.

### Lookup order (`get(name)`, `secret_store.py:180-211`)

1. **Env var** `INTERNETS_<NAME_UPPER>` (`ENV_PREFIX = "INTERNETS_"`, `secret_store.py:48,186`).
   Stripped, then rejected if blank or a known placeholder (same filtering as the file tier,
   `secret_store.py:189-193`).
2. **`config.ini[secrets]`** - only if the file exists and `perms_ok()` passes
   (`secret_store.py:195-206`). Value is stripped and placeholder-filtered.
3. **Empty string** default (`secret_store.py:211`), or the caller-supplied `default`.

First non-placeholder hit wins. Env var always overrides the file.

`config.py`'s credential constants go through `_secret_or_cfg()` (`config.py:24-31`), which calls
`secret_store.get()` and falls back to `cfg[section][key]` only if the store returned nothing.
Modules go through `modules/base.py:cred()` (`base.py:117-147`), which does the same: secret_store
first, then `cfg.get(section, key)` with placeholder-marker filtering, never a bare `KeyError`.

### Permission gate (`perms_ok`, `secret_store.py:161-175`)

Fail-closed. If `config.ini` exists with a mode other than `0o600`, `get()` logs
`REFUSING to read ...` and returns the default (`secret_store.py:196-199`); `set`/`delete` raise
`PermissionError` rather than silently no-op (`secret_store.py:352-355,402-404`). An absent file is
treated as OK (`"absent"`). On Windows, POSIX modes are advisory and the check returns OK
(`"windows (acl-based)"`, `secret_store.py:169-171`).

### KNOWN_SECRETS (`secret_store.py:57-84`)

41 canonical secret names. Membership here is what makes a name part of `migrate`/`list`/`status`.
Adding a name here pulls it into the migration sweep with no other code change.

IRC auth (sent reversibly, cannot be hashed):
`nickserv_password`, `sasl_password`, `server_password`, `oper_password`.

PII / contact identifier (sent in HTTP User-Agent): `weather_user_agent`.

Weather provider keys:
`weatherapi_key`, `tomorrowio_key`, `openweathermap_key`, `visualcrossing_key`,
`pirateweather_key`, `weatherstack_key`, `accuweather_key`, `worldweatheronline_key`,
`weatherbit_key`, `stormglass_key`, `meteomatics_username`, `meteomatics_password`,
`weatherkit_team_id`, `weatherkit_service_id`, `weatherkit_key_id`, `weatherkit_key_file`,
`airnow_key`, `purpleair_key`, `waqi_token`, `openaq_key`, `iqair_key`, `tidecheck_key`,
`firms_key`, `google_pollen_key`, `n2yo_api_key`.

Other module keys:
`omdb_key`, `lastfm_key`, `youtube_key`, `finnhub_key`, `alphavantage_key`, `twelvedata_key`,
`steam_key`, `twitch_client_id`, `twitch_client_secret`, `brave_key`, `abuseipdb_key`.

`weatherkit_key_file` is a **path** to the `.p8` private key, not the key contents
(`config.ini.example:299-302`). Store the path; the file is typically owned by the bot user
already.

### Placeholder filtering (`_PLACEHOLDERS`, `secret_store.py:135-145`)

A frozenset of dummy strings treated as "not set" - never returned by `get()`, never migrated,
never counted as stored by `list`. Matched case-insensitively (callers lowercase first). Includes
`""`, `changeme`/`change-me`/`change_me`, `your-key-here` and variants, `<your-token>`,
`placeholder`, `set-via-secret-store`, `todo`/`tbd`/`xxx`, `none`/`null`/`n/a`/`na`,
`example`/`demo`/`test`/`fixme`, `insert-key-here`, and more. This is why a template's empty or
`changeme` value reads as absent and the owning module simply hides its commands.

### Storage location vs migration source

- **Runtime read location** for every secret is env var or `config.ini[secrets]`.
- **`CONFIG_LOCATIONS`** (`secret_store.py:88-129`) maps each name to the `(section, key)` where
  `migrate` looks for legacy plaintext to scrape out. Note these source sections differ from
  `[secrets]`: e.g. `omdb_key` -> `[imdb]`, `n2yo_api_key` -> `[satpass]`, `abuseipdb_key` ->
  `[ipintel]`, `brave_key` -> `[search]`, `lastfm_key` -> `[lastfm]`.
- `sasl_password` is in `KNOWN_SECRETS` but **not** in `CONFIG_LOCATIONS` - it has no legacy
  plaintext source section, so `migrate` never sweeps it.

### secret_store CLI (`python -m secret_store <cmd>`)

| Command | Effect | Notes |
|---|---|---|
| `status` | Snapshot: file path, exists, perms, env prefix. `secret_store.py:249-258,509-517` | |
| `list` | Per-secret backend: `env` / `file` / `(unset)`. `secret_store.py:261-284,520-544` | Never prints values. |
| `get <name>` | Confirms presence: `(set, N chars, backend=...)` or non-zero exit. `secret_store.py:547-567` | **No flag prints the value.** Extract via `python -c "import secret_store; print(secret_store.get('NAME'))"`. |
| `set <name> [--value V]` | Writes to `config.ini[secrets]`. `secret_store.py:570-579` | Omit `--value` to be prompted via `getpass` (keeps it out of shell history). Rejects CR/LF in the value (`secret_store.py:227-228`) to prevent line injection. |
| `delete <name>` | Removes from `[secrets]`. `secret_store.py:582-588` | Raises `PermissionError` on bad perms rather than reporting "not found". |
| `init [--force]` | Creates `config.ini` from `config.ini.example`, mode 0600, `O_EXCL`. `secret_store.py:591-636` | Byte-for-byte copy (preserves comments/URLs). Refuses to overwrite without `--force`; `--force` replaces wholesale and warns to rotate. |
| `migrate [--config P] [--no-scrub]` | Moves plaintext from `CONFIG_LOCATIONS` sources into `[secrets]`, then scrubs the source. `secret_store.py:462-504,639-664` | Prints a ROTATE-EVERYTHING warning: scrubbed values are still in git history. `--no-scrub` stores but leaves sources intact. |

`set`/`delete` are targeted text edits on the `[secrets]` block (`_write_file_secret`/
`_delete_file_secret`, `secret_store.py:342-421`), not a `configparser` round-trip, because
`configparser.write()` strips every comment. The rest of `config.ini` is left byte-for-byte
untouched. Writes are atomic via a 0o600 temp file + `os.replace` (`_atomic_write_text`,
`secret_store.py:296-314`). `migrate`'s scrub (`_scrub_config_ini`, `secret_store.py:426-459`)
intentionally exempts the `[secrets]` section so it does not blank the destination it just wrote
when source and dest are the same file.

### Visibility guarantees (asserted in code)

- `get` CLI prints only a length + backend summary, never the value (`secret_store.py:547-567`).
- `list`/`status` print backend labels only (`secret_store.py:520-544`).
- Exception logging uses `_safe_exc()` (type name only, `secret_store.py:150-158`) because
  configparser/argon2/bcrypt messages can echo fragments of the offending value.
- Outbound IRC is scrubbed for credential prefixes (`PASS`/`IDENTIFY`/`OPER`/`AUTHENTICATE`)
  before logging (`config.ini.example:25-27`).

## Audit findings (cross-check of config.ini.example vs the code)

Verified against the readers above. These are the discrepancies the next maintainer should know.

1. **`[weather] units` is a dead key.** The template ships `units = us` and documents it as the
   default unit system (`config.ini.example:87-92`), but no code reads `cfg["weather"]["units"]`.
   Searched the full tree: the only `units` references are per-provider HTTP query params
   (`weather_providers/*/...` send `units=metric`/`si` to the upstream API) and a `from .units`
   import; the weather module reads only `[weather] user_agent` and `[weather] default_country`
   from this section (`modules/weather.py:518,525`). Unit selection is not driven by this key.
   Safe to leave (harmless) but it misleads; if removed, update the template comment.

2. **`sasl_password` secret is defined but never consumed at runtime.** It is in `KNOWN_SECRETS`
   (`secret_store.py:60`) and the template promises "set this only if it differs from
   `nickserv_password`; if empty, falls back to `nickserv_password`" (`config.ini.example:184-186`).
   The SASL PLAIN auth path hardcodes `NS_PW` (the `nickserv_password` value):
   `internets.py:899` calls `sasl_plain_payload(self._nick, NS_PW)`. The only place
   `"sasl_password"` appears in `internets.py` is `_tls_or_refuse("sasl_password")`
   (`internets.py:887`), where it is just a **label** for the TLS-guard log, not a value read.
   Net effect: a distinct `sasl_password` is silently ignored - SASL always uses
   `nickserv_password`. The documented "differs from nickserv" case does not work. `set`/`list`/
   `migrate` still manage the name; only the runtime read is missing.

3. **Keys read by code but absent from the template.** A fresh `config.ini` from
   `config.ini.example` will not mention these; they fall back to in-code defaults:
   - `[bot] shadow_bans_file` (default `shadow_bans.json`, `internets.py:259`).
   - `[seen] file` (default `seen.json`, `modules/seen.py:64`); `[seen]` section entirely absent
     from the template.

4. **`provider_priority` is an ordering, not an allowlist** (restated because it reads like an
   allowlist). Omitting a provider does not disable it; unknown names are tolerated and appended
   logic ignores them. See `weather_providers/__init__.py:490-505`.

5. **Credential source-section asymmetry.** The template documents API keys inline under
   `[weather_providers]` and other `[module]` sections, but the runtime reads them from
   `[secrets]` (or env). Those inline `(section, key)` pairs exist only as `migrate` scrape
   sources in `CONFIG_LOCATIONS`. Putting a real key under `[weather_providers]` instead of
   `[secrets]` works **only** via the legacy `cred()`/`_secret_or_cfg()` fallback; the supported
   location is `[secrets]` or `INTERNETS_*`.

6. **The `[qdb]` template comment is stale vs the code.** `config.ini.example:138-139` says
   `qdb.us is defunct` and `Leave blank to keep the .qdb command hidden`, but a blank `api_url`
   does **not** hide the command: `qdb.py:126` falls back to `_DEFAULT_URL =
   "https://bash-org-archive.com"` (`qdb.py:30`), and `is_configured()` always returns True
   (`qdb.py:128-130`). The command is visible out of the box. The comment misleads a maintainer
   editing the template; fix the template comment to describe the baked-in default and that blank
   means "use the default endpoint", not "hidden".

No other key in `config.ini.example` is unread, and no `config.py`-parsed key is missing from the
template (`[irc]`, the four `[bot]` keys, and `[logging]` are all present; the three credential
keys parsed from `[irc]` live in `[secrets]` and resolve via the documented fallback).
