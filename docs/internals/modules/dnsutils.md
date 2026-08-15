# dnsutils.py - DNS and RDAP lookups over HTTPS (.dns / .rdns / .caa / .whois / .asn)

## Purpose

Keyless DNS and registration-data lookups without a local resolver
dependency: forward and reverse DNS via Cloudflare DNS-over-HTTPS, CAA plus
best-effort SPF/DMARC, and RDAP domain / IP / autnum lookups via the
rdap.org bootstrap redirector. Module class: `modules/dnsutils.py -
DnsutilsModule`, built on [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.dns` | `.dns <host> [type]` (default `A`) | `**host** TYPE: data, data, ...` or `no TYPE records for host (NXDOMAIN)` |
| `.rdns` | `.rdns <ip>` | `**ip** PTR: name.` |
| `.caa` | `.caa <domain>` | `**domain** CAA: ... \| SPF: v=spf1 ... \| DMARC: v=DMARC1 ...` (CAA absent reads `none (any CA may issue)`) |
| `.whois` | `.whois <domain>` | `**domain** \| registrar X \| created YYYY-MM-DD \| expires YYYY-MM-DD \| ns a b \| status ...` |
| `.asn` | `.asn <ip\|ASn>` | `**label** \| name \| (handle) \| start-end \| type \| CC` |

`.dns` accepts types `A AAAA MX TXT NS CNAME SOA PTR SRV CAA` (`_TYPES`);
the usage line advertises only the common six. Example: `.dns example.com MX`.

## Integration

| Source | Endpoint | Timeout | Cap | Used by |
|---|---|---|---|---|
| Cloudflare DoH | `https://cloudflare-dns.com/dns-query?name=...&type=...` (`Accept: application/dns-json`) | 10 s | 256 KB default | `_doh()` -> `.dns` / `.rdns` / `.caa` |
| RDAP | `https://rdap.org/domain/<domain>`, `/ip/<ip>`, `/autnum/<n>` | 12 s | 512 KB | `_whois_sync()` / `_asn_sync()` |

All fetches go through `modules.base.fetch_json`; RDAP uses
`allow_404=True` so a missing record is a clean "no RDAP record" miss, not
an error. No API key anywhere; `is_configured()` returns `True`.

DoH answer parsing (`_answers()`) filters the `Answer` array by numeric
rrtype code (`_TYPE_CODES`) - necessary because DoH returns the full CNAME
chain, so an `A` query for a CNAMEd host contains type-5 records that must
not be reported as addresses (`tests/test_dnsutils.py -
TestAnswers.test_type_filter`). RCODE is mapped through `_RCODE` and only
appended when not `NOERROR`.

RDAP parsing helpers: `_rdap_registrar()` walks `entities[]` for the
`registrar` role and pulls the vCard `fn` (falling back to `handle`);
`_rdap_event()` picks `eventDate` by `eventAction`; `_rdap_nameservers()`
lowercases `ldhName`s. All defend against non-dict / non-list shapes
(`TestRdapHelpers`).

## Configuration

Only the shared `weather_user_agent` UA credential, read in `on_load()`
(cross-module naming finding recorded in [ipintel](ipintel.md)).

## State

None; no cache, nothing persisted.

## Failure behavior

Each `_*_sync` helper has two catch tiers: the specific tuple
(`ResponseTooLarge, KeyError, ValueError, TypeError`) and a broad
`except Exception` that intentionally absorbs `requests.RequestException`
and anything else; both log a warning and return `"lookup failed"`
(`TestDns.test_network_error_handled` drives a raw `RuntimeError` through
it). Input validation failures return specific strings (`invalid host`,
`invalid IP`, `invalid domain`, the `.asn` usage hint) without any network
call (`test_invalid_host` asserts no fetch happens). In `.caa`, SPF/DMARC
lookups are individually wrapped so their failure never suppresses the CAA
answer (`test_spf_dmarc_errors_are_nonfatal`).

## Security notes

- SSRF: none by construction - the module never connects to a user-supplied
  host. Every request goes to one of two fixed endpoints; user input rides
  as a query parameter (DoH) or a path segment (RDAP).
- Path-segment safety: RDAP interpolates the domain/IP into the URL path
  without `quote()`, guarded instead by validation: `_HOST_RE`
  (`[A-Za-z0-9._-]`, max 253 - underscore included for `_dmarc` style
  names) excludes `/`, `%`, and whitespace, so traversal or query injection
  is unreachable; `.asn` accepts only a strict `(?:as)?\d{1,10}` match or a
  string `ipaddress.ip_address()` parses.
- DNS rebinding: not applicable here. The classic rebinding attack needs a
  victim that resolves a name and then connects to the answer; dnsutils
  only reports resolution data and never opens a connection to any resolved
  address. Resolver behavior is Cloudflare's (1.1.1.1 policy: no client-IP
  forwarding, no EDNS client subnet), and answers are treated purely as
  display data.
- Private targets: `.rdns 10.0.0.1` or `.asn 192.168.1.1` are allowed - the
  query goes to the fixed public endpoint, which answers from the special
  registries; no internal connection results, so there is nothing to block.
- Privacy: every queried name/IP is disclosed to Cloudflare (DoH) or the
  rdap.org redirector plus the registry it forwards to. The requesting nick
  is never sent.
- Output injection: every upstream string passes `strip_ctrl` with a length
  cap at the point of extraction (`_answers()` at 200 chars per record,
  the RDAP field reads individually); `TestAnswers.test_strips_control_chars`
  pins a TXT record carrying CRLF. `_join()` caps the assembled line at
  `_MAX_LINE` (380).
- Rate limiting: per-nick `_gate()` -> `bot.rate_limited(nick)` on every
  command, refusal by NOTICE.

## Functions and methods

| Symbol | Purpose |
|---|---|
| `_doh()` | One DoH query via `fetch_json` (default 256 KB cap) |
| `_answers()` | Extract + sanitize `Answer[].data`, optional rrtype filter |
| `_join()` | Join fields, truncate to `_MAX_LINE` with `...` |
| `_dns_sync()` | Validate host+type, query, format with RCODE tail |
| `_reverse_name()` / `_rdns_sync()` | `ipaddress.reverse_pointer` -> PTR query (handles v4 and v6) |
| `_spf_dmarc()` / `_caa_sync()` | CAA plus best-effort apex-TXT SPF and `_dmarc` TXT |
| `_rdap_registrar()` / `_rdap_event()` / `_rdap_nameservers()` | Defensive RDAP field extraction |
| `_whois_sync()` | RDAP domain lookup + assembly (registrar, events, ns <= 4, status <= 3) |
| `_asn_sync()` | Dispatch `ASn` -> `/autnum/`, IP -> `/ip/`; format network fields |
| `DnsutilsModule.cmd_*()` | Gate -> usage -> `asyncio.to_thread(_*_sync)` -> privmsg |
| `setup()` | Module entry point |

## Findings

- questionable | `dnsutils.py - _asn_sync()` | The address-range separator
  in the reply f-string is the non-ASCII en dash U+2013 between `start` and
  `end`, which contradicts the repository owner's stated no-dash output
  preference; cosmetic.
- questionable | `dnsutils.py - _dns_sync()` | `.dns` accepts ten record
  types but the error and usage strings advertise only six
  (`try A/AAAA/MX/TXT/NS/CNAME`), so SOA/PTR/SRV/CAA support is
  undiscoverable from the command itself.
