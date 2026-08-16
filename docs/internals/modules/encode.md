# encode.py - offline encoding, hashing and generator utilities

## Purpose

Twelve pure-stdlib text/codec commands: Unicode inspection, digests, checksums,
Base32, slugs, ULIDs, ASCII lookup, data-size conversion, URL defanging,
password entropy estimation, password/passphrase generation, lorem ipsum. No
network, no key, no state. Logic lives in module-level `_*` functions (one `str`
out) so it unit-tests without a bot; `cmd_*` wrappers only gate, arg-check and
reply. Base contract: [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `unicode` | `.unicode <char\|U+XXXX\|name>` | `U+0041 'A' :: LATIN CAPITAL LETTER A :: cat Lu :: UTF-8 41 :: Basic Latin` |
| `hash` | `.hash [algo] <text>` | `sha256: <hex>` (algo one of md5/sha1/sha256/sha512/blake2b; default sha256) |
| `crc` | `.crc <text>` | `CRC32 3610a686 :: Adler-32 062c0215` |
| `b32` | `.b32 <text>` | Base32 encode, or decode when input is valid base32 (auto) |
| `slug` | `.slug <text>` | `creme-brulee` |
| `ulid` | `.ulid` | 26-char Crockford-base32 ULID |
| `ascii` | `.ascii [dec\|hex\|char]` | `dec 65 :: hex 41 :: oct 101 :: char 'A'` |
| `ds` | `.ds <value> <unit>` | bytes total plus decimal (KB..TB) and binary (KiB..TiB) breakdowns |
| `defang` | `.defang <url\|ip\|email>` | `defanged: hxxps[:]//evil[.]com` or `refanged: ...` (auto) |
| `entropy` | `.entropy <password>` | `len 12 :: pool 95 :: ~78.8 bits :: strong :: ~<time> at 10B/s` |
| `pw` | `.pw [len] [-s]` | random password (8..64 chars) or `-s` word passphrase |
| `lorem` | `.lorem [words]` | 1..60 words of lorem ipsum |

`is_configured()` returns `True`; keyless, always loaded.

## Input caps and transforms

`_MAX_INPUT = 400` chars applied to every arg in the `cmd_*` wrappers. Per
transform:

- `_unicode()`: resolution order is single char -> `U+`/`0x` hex -> bare hex
  (up to 6 hex digits) -> official Unicode name via `unicodedata.lookup`.
  Codepoint range-checked to U+10FFFF; surrogates render `(unencodable
  surrogate)` for the UTF-8 column; control/space categories display `·`
  instead of the raw glyph. Block name from a curated `_BLOCKS` table because
  `unicodedata` has no block API before 3.14 (in-code comment).
- `_hash()`: first token selects the algorithm if it is in the fixed
  `_HASH_ALGOS` whitelist; otherwise the whole string is sha256'd. Digest of
  UTF-8 bytes, hex output.
- `_b32()`: decodes only when the uppercased input matches `[A-Z2-7]+=*` AND
  length is a multiple of 8; otherwise encodes. The UTF-8 decode is deliberately
  outside the b32decode `try` because `UnicodeDecodeError` subclasses
  `ValueError` and would otherwise silently fall through to re-encoding
  (in-code comment).
- `_slug()`: NFKD normalize, drop non-ASCII, lowercase, collapse runs of
  non-alphanumerics to `-`.
- `_ulid()`: 48-bit millisecond timestamp + 80 bits from `secrets.randbits`,
  Crockford base32 - spec-shaped ULID (26 chars, sortable prefix).
- `_ascii()`: accepts a single char (< 256), `0x` hex, decimal, or bare 1-2
  digit hex; 0-255 only; control codes get their names from `_CTRL_NAMES`.
- `_ds()`: unit table maps decimal (KB=1000^n) and binary (KiB=1024^n)
  separately; accepts attached (`1.5GB`) or spaced form; prints both ladders.
- `_defang()`: direction auto-detected by `_is_defanged()` (presence of `hxxp`,
  `[.]`, etc.). Pure string replacement in a safe order (`https` before
  `http`); round-trips (test: `test_encode.py - TestDefang.test_roundtrip`).
- `_entropy()`: pool-size model (26+26+10+33 by character classes present),
  `bits = len * log2(pool)`, five labels, crack time at 1e10 guesses/s for half
  the keyspace. A naive charset model by design - it does not detect dictionary
  words or patterns. The reply does not echo the password itself.
- `_pw()`: `secrets.choice` over a 70-char alphabet, length clamped 8..64
  (default 16). `-s` builds a hyphenated passphrase of 3..10 words from the
  40-word `_DICE_WORDS` list, capitalizes one word, appends a 0..99 digit.
- `_lorem()`: repeats the canonical 69-word passage, 1..60 words.

## Failure behavior

All bad input returns usage/diagnostic strings; no exceptions escape in normal
use. Echoed fragments are `strip_ctrl()`-capped short. Every reply passes
through `strip_ctrl()` in its wrapper.

## Security notes

No network, no filesystem, no secrets, no persistence. `secrets` (CSPRNG) backs
`.pw` and `.ulid`. `.hash` intentionally offers md5/sha1 - it is a codec
utility, not an auth path. Passwords submitted to `.entropy` transit IRC in the
command itself (inherent to the medium; the bot adds no storage or logging of
its own here). CPU is trivially bounded by the 400-char cap everywhere.

Oddity: every argful wrapper treats a bare `!` argument as "no argument"
(`if not arg or arg.strip() == "!"`), a guard unique to this module. Consequence
for `.unicode`: the character `!` (U+0021) cannot be looked up. See Findings.

## Findings

- questionable | encode.py - EncodeModule.cmd_unicode() | The module-wide
  `arg.strip() == "!"` sentinel makes `.unicode !` print usage instead of
  describing U+0021; no other module uses this guard and no comment explains
  it.
- questionable | encode.py - _hash() | The `except (ValueError, TypeError)` /
  "unknown algo" branch is unreachable: `algo` is always a `_HASH_ALGOS` key by
  the time `hashlib.new` runs (dead error path).
- questionable | encode.py - _pw() | The `-s` passphrase draws from a 40-word
  list: 5 words + digit is about 33 bits total, which this module's own
  `.entropy` scale labels "weak"; a Diceware-size list (7776 words) would give
  about 64 bits at the same length. The character-mode default (16 chars, about
  98 bits) is fine.
- questionable | encode.py - _b32() | Auto-decode of alphabet-valid input can
  yield pure control bytes that are valid UTF-8 (e.g. `.b32 AAAAAAAA` decodes
  to five NULs), which `strip_ctrl()` then erases, sending an empty reply
  instead of either output or an error. Verified by direct call.
- test-gap | tests/test_encode.py | No test covers the `!`-sentinel behavior,
  the surrogate branch of `_unicode()`, or the control-byte b32 decode edge.
