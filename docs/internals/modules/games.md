# games.py - small chance/choice commands

Pure-local module (143 lines): four dice-adjacent parlor commands, no network,
no state. All picks use a module-level `random.SystemRandom` (`_RNG`; Bandit
B311 hygiene, per the same rationale as bofh.py/dice.py). Base contract in
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.coin` | `.coin` | `Heads` or `Tails` |
| `.8ball` | `.8ball <question>` | `<nick>: <one of 20 canonical answers>` |
| `.rps` | `.rps <rock\|paper\|scissors>` | `you: X, bot: Y - <tie\|you win\|you lose>` |
| `.choose` | `.choose A, B, C, ...` | `<nick>: <picked option>` |

Every handler checks `bot.rate_limited(nick)` first (notice on refusal), then
validates args, replying with a prefix-aware usage hint on bad input.

## Command logic

- `cmd_8ball`: requires a non-blank question (content ignored); answers come
  from the fixed 20-entry `_8BALL_ANSWERS` tuple (the canonical Magic 8-Ball
  set: 10 affirmative, 5 non-committal, 5 negative).
- `cmd_rps`: lowercased choice must be a `_RPS_BEATS` key; the bot picks
  uniformly, outcome decided by the `_RPS_BEATS[choice] == bot_pick` beats
  map (rock beats scissors, paper beats rock, scissors beats paper).
- `cmd_choose`: requires a comma in the arg; splits on commas, strips and
  drops empties, then bounds the request: at least 2 options, at most 20,
  each at most 60 chars. Violations get a targeted error, not a truncated
  answer.

## Integration / Configuration

No network, no keys, no persistence. `is_configured()` returns True
explicitly (same as the base default).

## Failure behavior

None beyond validation replies; nothing can raise past the argument checks.

## Security notes

The only user text echoed back is the picked `.choose` option and the nick
prefix on 8ball/choose replies; both routes pass through `strip_ctrl`
(400-char cap), so control-code injection via options is neutralized.
`.choose` bounds (20 x 60 chars) keep the reply within one IRC line.

## Findings

- questionable | games.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py; also the one copy in the batch with no type annotations.
- test-gap | games.py - `GamesModule` | No tests; notably `cmd_choose`
  bounds and the rps outcome map are unverified (games.py is also absent
  from the async-handler contract test at tests/run_tests.py:1471).
