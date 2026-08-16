# mathx.py - offline math toolbox (.isprime .factor .gcd .base .stats .roman .pct .bignum .const)

## Purpose

Nine pure-compute math commands, stdlib only, no network, no key. Each command's
logic lives in a module-level function returning one `str` (unit-testable without
a bot); the `cmd_*` methods only gate, arg-check and reply. Base contract:
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `isprime` | `.isprime <n>` | `7 is prime :: next prime 11` or `... composite :: smallest factor F :: next prime P` |
| `factor` | `.factor <n>` | `5040 = 2^4 x 3^2 x 5 x 7` |
| `gcd` | `.gcd <a> <b> [..]` | `gcd = 6 :: lcm = 36` |
| `base` | `.base <n> <from> <to>` | `255 (base 10) = ff (base 16)` |
| `stats` | `.stats <n1 n2 ...>` | `n=5 :: mean=3 :: median=3 :: stdev=... :: min :: max :: sum` |
| `roman` | `.roman <n\|numeral>` | `2024 = MMXXIV` / `MMXXIV = 2024` (auto-detect) |
| `pct` | `.pct <expr>` | `20% of 150 = 30` / `50 to 75 = 50% increase` / `30 is 25% of 120` |
| `bignum` | `.bignum <expr>` | `5! = 120` or `100! = 158 digits :: starts ... ends ...` |
| `const` | `.const <name>` | `speed of light = 2.99792e+08 m/s` |

No aliases. `is_configured()` returns `True` (always loaded, keyless).

## Integration / configuration / state

None. No HTTP, no secrets, no persistence, no cache. `random` is used only
inside Pollard rho (nosec-annotated: factorization randomness, not crypto).

## Input caps (DoS bounds)

| Cap | Value | Guards |
|---|---|---|
| `_MAX_INPUT` | 200 chars | every command arg |
| `_MAX_ISPRIME_DIGITS` | 100 | `.isprime` operand |
| `_MAX_FACTOR_DIGITS` | 19 | `.factor` operand (keeps Pollard rho fast) |
| `_MAX_STATS_NUMS` | 1000 | `.stats` and `.gcd` token count |
| `.bignum` | `n! <= 100000`, `fib <= 500000`, power result estimated `<= ~1M digits` via `b*log10(a)` | `_bignum()` |

## Concurrency

Only `cmd_bignum()` offloads to `asyncio.to_thread` (in-code comment: big-int
math up to about 1M digits is heavy CPU; a user must not be able to freeze the
bot). Every other command, including `.isprime` and `.factor`, runs
synchronously on the event loop. For `.factor` the 19-digit cap keeps that to
milliseconds; for `.isprime` it does not (see Findings).

## Algorithms

- `mathx.py - _is_probable_prime()`: Miller-Rabin with the fixed witness set
  {2..37}, proven deterministic for n < 3.3e24; beyond that (inputs up to 100
  digits) it acts as a strong probable-prime test with negligible error, as the
  docstring states.
- `mathx.py - _smallest_factor()`: trial division by 2 then odd i up to
  `1 << 20`, falling back to `_pollard_rho()`.
- `mathx.py - _pollard_rho()`: classic Floyd-cycle rho with random `x`, `c`;
  retries with new randomness when `d == n`. Unbounded loop by design - safe
  for the 19-digit `.factor` cap, not for 100-digit `.isprime` composites
  (Findings).
- `mathx.py - _prime_factors()`: peel witnesses as small primes, then a stack of
  cofactors split by rho until every element passes Miller-Rabin.
- `mathx.py - _fib()`: fast-doubling recursion (depth log2 n, about 19 frames at
  the 500000 cap). The comment says "iterative"; the implementation is
  recursive (harmless, see Findings).
- `mathx.py - _roman()` validates numerals by round-trip: `_from_roman()` is a
  permissive right-to-left accumulator, then `_to_roman(n) != s.upper()` rejects
  malformed forms like `IIII` (test: `test_mathx.py - TestRoman.test_malformed_numeral`).
- `mathx.py - _bignum_report()` temporarily raises
  `sys.set_int_max_str_digits(2_000_000)` around `str(value)` (Python's default
  4300-digit int-to-str DoS guard would reject the intentionally huge results),
  restoring the previous value in a `finally`. The limit is process-global, not
  thread-local (Findings).
- `mathx.py - _const()`: 16 CODATA-2018/SI-exact physical constants with a
  friendly alias table. Note `g` is big G (gravitational constant); standard
  gravity is `g_n` / `gravity`. Keys are lowercased, so `.const G` and
  `.const g` are the same lookup.

## Failure behavior

Every parse/range failure returns a usage or diagnostic string; no exceptions
are expected to escape the pure functions in normal use. Rejected tokens echoed
back are wrapped in `strip_ctrl()` with short caps (e.g. `_gcd()` echoes at most
20 chars). All replies pass through `strip_ctrl()` in the `cmd_*` wrappers.

## Security notes

No network, no filesystem, no secrets. Attack surface is CPU consumption only;
bounds above, one gap in Findings. Integer parsing uses `re.fullmatch(r"\d+")`
before `int()`, so no surprise bases or underscores.

## Implementation walk (non-obvious blocks only)

- `_fmt_int()` comma-groups only when the rendering stays at 30 chars or less -
  keeps one-line IRC output tidy.
- `_isprime()` strips a leading `+` then demands pure digits; negative input is
  a usage error rather than "not prime".
- `_gcd()` takes `abs()` of every operand and computes `math.lcm(*nums)` with a
  `ValueError` fallback to 0 (lcm raises on a 0 operand in some forms).
- `_base()` hand-rolls the digit loop for output (Python has no int-to-base-n),
  handles sign separately, accepts bases 2..36.
- `_stats()` uses `statistics.fmean/median/stdev`, `stdev=0.0` for n=1.
- `_pct()` tries three anchored regex forms in order; division-by-zero cases
  return worded messages ("percent change from 0 is undefined").
- `MathxModule._gate()` centralizes the per-nick rate-limit + notice used by all
  nine handlers.

## Findings

- defect | mathx.py - MathxModule.cmd_isprime() | Event-loop freeze DoS:
  `.isprime` accepts up to 100 digits and runs synchronously on the event loop;
  for a composite with no factor below 2^20 (e.g. a 100-digit RSA-style
  semiprime) `_isprime()` calls `_smallest_factor()` which falls through to
  `_pollard_rho(n)`, whose expected time for 50-digit factors is astronomically
  large and whose loop is unbounded - the bot hangs. Even the preceding trial
  division (524k big-int mods) is a noticeable on-loop stall. `.factor` avoids
  this with its 19-digit cap and `.bignum` with `to_thread`; `.isprime` has
  neither.
- questionable | mathx.py - _bignum_report() | `sys.set_int_max_str_digits` is
  process-wide: two concurrent `.bignum` calls race on set/restore (one thread
  can restore 4300 while the other is mid-`str()`, raising an uncaught
  ValueError out of the worker), and the raised 2M limit is briefly visible to
  every other thread.
- doc-drift | mathx.py - _fib() | Comment says "fast-doubling iterative"; the
  implementation is recursive fast-doubling.
- test-gap | tests/test_mathx.py | No test exercises `.isprime` with a
  trial-division-resistant composite (the defect path above); the existing
  large-composite test uses 7-digit factors that trial division finds
  instantly.
