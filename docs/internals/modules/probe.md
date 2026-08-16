# probe.py - SSRF-guarded active network probers (.headers / .ssl / .tcp / .down)

## Purpose

The only batch of commands that CONNECTS to a user-supplied host rather
than querying a fixed API about it: HTTP header inspection, TLS certificate
summary, a single TCP connect probe, and an up/down reachability check.
Because the destination is user-controlled, every path is wired through an
SSRF guard before any socket opens. Module class: `modules/probe.py -
ProbeModule`, built on [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.headers` | `.headers <url>` | `HTTP 301 :: server nginx :: type text/html :: -> <Location> :: sec: HSTS,CSP` (security headers tracked: HSTS, CSP, XFO, XCTO) |
| `.ssl` | `.ssl <host[:port]>` (default 443) | `host:443 CN=... issuer=... expires <notAfter> (Nd)` or a cert-invalid line |
| `.tcp` | `.tcp <host> <port>` | `host:22 open (12 ms) [ip]` / `closed` / `filtered (timeout)` |
| `.down` | `.down <host\|url>` | `host is UP (HTTP 200)` / `UP (tcp/443 open)` / `appears DOWN (no HTTP/TCP response)` |

Argument caps in the handlers: 300 chars for `.headers`/`.down`, 120 for
`.ssl`/`.tcp` host, 6 for the port string.

## What it will and will not probe

Will: one TCP connect, one TLS handshake, or one HTTP request per
invocation, to a host whose EVERY resolved address is public. Will not:
follow redirects (`.headers`/`.down` report the `Location` instead of
chasing it, closing the redirect-into-intranet hole), connect to private /
loopback / link-local / multicast / reserved / metadata addresses, scan
ranges or multiple ports (one target, one port, per rate-limited command),
or speak anything besides TCP connect / TLS / HTTP (no UDP, no ICMP, no
banner grabbing).

## Integration

No fixed third-party API; the "integration" is the target itself, plus two
guard layers from the codebase:

- `modules/base.py - resolve_public()` (used by `.tcp`, `.ssl`, and
  `.down`'s TCP fallback): raises `ValueError` for empty/oversized/
  unresolvable hosts or if ANY resolved address is non-public. Callers then
  connect to `infos[0]`'s sockaddr from the returned list rather than
  re-resolving the name, which removes the resolve/connect TOCTOU for the
  raw-socket paths.
- `modules/_netsafe.py - safe_open()` (used by `.headers` and `.down`'s
  HTTP path): per-hop validation via `resolve_safe_ip()` plus thread-local
  DNS pinning, so urllib3 connects to exactly the validated IP while SNI /
  TLS verification / Host still use the real hostname. Called with
  `follow_redirects=False` here, so only the first hop is ever contacted.

## Configuration

Only the shared `weather_user_agent` UA credential (`on_load()`; naming
finding recorded in [ipintel](ipintel.md)). `is_configured()` is `True`;
`_TIMEOUT` is 7 s (connect probes use 5 s socket timeouts).

## State

None; nothing cached or persisted.

## Failure behavior

- `.tcp` maps outcomes rather than failing: timeout -> `filtered`,
  `ConnectionRefusedError`/`OSError` -> `closed`; guard refusal echoes the
  `ValueError` text. The socket is closed in a `finally`.
- `.ssl` distinguishes `ssl.SSLCertVerificationError` (reported as
  `cert NOT valid - <verify_message>`) from transport/handshake failure
  (`TLS connect failed`). An unparseable `notAfter` leaves days as `"?"`
  rather than failing the reply.
- `.headers` returns the guard message on `SSRFBlocked` and a generic
  `request failed` on `requests.RequestException`.
- `.down` degrades in two stages: HTTP HEAD via `safe_open`, then on
  `requests.RequestException` a bare TCP connect to 443 then 80 (each
  re-validated through `resolve_public`), and only then `appears DOWN`.
  An `SSRFBlocked` refusal does NOT fall through to the TCP stage - the
  guard verdict is final.

## Security notes

- SSRF: the module's central property is that all four commands are
  guard-wired. `tests/test_probe.py - TestProbersRefuseInternal` proves the
  wiring per command by probing `localhost` with no mocks - each helper
  must refuse; `TestSSRFGuard` sweeps RFC1918, loopback, link-local, cloud
  metadata (169.254.169.254), multicast, unspecified, and class-E through
  `resolve_public`. The rebinding defenses differ by path and are
  complementary: raw-socket commands connect to the already-validated
  sockaddr (no second resolution exists); HTTP commands pin DNS for the
  request thread (`tests/test_netsafe.py -
  TestSafeOpen.test_redirect_to_internal_blocked` covers the hop
  re-validation that `follow_redirects=False` makes moot here).
- Abuse surface: `.tcp` (and `.down`'s fallback) lets any IRC user cause
  the bot's host to open one TCP connection to an arbitrary public
  host:port - a low-rate port probe by proxy, attributable to the bot
  operator's IP. Mitigations are the per-nick rate limit and the
  single-connect design; there is no port denylist. This is the command's
  documented purpose, not a defect, but operators should know the traffic
  originates from their address.
- TLS posture of `.ssl`: `ssl.create_default_context()` keeps full chain +
  hostname verification ON (that is what makes the cert-invalid branch
  reachable) and `minimum_version = TLSv1_2` refuses legacy protocol
  handshakes on the probe.
- Privacy: the target receives a normal connection from the bot's IP with
  the configured UA (HTTP paths); nothing is sent to any third party, and
  the requesting nick appears nowhere on the wire.
- Output injection: every reflected field (host, Server, Content-Type,
  Location, CN, issuer, verify message) is `strip_ctrl`'d with per-field
  caps before splicing into the reply.
- Rate limiting: per-nick `_gate()` on every command.

## Functions and methods

| Symbol | Purpose |
|---|---|
| `_tcp()` | Port parse/range check -> `resolve_public` -> timed connect to `infos[0]`, latency in ms |
| `_ssl_cert()` | `host[:port]` split -> `resolve_public` -> verified TLS handshake -> subject/issuer/notAfter summary with days-to-expiry |
| `_headers()` | Scheme normalization (bare host gets `https://`) -> `safe_open` GET, redirects reported not followed -> status/server/type/Location/security-header summary |
| `_down()` | URL or bare host -> `safe_open` HEAD -> TCP 443/80 fallback -> UP/DOWN verdict |
| `ProbeModule.cmd_*()` | Gate -> usage -> truncate arg -> `asyncio.to_thread(helper)` -> privmsg |
| `setup()` | Module entry point |

## Findings

- doc-drift | `probe.py` module docstring | Claims "every command calls
  base.resolve_public() first", but `_headers()` and `_down()`'s primary
  path use `_netsafe.safe_open()` / `resolve_safe_ip()` instead; the guard
  property holds, the stated mechanism is stale.
- questionable | `probe.py - _ssl_cert()` | `arg.partition(":")` splits at
  the FIRST colon, so a bare IPv6 literal (`.ssl 2001:db8::1`) is mangled
  into host `2001` and always fails resolution; `.tcp` handles IPv6 fine
  because host and port are separate arguments.
- questionable | `probe.py - ProbeModule.cmd_*()` | `import asyncio` is
  repeated inside all four handlers instead of a top-level import; the
  sibling modules in this batch import it at module scope.
- test-gap | `probe.py - _ssl_cert()` / `_down()` | Certificate-summary
  formatting, expiry-day computation, and the `.down` TCP fallback ladder
  have no tests; `tests/test_probe.py` covers only guard wiring and
  `_headers` formatting.
