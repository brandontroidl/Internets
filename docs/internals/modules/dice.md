# dice.py - dice roller (XdN+M notation)

Pure-local module (59 lines): parses a dice expression, rolls with
`random.SystemRandom` (Bandit B311 hygiene, per the module comment), and
reports total, range position, and individual rolls. Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.d` | `.d [X]dN[+/-M]` e.g. `.d 3d6+2` | `:: Total T/max [P%] :: Rolls [...] ::` |

## Parser and bounds

`_roll()` lowercases, strips all spaces, and matches
`^(?:(\d+)d)?(\d+)([+-]\d+)?$` (`_DICE_RE`), so `6`, `3d6`, and `3d6+2` are
all valid. Bounds enforced after parsing:

- count: 1 to 100 (default 1 when the `Xd` group is absent)
- sides: 2 to 10000
- modifier: unbounded in the regex (`[+-]\d+`); magnitude is limited only by
  the IRC line length of the incoming command, and the reply is truncated by
  the sender's 512-byte backstop, so this is not a practical DoS surface

The percentage is the total's position in the attainable range
`(total - min) / (max - min)`; the `max(maximum - minimum, 1)` guard is
defensively unreachable (sides >= 2 makes the range >= count >= 1). Rolls are
listed verbatim up to 20 dice; above that only the first 10 are shown with a
`... (N dice)` suffix.

## Integration / Configuration

No network, no keys, no state. Base-default `is_configured()`.

## Failure behavior

Non-matching input returns `invalid format - use: N  XdN  XdN+M`;
out-of-bounds count/sides return targeted messages. No exceptions escape.
Empty arg gets a prefix-aware usage hint from `cmd_dice`.

## Security notes

Output is bot-computed from validated integers; no `strip_ctrl` needed. No
user text is echoed. Note `cmd_dice` has no `bot.rate_limited` gate (see
Findings).

## Tests

`_roll()` is directly tested: format variants, invalid input, both bound
messages, and the >20-dice display truncation (tests/run_tests.py:431-463,
2477-2485). `DiceModule` is also in the async-handler contract test
(tests/run_tests.py:1476).

## Findings

- questionable | dice.py - `DiceModule.cmd_dice()` | No per-nick
  `bot.rate_limited()` gate, unlike the other novelty modules; local-only,
  so cost is just send-queue pressure.
- questionable | dice.py - `_roll()` | The bounds error strings contain a
  Unicode en-dash (U+2013, "1<en-dash>100"), inconsistent with the ASCII
  punctuation used everywhere else in the bot's output; the tests at
  tests/run_tests.py:457-458 pin the en-dash, so changing it means changing
  both.
