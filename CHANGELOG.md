# Changelog

All notable changes to Internets are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`modules/poke.py`** — raise the response cap from 256 KB to 1 MB so
  gen-1 Pokémon (Mewtwo ≈ 425 KB, Charizard ≈ 343 KB, Charmander ≈ 299 KB)
  no longer hit "PokéAPI response too large".  Also strip leading zeros
  on numeric IDs so `.poke 06` resolves to `#6` (Charizard) instead of
  404'ing against `/pokemon/06`.

### Changed

- **`modules/numberfact.py`** — rewritten as a Wikipedia / local-math
  hybrid because numbersapi.com is defunct (it 301-redirects to
  `rembrandtpublishing.com/<path>` which 404s).  `math` facts are now
  computed locally; `date` (MM/DD) and `year` use Wikipedia's REST
  On-This-Day and page-summary endpoints; `trivia` uses the number's
  article summary with a math-fact fallback when Wikipedia returns
  the boilerplate "natural number following X and preceding Y"
  extract.  The `.numberfact` / `.nf` command surface is unchanged.

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
