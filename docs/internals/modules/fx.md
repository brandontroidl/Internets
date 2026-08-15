# fx.py - foreign-exchange conversion via frankfurter.dev (ECB rates)

Keyless FX lookup against frankfurter.dev, which republishes ECB reference
rates. One class `FxModule` on the shared [base](base.md) contract; blocking
helper `_fetch_sync()` via `asyncio.to_thread`.

## Commands

| Command | Handler | Usage | Reply |
|---|---|---|---|
| `.fx <from> <to> [amount]` | `fx.py - FxModule.cmd_fx()` | `.fx usd eur 250` | `250.00 USD = 230.38 EUR  (frankfurter.dev - ECB rates)` |

Any arity/validation failure replies with the bare usage string
`fx <from> <to> [amount]`.

## Integration

- Endpoint: `GET https://api.frankfurter.dev/v1/latest?base=<FROM>&symbols=<TO>`
  (`fx.py - _fetch_sync()`), timeout 8 s (tightest in the batch), inline
  `requests.get(stream=True)` with a 16 KB cap - the response is one small
  JSON object (shape quoted in the module docstring). Sanctioned inline form.
- A 404 (frankfurter's response to an unsupported base) and a response whose
  `rates` lacks the target both map to `unknown currency code`.
- Privacy: sends only the two currency codes. The amount never leaves the
  host - conversion is `rate * amount` locally.

## Input validation (`FxModule.cmd_fx()`)

- Exactly 2 or 3 tokens.
- Both codes must match `_CCY_RE = ^[A-Za-z]{3}$` before uppercasing - this is
  the injection guard for what reaches the query string.
- Amount: `float()` parse, then `not (amount > 0) or amount > _MAX_AMOUNT`
  rejects. The `not (amount > 0)` form is deliberate NaN handling (NaN
  comparisons are False, so NaN fails the guard); `float("inf")` exceeds
  `_MAX_AMOUNT = 1e12` and is rejected; negatives and zero rejected.
- Rate limit check runs before parsing (same inversion as crypto.py).

## Precision and rounding (money-adjacent)

- The conversion is a single binary float multiply of the ECB reference rate
  by the user amount - display only, nothing is stored or accumulated, so
  float error (sub-ulp, far below the displayed precision) is acceptable
  here; this module makes no integer-minor-units claim.
- `_fmt_amount()`: absolute value >= 1 -> 2 decimals with thousands commas;
  below 1 -> 4 significant digits (`%.4g`), so tiny cross rates (e.g. IDR per
  USD inverted) stay meaningful.
- Rates are ECB daily reference rates, not live market quotes - the reply
  labels the source explicitly.

## Configuration

No key; `is_configured()` True. Shared UA credential.

## Failure behavior

`requests.RequestException` -> `fx API unavailable`; oversize -> `fx response
too large` (also logged); JSON error -> `fx parse error`; missing/unparsable
rate -> `unknown currency code` / `fx parse error`. Defensive `isinstance`
checks on the response shape before indexing.

## Security notes

Strict 3-letter code regex ahead of the query string; 16 KB cap before parse;
reply passed through `_strip_ctrl()` (belt-and-braces - every interpolated
value is already numeric or a validated code).

## Findings

None. (The rate-limit-before-usage inversion is shared with crypto.py and
recorded there.)
