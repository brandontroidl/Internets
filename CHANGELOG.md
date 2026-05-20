# Changelog

All notable changes to Internets are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

(No unreleased changes.)

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
