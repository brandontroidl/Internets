# crypto.py - keyless crypto spot price via CoinGecko

Keyless price lookup against CoinGecko's free API, with a bounded
per-instance symbol-to-coin-id cache. One class `CryptoModule` on the shared
[base](base.md) contract; blocking pipeline `_fetch_sync()` ->
`_resolve_coin_id()` -> `_get_json()` via `asyncio.to_thread`. Deliberately
uses the `.gecko`/`.cg` names so it can coexist with the keyed `.crypto`
command in [stocks.py](stocks.md) (the class docstring states this).

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.gecko <symbol-or-name>` (aliases `.coingecko`, `.cg`) | `crypto.py - CryptoModule.cmd_crypto()` | `.gecko btc` | `BTC $43,210.50  ^+1.23% 24h  |  market cap $850.0B  |  coingecko: bitcoin` (arrow + green for gains, red for losses) |

Only the first whitespace token is used. Displayed symbol is the user's input
uppercased when it is 6 chars or fewer, else the coin id uppercased.

## Integration

Two-call flow (`crypto.py - _fetch_sync()`), both through `_get_json()` - an
inline `requests.get(stream=True)` with 256 KB cap (sanctioned form),
timeout 10 s:

1. `GET https://api.coingecko.com/api/v3/search?query=<input>` - resolve input
   to a coin id. `_resolve_coin_id()` prefers an exact (case-insensitive)
   symbol match among the returned coins, else falls back to the first result.
   Skipped entirely on a cache hit.
2. `GET https://api.coingecko.com/api/v3/simple/price?ids=<id>&vs_currencies=usd&include_24hr_change=true&include_market_cap=true`.

Privacy: sends only the queried symbol/name. No key exists to leak.

## State: the coin-id cache

`self._cache` (`dict[str, str]`, created in `on_load()`) maps lowercased user
input to coin id. Bounded at `_CACHE_MAX = 512` with FIFO eviction
(`cache.pop(next(iter(cache)))` relies on dict insertion order). The bound
exists because the key is attacker-influenceable - anyone can spam distinct
lookups. `tests/test_crypto_cache.py` drives `_fetch_sync` 250 past the bound
and asserts size and oldest-first eviction (behavioral evidence for both).
Entries have no TTL: a coin-id remapping upstream stays stale until evicted or
the module reloads. A failed price call does not evict the cached id.

## Precision and rounding (money-adjacent)

All values are IEEE 754 floats used for display only - no arithmetic beyond
what CoinGecko already computed (the 24h change comes from the API, not from
local subtraction):

- `_fmt_price()`: >= $1 -> 2 decimals with thousands commas; >= $0.01 -> 4
  decimals; below that -> 4 significant digits (`%.4g`) so micro-cap prices
  stay meaningful.
- `_fmt_marketcap()`: K/M/B/T suffix, 1 decimal.
- Change: `%.2f` with explicit `+` sign and green/red color; `change >= 0`
  (including exactly 0) renders as a gain arrow.

Zero/negative or unparsable prices reply `coingecko price unavailable`
(`price <= 0` guard), so the zero-default from `info.get("usd", 0)` cannot
render as a real $0.00 price.

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`_get_json()` returns `None` on transport, oversize (logged), or JSON errors;
`_fetch_sync()` maps that to `no coin matched` (resolve stage - same
collapse-to-not-found shape as dnd.py) or `coingecko price unavailable`
(price stage). Type-checked extraction (`isinstance` on dict layers,
`float()` in try/except) guards against shape drift. Rate limiting is checked
before argument validation in `cmd_crypto()` - harmless inversion relative to
siblings (a rate-limited user cannot even get the usage line).

## Concurrency

`_fetch_sync` runs in `to_thread` workers sharing `self._cache` with no lock.
Individual dict ops are GIL-atomic; the check-then-act eviction can
transiently overshoot `_CACHE_MAX` by a small number of entries under
concurrent misses. The bound is approximate protective state, which is
acceptable; the tests only exercise the single-threaded invariant.

## Findings

- questionable | crypto.py - `_fetch_sync()` | resolve-stage infrastructure
  failures (`_get_json` returning None for a network error) are reported as
  `no coin matched '<q>'` - misleading not-found during a CoinGecko outage.
- questionable | crypto.py - `CryptoModule.cmd_crypto()` | rate-limit check
  precedes the empty-arg usage reply, so even asking for usage costs a
  rate-limit token (all sibling modules check args first).
- test-gap | crypto.py - `_resolve_coin_id()` | exact-symbol-match preference
  over first-result fallback has no test; a regression to first-result-always
  would silently change which coin `.gecko btc` returns.
