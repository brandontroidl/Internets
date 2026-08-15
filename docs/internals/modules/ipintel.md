# ipintel.py - multi-source IP reputation aggregator (.ip / .rep)

## Purpose

Answers "is this IP bad?" in one IRC line by fanning out to five independent
reputation sources concurrently and merging them into a single verdict:
DNSBL zones (over DNS-over-HTTPS), SANS ISC / DShield, GreyNoise community,
the Tor bulk exit list, and optionally AbuseIPDB. Every source except
AbuseIPDB is keyless. Module class: `modules/ipintel.py - IpintelModule`,
built on [base](base.md).

## Commands

| Command | Alias | Usage | Reply shape |
|---|---|---|---|
| `.ip` | `.rep` | `.ip <ip\|host>` | `**<ip>** [verdict] \| DNSBL x/y: ... \| DShield N rpts [CC] \| GreyNoise <class> \| Tor exit/no \| AbuseIPDB N% (M rpts)` |

`verdict` is one of `malicious` / `suspicious` / `clean` (see `_verdict()`).
A hostname argument is resolved first; the reply reports the resolved IP,
not the hostname. Example: `.ip 185.220.101.1`.

## Integration

All JSON endpoints are FIXED, trusted URLs; the validated IP only ever
appears as a query parameter or path segment. Every JSON fetch goes through
`modules.base.fetch_json` (size-capped, streamed); the Tor list is the one
inline stream+cap fetch (`_tor_fetch()`).

| Source | Endpoint | Auth | Timeout | Cap | Helper |
|---|---|---|---|---|---|
| DNSBL (6 zones) | `https://cloudflare-dns.com/dns-query` (DoH, `application/dns-json`) | none | 6 s | 16 KB per query | `_dnsbl_one()` |
| SANS ISC / DShield | `https://isc.sans.edu/api/ip/<ip>?json` | none | 8 s | 64 KB | `_dshield_sync()` |
| GreyNoise community | `https://api.greynoise.io/v3/community/<ip>` | none | 8 s | 16 KB | `_greynoise_sync()` |
| Tor bulk exit list | `https://check.torproject.org/torbulkexitlist` | none | 10 s | 4 MB | `_tor_fetch()` |
| AbuseIPDB v2 | `https://api.abuseipdb.com/api/v2/check` | `Key` header | 8 s | 32 KB | `_abuseipdb_sync()` |

DNSBL zones (`_DNSBL_ZONES`): DroneBL, SpamCop, PSBL, UCEPROTECT, s5h,
GBUdb. Zone selection is deliberate: only zones that answer queries from
large public resolvers are included. Spamhaus ZEN refuses public resolvers
and would always read as "clean" over Cloudflare DoH, which is worse than
absent (module docstring states this rationale).

A DNSBL "listed" answer is an A record inside `127.0.0.0/8`
(`_DNSBL_LISTED_NET`) excluding the `127.255.255.0/24` public-resolver /
error sentinel (`_DNSBL_SENTINEL_NET`); DoH `Status == 3` (NXDOMAIN) means
not listed. Verified by `tests/test_ipintel.py -
test_dnsbl_one_sentinel_not_listed` / `test_dnsbl_one_not_listed_nxdomain`.

GreyNoise: an HTTP 404 (surfaced as `None` via `fetch_json(allow_404=True)`)
means "IP not observed" and is mapped to `{"classification": "unseen"}`,
distinct from `None` = source error (`test_greynoise_unseen_404`).

## Configuration

- `abuseipdb_key` - optional secret, `cred(cfg, "abuseipdb_key", "ipintel",
  "abuseipdb_key", "")` in `IpintelModule.on_load()`; env override
  `INTERNETS_ABUSEIPDB_KEY` via the secret store. Keyless: the AbuseIPDB
  worker is simply not scheduled (`_aggregate()` appends it only when
  `self._abuse_key` is truthy) and its segment is absent from the reply.
- User-Agent - `cred(cfg, "weather_user_agent", "weather", "user_agent",
  "Internets/1.0")`; the same shared UA credential every HTTP module reads
  (see Findings).
- `is_configured()` returns `True` unconditionally: the command is fully
  usable keyless, AbuseIPDB only enriches.

## State

- `_tor_cache` / `_tor_lock` - module-level, in-memory only: `{"ts":
  monotonic-time, "set": frozenset[str]}` caching the parsed Tor exit list
  for `_TOR_TTL` (3600 s). Shared across all module instances and reloads
  of the command, reset only by process restart. Nothing is persisted; no
  per-user data, so the default `BotModule.forget()` no-op is correct.

## Concurrency

`_aggregate()` schedules every source as `asyncio.to_thread(...)` and joins
with `asyncio.gather(return_exceptions=True)`. Ordering is positional: the
first `len(zones)` results are DNSBL answers zipped back against
`_DNSBL_ZONES`, then DShield, GreyNoise, Tor, and (only if a key is set)
AbuseIPDB - the append order in `_aggregate()` and the index arithmetic
(`res[n]` .. `res[n + 3]`) must stay in lockstep.

The Tor cache uses a check-then-fetch pattern: the freshness check and the
cache write hold `_tor_lock`, but the download itself runs unlocked, so two
concurrent cold-cache callers can both download the list; the second write
wins and correctness is unaffected (see Findings). The cached timestamp is
captured before the fetch, so the effective TTL is shortened by the download
duration - conservative, not a bug.

## Failure behavior

Per-source degradation, never whole-reply failure, at three layers:

1. Each sync helper catches `_NET_ERRORS` (`requests.RequestException`,
   `ResponseTooLarge`, `ValueError` which covers `json.JSONDecodeError`,
   `TypeError`, `KeyError`) plus a broad `except Exception` backstop, logs a
   warning, and returns that source's sentinel (`-1` for DNSBL/Tor, `None`
   for the dict sources).
2. `gather(return_exceptions=True)` converts any exception that still
   escapes a worker thread into a result object; `_aggregate()` maps
   non-expected types back to the sentinel (`_val()`, the `isinstance`
   checks). Behavioral evidence: `test_cmd_ip_survives_unexpected_exception`
   raises a raw `RuntimeError` from one source and asserts the reply is
   still assembled.
3. `_format()` renders sentinels explicitly: `DNSBL unknown` when zero zones
   answered, `DNSBL n/a (IPv6)` for IPv6 targets (the default zones are
   IPv4-only, so absence of data is labeled rather than shown as "clean"),
   omitted segments for `None` dict sources, no Tor segment for `-1`.

An unresolvable or non-public target fails closed before any request is
made (`cmd_ip()` refuses with a sanitized echo of the target).

## Security notes

- SSRF: none by construction. The target is validated by `_TARGET_RE`
  (charset `[A-Za-z0-9.:_-]`, max 253) and then resolved through the shared
  `modules/_netsafe.py - resolve_safe_ip()`, which returns `None` for
  private / loopback / link-local / multicast / reserved / metadata /
  IPv4-mapped targets and refuses a hostname if ANY DNS answer is unsafe
  (rebinding defense, `tests/test_netsafe.py`). Only the resulting public
  IP literal is interpolated (URL-quoted) into the fixed endpoints, so an
  internal IP or hostname is never sent to a third party, and the bot never
  connects to a user-controlled URL. `test_cmd_ip_rejects_non_public`
  covers the refusal path end to end.
- Privacy: the queried IP is disclosed to Cloudflare (as the reversed-octet
  DNSBL query name, which also reveals it to each DNSBL operator), SANS
  ISC, GreyNoise, and AbuseIPDB (with the operator's key). The Tor check is
  privacy-preserving by design: the full exit list is downloaded and the
  membership test is local (`_tor_is_exit()`), so the target never reaches
  torproject.org. The requesting nick is never sent upstream.
- Output injection: every upstream-derived field is individually run
  through `strip_ctrl` with a per-field length cap; the assembled line is
  then scrubbed only for transport bytes (`_TRANSPORT_RE`, `[\r\n\x00]`)
  so the module's own intentional `\x02` bold codes survive. Re-running the
  whole line through `strip_ctrl` would delete them - the comment above
  `_MAX_LINE` documents this invariant and `test_format_strips_control_bytes`
  / `test_format_malicious_line` pin both directions. The line is capped at
  `_MAX_LINE` (400) to stay within one PRIVMSG split.
- Rate limiting: per-nick via `IpintelModule._gate()` ->
  `bot.rate_limited(nick)`; refusal is sent as a NOTICE to the nick, not to
  the channel.

## Functions and methods

| Symbol | Purpose |
|---|---|
| `_dnsbl_name()` | Reversed-octet query name for IPv4, `None` for IPv6/invalid (so IPv6 reads as "n/a", never "clean") |
| `_dnsbl_one()` | One zone lookup over DoH; 1 listed / 0 clean / -1 unknown |
| `_dshield_sync()` | ISC `ip` object or `None` |
| `_greynoise_sync()` | GreyNoise record; 404 -> `{"classification": "unseen"}` |
| `_abuseipdb_sync()` | AbuseIPDB `data` object; `None` when keyless or on error |
| `_tor_fetch()` / `_tor_is_exit()` | Cached bulk-list download + local membership test |
| `_coerce_int()` | Tolerant int coercion for upstream-typed fields |
| `_verdict()` | Pure threshold merge: malicious if 2+ DNSBL listings, Tor exit, GreyNoise `malicious`, or abuse score >= 50; suspicious if 1 listing, abuse 25-49, or DShield count >= 10; else clean (each branch unit-tested) |
| `_format()` | Pure reply assembly from the collected result dict |
| `IpintelModule._aggregate()` | Concurrent fan-out + positional join (async) |
| `IpintelModule.cmd_ip()` | Gate -> usage -> charset check -> `resolve_safe_ip` off-thread -> aggregate -> privmsg |
| `setup()` | Module entry point |

## Findings

- questionable | `ipintel.py - _tor_is_exit()` | The exit-list download runs
  outside `_tor_lock`, so N concurrent cold-cache commands trigger N
  parallel downloads of a list capped at 4 MB; harmless duplicate work
  (last write wins), but a fetch-in-progress flag would remove it.
- questionable | `ipintel.py - IpintelModule.on_load()` | The outbound
  User-Agent is read from the `weather_user_agent` secret; the "weather"
  name for a bot-wide HTTP identity is naming drift shared by secinfo,
  dnsutils, probe, and ipinfo (cross-module; reported once here).
- test-gap | `ipintel.py - _tor_fetch()` | The `ResponseTooLarge` size-cap
  branch is never exercised; tests stub `_tor_fetch` itself
  (`tests/test_ipintel.py` patches it in every Tor test).
