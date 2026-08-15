# cowsay.py - pure-Python ASCII cowsay

Local renderer (97 lines): builds the classic speech-bubble-plus-cow figure with no
external `cowsay` binary or library. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.cowsay` | `.cowsay <text>` | multi-line ASCII figure, one `privmsg` per line |

## Rendering

- Input is truncated to 200 chars (`_MAX_INPUT`, applied before rendering).
- `_bubble()` expands tabs to 4 spaces, splits on `\n` (defensive only - an IRC
  command argument cannot contain a newline), and wraps each line at 40 columns
  via `textwrap.wrap` (`_WRAP`). Bubble borders and side glyphs follow the
  classic cowsay convention: `< >` for one line, `/ \` first, `\ /` last,
  `| |` in between, padded to the widest line.
- `_render()` appends the fixed 5-line `_COW` template.
- Worst case is roughly 13 output lines (200 chars at width 40 gives up to 5-6
  bubble lines, plus 2 borders and 5 cow lines). Each line goes out as its own
  `privmsg`; flood pacing is delegated to the bot's send queue, and the
  per-nick `bot.rate_limited()` gate (checked first) bounds invocation rate.

## Integration / Configuration

No network, no keys. `is_configured()` returns True explicitly (same as the
base default).

## Failure behavior

None beyond the usage hint on empty input. Rendering cannot fail: `_bubble`
guards every empty-list case (`or [""]`, `lines = [""]`).

## Security notes

User text is truncated then each rendered line passes through
`strip_ctrl` (400-char cap, far above any possible line width) before
`privmsg`, so embedded IRC control codes cannot ride into a bot-attributed
multi-line block. Leading-whitespace cow art survives because `strip_ctrl`
only removes C0/DEL bytes, not spaces.

## Findings

- questionable | cowsay.py - `_strip_ctrl()` | Module-local wrapper is a
  no-op alias of `base.strip_ctrl` (same 400 default); pure indirection,
  repeated across most modules in this batch.
- test-gap | cowsay.py - `_bubble()` | No tests; wrap/border geometry and the
  200-char truncation are unverified.
