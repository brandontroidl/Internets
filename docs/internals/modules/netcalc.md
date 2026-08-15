# netcalc.py - offline network calculators (.cidr / .subnet / .port)

## Purpose

Pure-stdlib subnet math and port/service mapping. No network, no key, no
state. Module class: `modules/netcalc.py - NetcalcModule`, built on
[base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.cidr` | `.cidr <a.b.c.d/prefix>` | `10.0.0.0/24 :: net 10.0.0.0 :: bcast 10.0.0.255 :: mask 255.255.255.0 :: hosts 254 :: usable <first>-<last>` (IPv6: prefix + address count, shown as a power of two above 10^12) |
| `.subnet` | `.subnet <ip/prefix> <new_prefix>` | `256 x /24 :: 256 addr each :: first 10.0.0.0 :: last 10.0.255.0` |
| `.port` | `.port <number\|name>` | `port 22 -> ssh` / `https -> port 443` |

## Integration

None. `.port` consults the bundled `_PORTS` table first, then falls back to
`socket.getservbyport()` / `getservbyname()` - a local `/etc/services` read,
not a network call; the bundled table exists precisely because that file
varies by host.

## Configuration

None; `is_configured()` returns `True`.

## Failure behavior

All parsing failures return usage/error strings from the pure helpers:
`ipaddress.ip_network(strict=False)` rejects malformed CIDR, `.subnet`
range-checks the new prefix against `prefixlen..max_prefixlen`, `.port`
range-checks 0-65535 and reports unknown names/numbers explicitly. Nothing
can raise past the helpers except `strip_ctrl`-wrapped returns.

## Security notes

No trust boundary is crossed: no network, no filesystem writes, no secrets.
Inputs are truncated in the handlers (`_MAX_INPUT` 80, port arg 8) and
replies pass `strip_ctrl`. Computation cost is bounded: `_subnet()` derives
count and first/last arithmetically (`2 ** (newlen - prefixlen)`,
`net[-1]`) instead of enumerating subnets, so `.subnet ::/0 128` is O(1)
big-int math, not 2^128 iterations. Per-nick rate limiting via `_gate()`.

## Findings

- questionable | `netcalc.py - _cidr()` / `_subnet()` / `_port()` | Reply
  strings use non-ASCII glyphs (the en dash U+2013 in `.cidr`'s usable
  range and `.port`'s range message, U+00D7 multiply and U+2192 arrow),
  contradicting the repository owner's stated no-dash output preference;
  cosmetic.
- test-gap | `netcalc.py - _cidr()` / `_subnet()` / `_port()` | No test
  file; the /31 / /32 host-count branches, IPv6 formatting, and the
  getservby* fallbacks are unpinned.
