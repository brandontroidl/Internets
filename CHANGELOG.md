# Changelog

All notable changes to Internets are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — secret-store consolidation (BREAKING for fresh setups)

- **`config.ini` is now gitignored**; `config.ini.example` is the
  committed credential-free template.  The old separate `secrets.ini`
  is gone — its `[secrets]` section is now appended to the bottom of
  `config.ini` itself (still 0o600, still falls back to the OS keyring,
  still overridden by `INTERNETS_<NAME>` env vars).  Rationale: a flat
  0o600 file beside a flat 0o644 file isn't meaningfully more secure
  than one 0o600 file holding both; the split mostly created friction.
- **`secret_store.py`** — `SECRETS_FILE` now points at `config.ini`.
  `set`/`delete` perform **text-based in-place edits** of the
  `[secrets]` section (the old configparser round-trip stripped every
  comment in the file).  `init` copies `config.ini.example → config.ini`;
  `--force` is now a wholesale overwrite (the old configparser-based
  merge was incompatible with comment preservation).  `migrate` auto-
  chmods `config.ini` to 0o600 before writing, and `_scrub_config_ini`
  is now section-aware so it never blanks the very `[secrets]` entries
  it just populated.
- **Migrating an existing install:**
  `cd ~/your-bot-dir && { echo; cat secrets.ini; } >> config.ini && shred -u secrets.ini && chmod 600 config.ini`
- **`modules/numberfact.py`** — rewritten as a Wikipedia / local-math
  hybrid because numbersapi.com is defunct (it 301-redirects to
  `rembrandtpublishing.com/<path>` which 404s).  `math` facts are now
  computed locally; `date` (MM/DD) and `year` use Wikipedia's REST
  On-This-Day and page-summary endpoints; `trivia` uses the number's
  article summary with a math-fact fallback when Wikipedia returns
  the boilerplate "natural number following X and preceding Y"
  extract.  The `.numberfact` / `.nf` command surface is unchanged.

### Fixed

- **`modules/poke.py`** — raise the response cap from 256 KB to 1 MB so
  gen-1 Pokémon (Mewtwo ≈ 425 KB, Charizard ≈ 343 KB, Charmander ≈ 299 KB)
  no longer hit "PokéAPI response too large".  Also strip leading zeros
  on numeric IDs so `.poke 06` resolves to `#6` (Charizard) instead of
  404'ing against `/pokemon/06`.

### Security

- **`modules/idlerpg.py`** — use `defusedxml.ElementTree` instead of the
  stdlib parser for 3rd-party IdleRPG XML (Bandit B314 — XXE / billion-
  laughs hardening).
- **`metrics.py`** — annotate the all-interfaces refusal guard with
  `# nosec B104` (the literals appear as a defensive *check*, not a
  bind target; false positive).
- **`secret_store.py`** — strip the secret *name* from the keyring-
  failure debug log (CodeQL `py/clear-text-logging-sensitive-data` was
  flagging the identifier).
- **`weather_providers/__init__.py`** — replace WeatherKit's
  "missing: <names>" log with a count-only message (same CodeQL query
  was flagging the comprehension that bound key+value tuples).
- **Random-pick sweep** — every `random.choice` / `random.randint` /
  `random.uniform` call site routed through `random.SystemRandom`
  (`internets.py`, `modules/bofh.py`, `modules/dice.py`, `modules/fml.py`,
  `modules/numberfact.py`, `modules/xkcd.py`).  Clears Bandit B311
  across the codebase without per-line suppressions.
- **`except Exception: pass` → debug log** in five hot paths
  (`internets.py` shadow-ban prefix parse and stdin-close on shutdown,
  `admin_cmds.py` `_state_file`, `modules/tell.py` async-save scheduler,
  `modules/seen.py` temp-file cleanup).  Same best-effort semantics,
  but now observable in `--log-level=debug`.  The remaining ~25 broad
  `except Exception: pass` sites (best-effort cleanup, fallback paths)
  are annotated with `# nosec B110: best-effort cleanup` instead of
  changed — they're intentional swallows with no observability gain.
- **`assert` → `raise RuntimeError`** at two invariant checks that
  would otherwise be stripped by `python -O` (Bandit B101):
  `process_lock.py:_read_existing` and `weather_providers/_http.py:_get_session`.
- **`# nosec B105`** on `weather_providers/weatherkit/__init__.py:105`
  (`self._token = ""` is JWT-cache init, not a hardcoded password —
  `_headers()` regenerates the token on first use).
- **`# nosec B404 / B603 / B606`** on `internets.py`'s Windows
  self-restart path (`subprocess.Popen` + `os.execv` with
  `sys.executable` + `sys.argv` — interpreter-controlled, not user input).

## [2.6.0] — 2026-05-20

### Added — 24 new modules

- **IRC-native stateful** (use `on_raw` hook + own JSON store, atomic
  0o600 writes):  `seen`, `tell`, `remind`, `notes`.
- **Stateless API toys** (no key required):  `poke` (PokéAPI), `dnd`
  (D&D 5e SRD), `mtg` (Scryfall), `iss` (ISS tracker + crew), `xkcd`,
  `apod` (NASA APOD — `DEMO_KEY` fallback), `cocktail` (TheCocktailDB),
  `recipe` (TheMealDB), `hn` (Hacker News), `reddit` (subreddit top
  post), `numberfact` (NumbersAPI), `bored` (Bored API).
- **Pure-local utilities** (no network):  `games` (`.coin` `.8ball`
  `.rps` `.choose`), `devutils` (`.b64` `.unb64` `.hex` `.morse`
  `.uuid` `.epoch`), `qr` (api.qrserver.com URL builder), `httpcode`
  (HTTP status code table), `cowsay`.
- **Live data:**  `crypto` (CoinGecko spot + 24h change, no key —
  command renamed to `.gecko` / `.cg` to coexist with the keyed
  `stocks.crypto` Finnhub/AV/TD command), `fx` (frankfurter.dev
  ECB rates), `spacex` (next launch + countdown + rocket + pad).

### Added — 10 new admin commands

- `.raw <line>` — inject a raw IRC protocol line (CR/LF/NUL rejected,
  510-byte cap, audit-logged).
- `.say [target] <text>` / `.act [target] <text>` — speak / CTCP
  ACTION as the bot (target defaults to current channel).
- `.nick <newnick>` — change bot nick at runtime (RFC-2812 validated,
  `_nick` updates on the server NICK echo).
- `.uptime` — process uptime + current-connection uptime.
- `.stats` — counters (cmds dispatched, PRIVMSG in/out), sender queue
  depth, modules loaded/configured, audit log size, RSS memory.
- `.audit [N | grep <pat> | tail | verify]` — view the audit log;
  `verify` re-walks the SHA-256 chain.
- `.fingerprint <nick>` — cross-reference everything the bot knows
  about a nick: hostmask, channels, shadow-ban status, last `.seen`,
  `.tell` counts, `.notes` count, audit-log mentions.
- `.shadow-ban <nick> [reason]` / `.shadow-unban <nick>` /
  `.shadow-list` — silently drop ALL traffic from a nick (commands +
  `on_raw` fanout); persisted to `shadow_bans.json` (0o600).

### Changed

- **`.help` redesigned for progressive disclosure** — the default view
  is now ~8 lines (a wrapped module roster + drill-down hints) instead
  of 30+.  `.help <module>` shows that module's full command list,
  `.help <cmd>` shows the one-liner, `.help admin` shows the admin
  grid, `.help all` is the escape hatch for the full alphabetical
  command grid.  Canonical alias = first key in each module's
  `COMMANDS` dict (insertion order), not the longest.
- **Module lookup before command lookup** in `.help <target>`, so
  `.help weather` shows the whole module rather than collapsing to
  the single `.weather` line.

### Fixed

- **`modules/qdb.py`** — extract the real numeric quote ID from the
  bash-org-archive permalink anchor instead of falling back to the
  literal placeholder `"qdb"` (was producing `[qdb qdb] ...` lines).
- **`modules/fml.py`** — rewritten for fmylife.com's Tailwind layout
  (the old `article-link` / `article-contents` selectors are gone).
  Regex anchors on the `block text-blue-500` class so it captures the
  full body instead of the short category tag-line (`Magic underwear`,
  `Knackered`, etc.).

## [2.5.0] — 2026-05-19

- Per-provider weather flags (`-nws`, `-aw`, `-vc`, `-om`, …) plus `-l` for
  a ranked-by-accuracy listing of currently-active providers.
- Provider chain now sorts by scientific accuracy first, then by live
  health score, then by registration order.
- Stormglass and WeatherBit providers wired into the dispatcher.
- Tiered secret store (`secret_store.py`): env → OS keyring → 0600
  `secrets.ini`.  Replaces plaintext keys in `config.ini`.
- `config.local.ini` overlay for personal non-secret settings.
- `is_configured()` hook on `BotModule` — `.help` and weather `-l` hide
  modules / providers without their key.
- Per-process lockfile (`process_lock.py`); circuit breaker on provider
  health; per-provider quota counter; geocoding TTL cache.
- Tamper-evident admin-action audit log (`audit_log.py`).
- Optional Prometheus exporter (`metrics.py`, off by default).
- DNS-pinned SSRF adapter in `modules/urls.py`.
- Hardened XML parser in `modules/qdb.py` via `defusedxml`.
- `.forgetme`, `.privacy`, `.optout`, `.optin` user commands.

## [2.4.0] and earlier

See git history.
