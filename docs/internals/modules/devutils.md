# devutils.py - local text codecs and time helpers (.b64 .unb64 .hex .morse .uuid .epoch)

## Purpose

Six small pure-local utilities: base64 encode/decode, hex auto-codec, morse
auto-codec, random UUIDv4, and epoch/ISO-8601 conversion. No network, no key,
no subprocess, no filesystem. Base contract: [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `b64` | `.b64 <text>` | base64 of UTF-8 input |
| `unb64` | `.unb64 <text>` | decoded text, `invalid base64`, or `binary or invalid utf-8` |
| `hex` | `.hex <text>` | auto: even-length hex-only input decodes, anything else encodes |
| `morse` | `.morse <text>` | auto: input of only `.` `-` `/` and spaces decodes, else encodes (`/` = word break) |
| `uuid` | `.uuid` | random UUIDv4 (`uuid.uuid4()`, i.e. `os.urandom`-backed) |
| `epoch` | `.epoch [arg]` | no arg = current epoch; numeric = epoch -> ISO 8601 UTC; ISO string = -> epoch |

`is_configured()` returns `True`; keyless, always loaded. All commands
rate-limited via `bot.rate_limited()`.

## Behavior notes

- Caps: `_MAX_INPUT = 400` chars on every argument.
- `devutils.py - cmd_unb64()`: `base64.b64decode(..., validate=True)` rejects
  non-alphabet characters instead of silently skipping them; a decode that is
  not valid UTF-8 reports `binary or invalid utf-8` rather than emitting raw
  bytes.
- `devutils.py - cmd_hex()`: decode path requires `_HEX_RE` match AND even
  length; the encode path uses the original (untrimmed) `arg`, so
  leading/trailing spaces are preserved in an encode but stripped before the
  decode check.
- `devutils.py - _morse_encode()` uppercases and silently drops characters
  outside `_MORSE_MAP`; `_morse_decode()` maps unknown codes to `""`. A result
  that sanitizes to nothing replies `no output`.
- `devutils.py - cmd_epoch()`: numeric detection via
  `re.fullmatch(r"-?\d+(\.\d+)?")`; out-of-range timestamps surface as
  `invalid epoch` (`OSError`/`OverflowError` caught). ISO input accepts a
  trailing `Z` (rewritten to `+00:00` for `fromisoformat` compatibility with
  pre-3.11 semantics); naive datetimes are assumed UTC.

## Failure behavior

All failures return short fixed strings; nothing raises out of the handlers in
normal use. Every reply routes through `strip_ctrl()` (via the private
`_strip_ctrl` alias) so decoded output - which is attacker-chosen bytes in the
decode commands - cannot inject IRC control codes. That sanitizer is the
security-relevant line in this file: `.unb64`/`.hex` decode arbitrary
user-supplied payloads and the C0-stripping is what keeps a crafted payload
from emitting color/CTCP/ESC sequences as the bot.

## Security notes

No network, secrets, persistence, subprocess, or filesystem access. UUIDs come
from `uuid.uuid4()` (CSPRNG-backed). CPU trivially bounded by the 400-char cap.

## Findings

- test-gap | modules/devutils.py | No test file exists for this module at all
  (`tests/` has no `test_devutils.py`; `test_dnsutils.py` is a different
  module). Every codec path, including the security-relevant decode ->
  strip_ctrl behavior and the epoch edge cases, is untested.
- questionable | devutils.py - _strip_ctrl() | Private wrapper that only
  forwards to `base.strip_ctrl` with identical defaults - indirection with no
  behavior, kept presumably for symmetry with other modules.
- questionable | devutils.py - cmd_hex() | Auto-detection makes even-length
  hex-looking words undecodable-as-intent: `.hex cafe` tries to DECODE (yielding
  `binary or invalid utf-8`) rather than encoding the word "cafe"; there is no
  way to force the encode direction.
