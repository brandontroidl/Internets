# qr.py - QR-code link generator (no network)

Local URL builder (58 lines): turns `.qr <text>` into a clickable
api.qrserver.com image URL. The bot performs no HTTP at all - the URL is
constructed and sent as text. Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.qr` | `.qr <text>` (max 1000 chars) | `https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=<encoded>` |

## Behavior

After the `bot.rate_limited(nick)` gate, the arg is stripped and bounded to
1000 chars (`_MAX_INPUT`); empty or oversize input gets the usage hint. The
text is percent-encoded with `urllib.parse.quote(text, safe='')` - every
non-unreserved byte escaped, so the payload cannot break out of the `data=`
query parameter - and spliced into the fixed goqr.me API URL, which is then
`strip_ctrl`-sanitized and sent as one `privmsg`.

## Integration / Configuration

No outbound HTTP, no keys, no state. `is_configured()` returns True
explicitly (same as the base default).

## Failure behavior

None beyond the usage hint; URL construction cannot fail.

## Security notes

The bot never fetches the URL, so there is no SSRF surface; the fixed host
plus full percent-encoding means user text cannot alter the URL structure.
Privacy: the encoded text becomes part of a third-party URL - whoever clicks
it sends the content to qrserver.com. `strip_ctrl` on the output is
defense-in-depth (encoding has already removed control bytes) but is also the
source of the truncation defect below.

## Findings

- defect | qr.py - `QRModule.cmd_qr()` | The advertised 1000-char input cap
  exceeds what the output path can carry: the final URL passes through
  `base.strip_ctrl` with its default 400-char cap, so any input whose
  encoded form pushes the URL past 400 chars (roughly >338 plain-ASCII
  chars, or ~112 chars of mostly-escaped text at 3x expansion after the
  62-char URL prefix) is silently truncated into a broken link that renders
  the wrong QR code. Either lower `_MAX_INPUT` or raise the `max_len`
  passed to `strip_ctrl` (the sender's 512-byte line limit is the real
  ceiling).
- questionable | qr.py - `_strip_ctrl()` | Same no-op `base.strip_ctrl`
  alias as fact.py.
- test-gap | qr.py - `QRModule.cmd_qr()` | No tests; the encoding and the
  cap mismatch above are unverified.
