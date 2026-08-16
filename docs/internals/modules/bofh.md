# bofh.py - BOFH excuse generator (local list)

Pure-local novelty module (141 lines): picks one line from an embedded excuse list.
No network, no config, no state. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.bofh` / `.excuse` | `.bofh` (no args) | `[BOFH] Your excuse: <excuse>` |

Any argument is ignored (`cmd_bofh` never reads `arg`).

## Integration

None. The excuse pool is the module-level `_EXCUSES` list (105 entries,
bofh.py:16-122). Selection uses a module-level `random.SystemRandom`
instance (`_rng`); the comment explains this is to keep Bandit B311 quiet
without per-line `# nosec`, not for cryptographic need.

## Configuration

None. Keyless; base-default `is_configured()` (module always visible in
`.help`). No `on_load()` override.

## Failure behavior

None possible at runtime: `_rng.choice` over a non-empty static list cannot
fail, and the reply is a single `privmsg`.

## Security notes

Output is entirely bot-authored static text, so the absence of `strip_ctrl`
is safe (nothing third-party or user-supplied is spliced in). No input is
parsed. Nothing leaves the machine.

## Findings

- questionable | bofh.py - `BofhModule.cmd_bofh()` | Unlike every other
  fun/novelty module in this batch, there is no `bot.rate_limited(nick)`
  gate; cheap locally, but spam lands on the shared send queue unmetered.
- questionable | bofh.py - `_EXCUSES` | The header comment claims the list
  is "sourced from the community-maintained canon", but most entries are
  original/modern additions (CI/CD, containers, crypto mining), and a few
  look like private in-jokes or typos ("langerie", "positstronic",
  "lusstrious"); the comment overstates provenance.
- test-gap | bofh.py - `BofhModule` | No tests reference this module.
