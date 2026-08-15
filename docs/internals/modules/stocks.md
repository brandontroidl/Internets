# stocks.py - stock and crypto quotes with multi-provider failover

Keyed quote lookup that fails over across three free-tier providers -
Finnhub (60 calls/min), Alpha Vantage (25/day), Twelve Data (800/day) - in
that fixed order. One class `StocksModule` on the shared [base](base.md)
contract; six provider functions (a stock and a crypto variant per provider)
plus the failover driver `_try_providers()`, all blocking and run via
`asyncio.to_thread`. Its `.crypto` command is the keyed counterpart of the
keyless [crypto.py](crypto.md) `.gecko`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.stock <symbol>` (alias `.s`) | `stocks.py - StocksModule.cmd_stock()` | `.s AAPL` | `AAPL $123.45 ^ +1.10 (+0.90%) | O/H/L | vol or prev close | [provider]` |
| `.crypto <symbol>` | `stocks.py - StocksModule.cmd_crypto()` | `.crypto BTC` | `BTC/USDT $43,210.00 ^ ... | H/L | [provider]` (pair suffix varies by provider) |

Symbol: first whitespace token, `strip_ctrl(..., 16)` (control-strip + 16-char
cap) before use - the input side is well bounded.

## Provider matrix

All calls via `modules.base - fetch_json()` (256 KB cap), timeout 10 s, key in
query string:

| Function | Endpoint | Notes |
|---|---|---|
| `_finnhub_quote()` | `finnhub.io/api/v1/quote` | Change computed locally: `chg = c - pc`, `pct = chg/pc*100` (guarded against `pc == 0`); `c == 0` raises `ValueError("no data")` |
| `_finnhub_crypto()` | same endpoint, symbol `BINANCE:<SYM>USDT` | quotes the Binance USDT pair, not USD |
| `_alphavantage_quote()` | `alphavantage.co/query` `GLOBAL_QUOTE` | change/pct parsed from the response's numbered fields (`09. change`, `10. change percent` with `%` stripped) |
| `_alphavantage_crypto()` | `CURRENCY_EXCHANGE_RATE` to USD | price + bid/ask only, no change |
| `_twelvedata_quote()` | `api.twelvedata.com/quote` | a `code` field in the body signals an API error -> `ValueError(message)` |
| `_twelvedata_crypto()` | same, symbol `<SYM>/USD` | |

Every provider treats a zero price as "no data" and raises, which converts
provider-side rate-limit bodies (e.g. Alpha Vantage's 200-with-`Note`
payload) into failover triggers rather than garbage output.

## Failover (`_try_providers()`)

Iterates the `_STOCK_PROVIDERS` / `_CRYPTO_PROVIDERS` registry in order,
skipping providers without a configured key, returning the first success.
On per-provider exception it logs at debug and records `f"{name}: {e}"`.
End states:

- No keys at all: `no finance API keys configured - see [stocks] in config.ini`.
- All configured providers failed: `all providers failed for '<sym>'
  (<name>: <error>; ...)` - the collected exception texts are sent to the
  channel (see Findings: this leaks keys).

## Precision and rounding (money-adjacent)

Floats throughout, display only, but unlike crypto.py/fx.py two derived
values are computed locally on the Finnhub path (`chg`, `pct`); both render at
`%.2f`, well above float error for realistic magnitudes. Formatting:

- Prices `%.2f` (crypto variants add thousands commas). Sub-cent asset prices
  lose precision at 2 decimals - the keyless `.gecko` handles that class
  better (4 sig figs).
- `_fmt_change()`: green up-arrow for `change >= 0` (zero counts as up),
  red down-arrow otherwise, explicit `+` sign. Covered by
  `tests/test_stocks.py - test_fmt_change_positive/negative`.
- `_fmt_number()`: volume with K/M/B suffix at 2 decimals. Covered by
  `tests/test_stocks.py - test_fmt_number_*`.

## Configuration

- Keys: `cred(cfg, "<provider>_key", "stocks", "<provider>_key")` for
  `finnhub_key`, `alphavantage_key`, `twelvedata_key` - env overrides
  `INTERNETS_FINNHUB_KEY`, `INTERNETS_ALPHAVANTAGE_KEY`,
  `INTERNETS_TWELVEDATA_KEY`; then secret_store; then legacy `[stocks]`.
  Only present keys enter `self._keys`; active providers logged at load.
- `is_configured()`: any one key suffices.

## Failure behavior

Per-provider exceptions are the failover mechanism (nothing retries within a
provider). Rate limit check after argument validation. Empty-arg replies show
combined stock+crypto usage.

## Security notes

Input capped and control-stripped; HTTP via `fetch_json` only. Output-side
key exposure is the finding below.

## Findings

- defect | stocks.py - `_try_providers()` | the all-failed reply interpolates
  raw exception text into the channel message, and `requests` exception
  strings include the full request URL with the query string - which carries
  `token=`/`apikey=` for every provider here. A transport error, 401, or 429
  raised by `fetch_json - raise_for_status()` therefore publishes the
  configured API key(s) to IRC. The debug log line has the same content
  (lower exposure). Needs exception-text scrubbing or a generic per-provider
  message.
- questionable | stocks.py - `_finnhub_crypto()` | quotes the Binance USDT
  pair while both Alpha Vantage and Twelve Data quote USD, so `.crypto BTC`
  output silently changes pair semantics depending on which provider
  succeeded.
- test-gap | stocks.py - `_try_providers()` | tests cover only the two pure
  formatters; failover order, key skipping, the no-keys message, and the
  error-aggregation path (including the key-leak defect above) are untested.
