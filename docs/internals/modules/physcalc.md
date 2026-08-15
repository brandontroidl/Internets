# physcalc.py - physics and engineering calculators (.ly .sr .escape .ohm .rc .baud)

## Purpose

Six pure-stdlib physics/EE calculators: light travel time, special relativity,
escape velocity, Ohm's law, resistor color codes, serial transfer time. No
network, no key, no state. Logic in module-level pure functions returning
`str`; `cmd_*` wrappers gate and reply. Base contract: [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `ly` | `.ly <distance>` (`4.2 ly` / `1 au` / `384400 km` / `8 min`) | distance in ly/au/km plus light travel time (or distance from a light-time) |
| `sr` | `.sr <v>` (fraction of c, `0.99` or `0.99c`) | `v = 0.99c (...) :: gamma 7.08 :: time dilation x... :: length contraction x...` |
| `escape` | `.escape <body\|mass radius>` | `earth: escape velocity 11.1 km/s (...) :: surface gravity 9.81 m/s^2 (1 g)` |
| `ohm` | `.ohm <two of V,I,R,P>` (`V=12 R=4`) | `V 12 V :: I 3 A :: R 4 ohm :: P 36 W` |
| `rc` | `.rc <bands\|ohms>` (`red red brown gold` / `4700` / `4.7k`) | bands -> ohms + tolerance, or value -> 4-band colors |
| `baud` | `.baud <bytes> <bps> [-fmt 8N1]` | `1,024 bytes @ 9,600 bps (8N1, 10 bits/byte) = 1.06667 s (10,240 bits)` |

`is_configured()` returns `True`; keyless, always loaded. `_MAX_INPUT = 120`
chars per argument. Everything runs synchronously on the event loop (all paths
are O(1)).

## Formulas and data sources

- Constants (physcalc.py:19-24): `c = 299792458 m/s` (exact SI),
  `G = 6.67430e-11` (CODATA 2018), `1 au = 1.495978707e11 m` (IAU 2012 exact),
  `1 ly = 9.4607304725808e15 m` (exactly c x Julian year 31,557,600 s).
- `physcalc.py - _ly()`: distance -> light time `t = d/c`; time -> distance
  `d = t*c`. Unit tables `_LY_DIST_UNITS` / `_LY_TIME_UNITS`.
- `physcalc.py - _sr()`: Lorentz factor `gamma = 1/sqrt(1 - beta^2)`; prints
  time dilation (x gamma) and length contraction (x 1/gamma). Rejects
  `beta >= 1` with a worded message for exactly `c`.
- `physcalc.py - _escape()`: `v_esc = sqrt(2GM/r)`, `g = GM/r^2`; g also shown
  in units of standard gravity 9.80665. `_BODIES` carries mass/radius for the
  Sun, the 8 planets, Moon, Pluto, Ceres - values match standard published
  figures (spot-checked: Earth 5.97219e24 kg / 6.371e6 m; results match the
  known 11.2 km/s within rounding, test: `test_physcalc.py - TestEscape`).
- `physcalc.py - _ohm()`: regex-extracts `v/i/r/p = number` pairs, requires
  exactly two, solves the remaining two per pair (`P = VI`, `V = IR`,
  `I = sqrt(P/R)`, etc.) with explicit zero-divisor messages.
- `physcalc.py - _rc()`: bidirectional resistor color code.
  Value -> bands (`_rc_from_value()`): normalize to two significant digits x
  10^exp, map digits and multiplier through `_RC_DIGIT_REV`/`_RC_MULT_REV`,
  always emit gold (5%) tolerance, and show the re-encoded value so rounding is
  visible. Bands -> value (`_rc_from_bands()`): pop an optional tolerance band,
  then treat the rest as digit bands + one multiplier
  (`sig x 10^mult`). Tables follow IEC 60062 (gold/silver = x0.1/x0.01 and
  5%/10%). The tolerance-pop logic is defective for 5-band codes - see
  Findings.
- `physcalc.py - _baud()`: bits/byte from framing `(\d)([noems])(stop)` =
  1 start + data + parity(0 if N) + stop, fractional stop bits rounded up;
  `seconds = bytes * bits_per_byte / bps`.
- `physcalc.py - _fmt()` / `_fmt_time()`: compact 6-sig-fig formatting; time
  unit ladder us/ms/s/min/hr/days/yr.

## Failure behavior

All bad input returns usage/diagnostic strings; echoed fragments are
`strip_ctrl()`-capped (16 chars for color names). Every reply passes through
`strip_ctrl()` in the wrappers. No exceptions expected to escape.

## Security notes

No network, filesystem, secrets, subprocess, or state. All computation O(1);
the 120-char cap bounds parsing. Nothing further.

## Findings

- defect | physcalc.py - _rc_from_bands() | 5-band color codes are computed
  wrongly: the first tolerance-pop condition (`bands[-1] in _RC_TOL and
  bands[-1] not in _RC_MULT`) is always False because every `_RC_TOL` color is
  also a `_RC_MULT` key, and the gold/silver branch only pops when
  `len(bands) == 4`. A 5-band input therefore keeps its tolerance band as the
  multiplier and folds 4 digits: `brown black black red brown` returns
  `10.02k ohm` (correct: 10 kohm +/-1%) and `red red black brown gold` returns
  `220.1 ohm +/-5%` (correct: 2.2 kohm +/-5%). Verified by direct call.
  4-band and 3-band inputs are unaffected.
- questionable | tests/test_physcalc.py - TestRc.test_five_band | The test
  asserts the defective value (`10.02k ohm`), locking the bug in - a
  change-detector test that restates the implementation instead of the
  resistor-code contract.
- questionable | physcalc.py - _fmt_time() | The year conversion divides by
  3.155815e7 s (sidereal year) while `_LY_M` is defined via the Julian year
  (31,557,600 s), so `.ly 4.2 ly` reports its own light-travel time as
  `4.19993 yr` instead of `4.2 yr`. Verified by direct call. `encode.py -
  _human_time()` uses the Julian value; this file disagrees with both.
- questionable | physcalc.py - _ohm() | The R,P pair with `R=0` returns the
  physically inconsistent `V 0 V :: I 0 A :: R 0 ohm :: P 36 W` (I is forced
  to 0 when R is falsy) instead of rejecting R=0 like the other pairs do.
  Verified by direct call.
