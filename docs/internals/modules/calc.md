# calc.py - safe arithmetic expression evaluator (.cc)

## Purpose

Evaluates user-typed math expressions (`.cc 2pi`, `.cc sqrt(144)`) entirely offline.
The whole file exists to do this WITHOUT `eval()`: expressions are parsed with
`ast.parse(mode="eval")` and walked by a strict whitelist interpreter
(`calc.py - _safe_eval()`), so arbitrary Python can never execute. Module contract
(commands dict, gating, help) is the shared [base](base.md) contract.

## Commands

| Command | Usage | Example | Reply shape |
|---|---|---|---|
| `cc` | `.cc <expression>` | `.cc 2pi` | `[calc] 2*pi = 6.2831853` |

No aliases. Keyless and always loaded: no `is_configured()` override, so the base
default (configured) applies.

## Integration / configuration

None. No network, no secrets, no state. `CalcModule.cmd_calc()` reads only
`bot.cfg["bot"]["command_prefix"]` for the usage line and `bot.rate_limited()` for
gating.

## Evaluation sandbox (security)

This is the load-bearing part of the file.

**Parse.** `_calc()` normalizes the string (see Implementation walk) and calls
`ast.parse(expr, mode="eval")`. Nothing is ever passed to `eval`/`exec`; the AST is
interpreted by `_safe_eval()`.

**Whitelist walker.** `_safe_eval()` accepts exactly these node types and rejects
everything else with `ValueError`:

| Node | Accepted subset |
|---|---|
| `ast.Expression` | unwrap |
| `ast.Constant` | only `int` / `float` values - string/bytes/None constants are rejected, so there is no object to pivot from |
| `ast.Name` | only names in `_CONSTS` (`pi`, `e`, `tau`, `inf`) |
| `ast.BinOp` | operators in `_BIN_OPS`: `+ - * / // % **` |
| `ast.UnaryOp` | `+x`, `-x` |
| `ast.Call` | plain-name calls into `_FUNCS` only, positional args only (`node.keywords` rejected) |

Blocked by construction: attribute access (`().__class__` is an unhandled
`ast.Attribute` -> error), subscripts, comprehensions, lambdas, boolean/compare
operators, bit operators (`<<`, `|`, ...), f-strings, walrus, starargs. Test
evidence: `tests/test_calc.py - TestCalcSafety` (`__import__`, `__class__`,
unknown names all error).

**Resource bounds** (big-number DoS):

- Recursion: `_MAX_DEPTH = 50` on the walker; deeper nesting raises before
  Python's own recursion limit is near.
- Input length: `cmd_calc()` truncates to 200 chars before parsing.
- `**`: BOTH operands are capped (`_safe_eval()` Pow branch). `abs(exponent) >
  10000` is rejected, and for int**int the estimated result size
  `left.bit_length() * right > 100_000` bits is rejected - the in-code comment
  records why: capping only the exponent still allowed `(10**300)**9999`-style
  base amplification. Nested pow chains compose safely: the inner result's
  bit_length feeds the outer check, so `(2**9999)**9999` is rejected at the
  outer level. `2**10000` (about 3 KB of digits) is the allowed ceiling per pow.
- `factorial`: replaced by `_safe_factorial()`, non-negative integers only,
  capped at 170 (the last factorial below float overflow territory; result about
  7.3e306). `math.factorial` itself is never exposed.
- Float overflow (`1e300**9999` via `math.pow` or `exp(10000)`) raises
  `OverflowError`, caught in `_calc()`.

**Known residual classes** (accepted, low impact):

- Multiplication is uncapped, but the 200-char input and the per-pow 100k-bit
  ceiling bound any product to a few megabits - milliseconds of CPU.
- Evaluation runs synchronously on the event loop (no `asyncio.to_thread`,
  unlike `mathx.cmd_bignum()`); with the bounds above the worst case is small,
  so this is a deliberate simplicity trade.
- `min`/`max`/`round` accept arbitrary arg counts; arg count is bounded by input
  length, and misuse raises `TypeError`, which is caught.

**Output safety.** The echoed expression is user input; `cmd_calc()` wraps the
entire reply in `strip_ctrl()` ([base](base.md)) so IRC control codes cannot be
reflected. `_calc()` additionally strips `\x01` (CTCP) before parsing.

## Failure behavior

`_calc()` converts all expected failures to strings: `ZeroDivisionError` ->
`"division by zero"`; `ValueError` / `TypeError` / `OverflowError` /
`SyntaxError` -> `"error: <message>"`. The exception text is either Python's own
parser message or a message this module wrote, and the reply passes through
`strip_ctrl()`, so nothing attacker-shaped reaches the channel unfiltered.
Nothing retries; there is nothing to retry.

## Functions

| Symbol | Role |
|---|---|
| `calc.py - _safe_factorial()` | bounded factorial (cap 170), installed into `_FUNCS` |
| `calc.py - _safe_eval()` | recursive whitelist AST interpreter (see above) |
| `calc.py - _calc()` | normalize -> parse -> eval -> format; all error handling |
| `calc.py - CalcModule.cmd_calc()` | rate-limit gate, usage line, 200-char cap, strip_ctrl reply |
| `calc.py - CalcModule.help_lines()` | one `help_row()` line |
| `calc.py - setup()` | standard module entry point |

## Implementation walk

- `_FUNCS` / `_CONSTS` (calc.py:13-26): the entire callable surface - `abs`,
  `round`, `min`, `max` builtins plus selected `math` functions, and four float
  constants. `cbrt` falls back to a sign-aware `x**(1/3)` lambda on Pythons
  without `math.cbrt` (compatibility shim).
- `_IMPLICIT_MUL` / `_DIGIT_NAMES` (calc.py:37-45): implicit multiplication.
  Two regexes insert `*` between a digit and a letter in either order (`2pi` ->
  `2*pi`). Function names that themselves contain digits (`log2`, `log10`,
  `atan2`) would be mangled by those regexes, so `_calc()` first swaps each such
  name (longest first) for a sentinel tag built from Unicode noncharacters
  (`﷐{i}﷐`), applies the regexes, then swaps the names back. The
  earlier `\x01` strip exists because CTCP bytes could otherwise collide with
  this placeholder logic (per the in-code comment).
- `_safe_eval()` (calc.py:60-99): the walker described above; depth incremented
  on every recursion.
- `_calc()` (calc.py:102-125): normalization, parse, eval, then formatting:
  integer-valued floats below 1e15 print as ints, other floats as `%.8g`,
  ints via `str()`.
- `CalcModule` (calc.py:128-148): gate -> usage -> truncate -> reply. Nothing
  else.

## Findings

- questionable | calc.py - _calc() | Scientific notation is silently
  reinterpreted as implicit multiplication with Euler's constant: `.cc 1e10`
  returns `27.182818` (`1*e*10`), not `10000000000`; the implicit-mul regexes
  fire before `ast.parse` ever sees the literal. Verified by direct call. No
  test covers scientific notation.
- test-gap | tests/test_calc.py | No test pins the nested-pow amplification
  guard (`(2**9999)**9999`) or the depth cap, the two most security-relevant
  bounds after the factorial/exponent caps that are tested.
