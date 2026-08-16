# ipinfo.py - IP/hostname geolocation lookup (.ipinfo)

## Purpose

One-command geolocation wrapper around ip-api.com: city / region / country,
timezone, ISP, and a Google Maps link for an IP or hostname. Distinct from
[ipintel](ipintel.md)'s `.ip` (reputation): `.ipinfo` answers "where is
it", `.ip` answers "is it bad". Module class: `modules/ipinfo.py -
IpinfoModule`, built on [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.ipinfo` | `.ipinfo <ip/host>` | `**IP/Host** target (ip) \| **Location** city, region, country [CC] \| **Timezone** tz \| **ISP** name \| https://maps.google.com/maps?q=lat,lon` |

## Integration

- Endpoint: `http://ip-api.com/json/<target>` with a `fields=` allowlist
  (status, message, query, location fields, lat/lon, timezone, isp, org).
  Plain HTTP: the source comment in `_lookup_sync()` records that the free
  tier is HTTP-only (HTTPS requires the paid `pro.ip-api.com` host); see
  Findings.
- Keyless; timeout 10 s; inline stream+cap read bounded at `_MAX_BODY_BYTES`
  (32 KB) - this module predates `fetch_json` and implements the same
  stream+cap pattern inline, which the HTTP-cap policy explicitly allows.
- Upstream `status: "fail"` (private ranges, unresolvable hosts, quota)
  is reflected as `<message> for '<target>'`.

## Configuration

Only the shared `weather_user_agent` UA credential (`on_load()`; naming
finding recorded in [ipintel](ipintel.md)). No `is_configured()` override
(inherits `True`).

## State

None.

## Failure behavior

A single broad `except Exception` around the whole fetch/parse collapses
transport errors, size-cap overrun, and JSON errors to `"lookup failed"`
(size overrun also logs a specific warning). The rate-limit check runs
after the usage branch, so an empty-arg reply is not rate-gated (trivial
ordering difference from the `_gate()`-first modules).

## Security notes

- SSRF: none - the endpoint is fixed; the bot never connects to the target.
  Consequently `_netsafe` is deliberately absent, and private-range targets
  are NOT refused locally: `.ipinfo 10.0.0.1` goes to ip-api, which answers
  with a `fail`/`private range` message. Nothing internal is contacted.
- Input validation: `_TARGET_RE` (`[A-Za-z0-9.:\-]`, max 253) rejects
  before any request - both in the handler (cheap user feedback) and again
  inside `_lookup_sync()` - and the target is additionally `quote(...,
  safe='')`-escaped into the path; the source comments call out the exact
  threats (path traversal `8.8.8.8/../..`, scheme injection). The
  validate+quote pattern is referenced as the model by
  `tests/test_pkginfo_validate.py`'s docstring.
- Transport: because the request is cleartext HTTP, the queried target and
  the response cross the network unencrypted; an on-path attacker can read
  the query and forge the response. The bounded read plus per-field
  `strip_ctrl` (every upstream field, with per-field caps - the source
  comment names DNS/PTR poisoning as the vector) confine a forged response
  to bogus but non-injectable display text.
- Privacy: the queried IP/hostname is disclosed to ip-api.com in cleartext;
  the requesting nick is not sent. The maps link is emitted only when lat
  and lon are numeric types, never interpolated from strings.
- Rate limiting: per-nick `bot.rate_limited()` check inline in
  `cmd_ipinfo()`.

## Notes

Small module, no implementation walk needed: `_lookup_sync()` is
validate -> fetch (stream+cap) -> parse -> sanitize-per-field -> assemble,
run off-thread from `cmd_ipinfo()`. `_strip_ctrl()` is a thin alias for
`base.strip_ctrl` kept from before the helper moved to base. The file has
no module docstring (the only one in this batch without one).

## Findings

- questionable | `ipinfo.py - _lookup_sync()` | Cleartext HTTP transport to
  ip-api.com: queried targets are observable and responses forgeable
  on-path. Documented in-source as a free-tier constraint and mitigated for
  injection (cap + strip_ctrl), but the disclosure itself is inherent;
  switching to an HTTPS-capable geolocation source would remove it.
- test-gap | `ipinfo.py - _lookup_sync()` | No test_ipinfo.py exists;
  `_TARGET_RE` and the fail-status / size-cap / sanitization branches are
  untested (the pattern is only indirectly referenced by
  `tests/test_pkginfo_validate.py`).
- questionable | `ipinfo.py - _strip_ctrl()` | Redundant local alias of
  `base.strip_ctrl` (as is `httpcode.py`'s); harmless, but inviting drift
  if the base helper's signature changes.
