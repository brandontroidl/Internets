# devtools.py - developer lookup/convert tools (.jwt .semver .uuid5 .tz .unix .color .cron)

## Purpose

Seven pure-stdlib developer utilities: JWT claim decoding (no verification),
semver comparison, UUIDv5 generation / UUID inspection, timezone conversion,
signal/errno lookup, color conversion, cron validation with next-fire
prediction. No network, no key, no subprocess. Logic lives in module-level pure
functions returning `str`; `cmd_*` wrappers gate and reply. Base contract:
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `jwt` | `.jwt <token>` | `alg=HS256 :: typ=JWT :: sub=1234 :: exp=2023-11-14T23:13:20Z` |
| `semver` | `.semver <a> <b>` | `1.2.3 < 1.2.4` |
| `uuid5` | `.uuid5 <ns> <name>` (ns = dns/url/oid/x500 or a UUID); single UUID arg = inspect | `<uuid>` or `<uuid> :: version 4 :: variant RFC 4122` |
| `tz` | `.tz <time> <from> <to>` | `06:14 pst = 14:14 UTC (utc)` (bare times flag `+1d`/`-1d` rollover) |
| `unix` | `.unix <signal\|errno>` | `SIGKILL = signal 9` / `ENOENT = errno 2: No such file or directory` |
| `color` | `.color <value>` | `#ff8800 :: rgb(255,136,0) :: hsl(32,100%,50%) :: ~orange` |
| `cron` | `.cron <expr>` | `at 09:30, day-of-week 1-5 :: next 2026-01-02 09:30 UTC ; ...` |

`is_configured()` returns `True`; keyless, always loaded.

## External access audit

Requested explicitly for this batch: there is **no subprocess use and no
filesystem write** anywhere in this module. The only filesystem touch is
read-only and indirect: `zoneinfo.ZoneInfo` loads the system IANA tz database
for `.tz`, and `os.strerror` / `signal.Signals` / `errno` are in-process libc
lookups. No finding.

## Notable per-command behavior

- `devtools.py - _jwt()`: splits on `.`, base64url-decodes header and payload
  with re-padding (`_b64url_decode()`), requires both to be JSON objects.
  Signature is never checked and the output says so in help; `alg=none` gets an
  explicit `WARNING: alg=none (unsigned!)`. Registered claims print first,
  time claims (`iat`/`nbf`/`exp`) render as UTC ISO via `_fmt_ts()`, then up to
  3 arbitrary extra claims. Untrusted claim values are only sanitized by the
  wrapper-level `strip_ctrl()` (sufficient: single line, 400-char cap).
- `devtools.py - _semver_parse()` / `_semver_cmp()`: SemVer 2.0.0 precedence -
  optional `v` prefix, build metadata split off and ignored, pre-release
  identifiers compared as `(0, int)` / `(1, str)` tuples so numeric < 
  alphanumeric and no int/str comparison can throw. A release outranks its own
  pre-release. Tests: `test_devtools.py - TestSemver`.
- `devtools.py - _uuid5()`: two-form command - with a name it derives
  `uuid.uuid5` under a well-known namespace (or any UUID as namespace); with a
  single UUID argument it inspects version/variant instead
  (`_uuid_inspect()`, which returns `None` on non-UUIDs despite its `-> str`
  annotation - a known, `type: ignore`d lie).
- `devtools.py - _tz()`: `_resolve_zone()` first maps common abbreviations
  (`_TZ_ABBR`) to IANA names; the table's comment documents the policy -
  abbreviations are formally ambiguous, this maps what a user of a US-operated
  bot means, and genuinely ambiguous ones (IST, BST) are deliberately omitted.
  `_parse_clock()` accepts full ISO datetimes or bare `HH:MM[:SS]`; bare times
  anchor to 2000-01-01 (January = northern standard time, so `pst` means PST
  not PDT), the placeholder date is suppressed in output, and a day rollover is
  flagged as `+1d`/`-1d` (tests: `TestTz.test_bare_time_flags_a_day_rollover`
  and neighbors).
- `devtools.py - _unix()`: numeric input reports both the signal and the errno
  with that number; names try `SIG`-prefixed signals first, then errno
  constants via `hasattr(errno, up)`. `os_strerror()` wraps `os.strerror`
  against `ValueError`/`OverflowError`.
- `devtools.py - _color()`: parses CSS name (29-entry table), `#rgb`/`#rrggbb`,
  `rgb(...)` (clamped 0-255), `hsl(...)` (via `colorsys.hls_to_rgb` - note the
  argument-order swap hsl -> hls). Output adds nearest CSS name by squared RGB
  distance, `~`-prefixed when inexact.
- `devtools.py - _cron()`: 5-field parser. `_cron_field()` expands
  lists/ranges/steps/names into a set, and bounds-checks start/end BEFORE
  materializing `range()` - the in-code comment records the DoS this prevents
  (`0-999999999` would otherwise build a billion-element set on the event
  loop). Sunday accepts both 0 and 7. Matching uses vixie-cron OR semantics
  when both day-of-month and day-of-week are restricted
  (`_cron_matches()`). An impossible-date short-circuit (e.g. `0 0 30 2 *`)
  skips the scan when day-of-week is unrestricted. Otherwise it scans forward
  minute-by-minute up to 366 days for the next 2 fire times - up to ~527k
  iterations, which is why `cmd_cron()` is the one handler that runs via
  `asyncio.to_thread` (in-code comment).

## Concurrency

Only `cmd_cron()` offloads to a thread. Everything else is sub-millisecond and
runs on the event loop.

## Failure behavior

All parse failures return usage/diagnostic strings; user fragments echoed back
are `strip_ctrl()`-capped short (40-60 chars). Every reply passes through
`strip_ctrl()`. Arg length caps: 400 default, 80 per semver operand, 40 for
`.unix` and the `.tz` time, 60 per zone name.

## Security notes

No network, no secrets, no state, no subprocess. `.jwt` decodes but never
validates - a display tool, correctly labeled. CPU bounded by the cron
pre-materialization bounds check plus `to_thread` for the scan.

## Findings

- questionable | devtools.py - _cron_matches() | Restriction detection compares
  the expanded set against the full range, so an explicitly written full range
  (`* * 1-31 * 1-5`) is treated as unrestricted day-of-month; real cron treats
  any non-`*` field as restricted, changing the dom/dow OR semantics in this
  edge case.
- questionable | devtools.py - os_strerror() | Module-level helper without the
  `_` prefix every sibling uses, with a function-local `import os`; works, but
  inconsistent with the file's conventions.
- test-gap | tests/test_devtools.py | No test covers the dom/dow OR rule
  (both restricted), the impossible-date short-circuit (`0 0 30 2 *`), or the
  `_cron_field` huge-range rejection that the in-code comment calls out as the
  DoS guard.
