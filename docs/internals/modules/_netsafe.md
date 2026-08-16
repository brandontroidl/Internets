# _netsafe.py - SSRF-safe HTTP fetch with DNS pinning

`modules/_netsafe.py` (187 lines) is the defense layer for fetching
user-influenceable URLs: it validates that a URL's host resolves only to public
addresses, then forces the actual TCP connection to the exact IP that was validated
(closing the DNS-rebinding TOCTOU), and repeats both steps on every redirect hop.
The underscore prefix marks it as infrastructure, not a loadable command module (the
loader's `^[a-z]...` name check would accept it, but it defines no `setup()`).

## Purpose

`base.fetch_json` only caps response size; it trusts its destination. Any command
that fetches a URL a user (or a fetched feed) chose - `urls.py` (`.title`,
shortener pre-flight), `probe.py` (`.headers`/`.down`), `linktitle.py` (passive
link titles), `scinews.py` (article reader), `ipintel.py` (hostname resolution) -
would otherwise be an SSRF proxy into the operator's network: cloud metadata
endpoints (`169.254.169.254`), RFC1918 hosts, localhost services. `_netsafe`
centralizes the refusal logic so every URL-fetching module shares one audited
implementation.

## Responsibilities and boundaries

Belongs here: address classification (`ip_is_blocked`), safe resolution
(`resolve_safe_ip`), URL pre-flight (`url_is_safe`), and the pinned fetch
(`safe_open`).

Deliberately NOT here:

- **Response size capping** - `safe_open` yields a streaming `Response`; bounding the
  body is the caller's job (`probe.py`, `linktitle.py`, `scinews.py`, `urls.py` each
  do a capped read). A caller that reads unboundedly has a defect, but not here.
- **Content sanitization** - callers route extracted text through
  `base.strip_ctrl()`.
- **aiohttp** - the pin hooks `socket.getaddrinfo`, which aiohttp does not use (it
  resolves via the event-loop resolver); the module docstring states this. All
  callers use `requests` inside `asyncio.to_thread`.

## Dependencies and dependents

Imports `requests` at module top (unlike `base.py`, this file is only pulled in by
modules that already need HTTP), plus stdlib `socket`, `threading`, `ipaddress`,
`urllib.parse`, `contextlib`.

Dependents: `urls.py` (`url_is_safe`, `safe_open`), `probe.py` (`safe_open`),
`linktitle.py` (`safe_open`), `scinews.py` (`safe_open`), `ipintel.py`
(`resolve_safe_ip`). `base.resolve_public()` is the sibling guard for raw-socket
probes; the comparison table is in [base.md](base.md#resolve_public-vs-_netsafe).

## Lifecycle and state

Imported on first use by any dependent module. Import has one global side effect:
it wraps `socket.getaddrinfo` process-wide with `_pinning_getaddrinfo` (guarded by a
`_netsafe_wrapped` marker attribute so re-imports are idempotent). The wrapper is a
pass-through no-op for every thread that has not set a pin, so unrelated code paths
(IRC connection, weather aiohttp, DNS in other modules) are unaffected.

Mutable state is exactly one thread-local: `_pin.map`, a `{hostname: ip}` dict set
around each `session.request()` call in `safe_open` and cleared in a `finally`. No
persistent state.

## Concurrency

The pin is `threading.local()`, so concurrent `safe_open` calls in different
`to_thread` workers cannot see or clobber each other's pins. The pin is set only for
the duration of `session.request(...)` (connection establishment); the subsequent
body read reuses the already-established connection, so no pin is needed then - the
inline comment states this and it holds because `safe_open` uses a fresh
single-connection `requests.Session` per hop. The global wrapper installation is a
one-time import-time assignment; worst case under a re-import race is installing the
wrapper twice, and a double-wrapped chain still resolves pins correctly since the
outer wrapper forwards the literal IP.

## The algorithm and what each step stops

`safe_open(method, url, ua, *, follow_redirects=True, timeout=10, max_redirects=5)`
is a `@contextmanager`. Per hop (at most `max_redirects + 1` iterations):

1. **Parse + scheme allowlist** - only `http` / `https`; anything else
   (`file://`, `gopher://`, `ftp://`) raises `SSRFBlocked`. Stops protocol-smuggling
   into local files or non-HTTP services.
   (`test_netsafe.py - TestSafeOpen.test_bad_scheme_raises`)
2. **Zone-id strip** - `host.split("%", 1)[0]`; an IPv6 zone suffix
   (`fe80::1%eth0`) cannot smuggle a different interpretation past the parser.
3. **Metadata-name blocklist** - `METADATA_HOSTS` (`169.254.169.254`,
   `fd00:ec2::254`, `metadata.google.internal`) rejected by name before resolution.
   Defense-in-depth: each of these also fails the IP check after resolution, but the
   name check does not depend on DNS answering honestly.
4. **Resolve + validate ALL answers** - `resolve_safe_ip(host)`:
   - An IP literal is classified directly (no DNS).
   - A hostname is resolved via `_orig_getaddrinfo` (the unwrapped resolver - the
     pin must never influence validation), and EVERY answer must pass
     `ip_is_blocked`; one bad answer rejects the whole host. Stops the
     mixed-answer rebinding trick where a resolver returns one public and one
     internal address and hopes the client picks the internal one
     (`test_netsafe.py - TestResolveSafeIp.test_any_private_answer_blocks`).
   - Returns the FIRST safe answer as the pin target, or `None` on any failure
     (fail-closed: unresolvable = blocked).
5. **Pin + connect** - a fresh `requests.Session` per hop; `_pin.map = {host:
   pinned}` for exactly the `session.request(...)` window. urllib3 resolves the
   hostname through the wrapped `socket.getaddrinfo`, which forwards the pinned
   literal - the connection lands on the validated IP even if the authoritative DNS
   now answers differently. Stops the classic resolve/connect TOCTOU (DNS
   rebinding). Because the REQUEST still uses the hostname, SNI, certificate
   verification, and the Host header all work normally - which is the whole reason
   for pinning at the resolver instead of connecting to an IP literal (the module
   docstring records that the `HTTPAdapter server_hostname` alternative breaks TLS
   under requests 2.34 / urllib3 2.7).
6. **Redirect handling** - requests-level redirects are disabled
   (`allow_redirects=False`); `safe_open` implements the loop itself so hops cannot
   escape validation. On a 3xx: with `follow_redirects=False` the redirect response
   itself is yielded (probe's `.headers` wants to SHOW the redirect); otherwise the
   `Location` header is resolved against the current URL (`urljoin` - relative
   redirects work) and the loop restarts, re-running every check above. Stops the
   open-redirect SSRF: a public host 302-ing to `http://169.254.169.254/...`
   (`test_netsafe.py - TestSafeOpen.test_redirect_to_internal_blocked`). A 3xx
   without a `Location` raises `SSRFBlocked`; exhausting `max_redirects` raises
   `SSRFBlocked("redirect limit exceeded")`.
7. **Cleanup** - the previous hop's session is closed before each new hop, and the
   final session closes in the outer `finally`, so the streaming body must be read
   inside the `with` block.

### ip_is_blocked()

Classification used everywhere: unwraps IPv4-mapped IPv6 (`::ffff:10.0.0.1` is
judged as `10.0.0.1` - interpreter-version-independent, unlike relying on
`is_private` semantics), then blocks `is_private` (RFC1918 + ULA `fc00::/7`),
`is_loopback`, `is_link_local` (includes 169.254.0.0/16, hence all AWS-style
metadata IPs), `is_multicast`, `is_reserved` (includes NAT64 `64:ff9b::/96`,
verified locally), `is_unspecified` (`0.0.0.0`, `::`), and IPv6 site-local
`fec0::/10`. `test_netsafe.py - TestIpBlocked` pins ten blocked ranges and four
public allowances.

### url_is_safe()

Pre-flight validation only (scheme + metadata-name + `resolve_safe_ip`), returning
bool. Used by `urls.py` before handing a user URL to a third-party shortener - the
bot never fetches that URL itself, so there is nothing to pin; the check keeps the
bot from laundering internal-address URLs through its shortener credential. By
nature TOCTOU-open (resolution can change after the check); acceptable because the
subsequent fetch is done by the third party from their network, not the bot's.

## Failure behavior

`SSRFBlocked` for every guard refusal (unparseable URL, bad scheme, metadata name,
non-public or unresolvable host, missing Location, hop-limit). Transport errors
surface as `requests.RequestException`. Callers (all module code) catch both and
degrade to a user-facing error string. `resolve_safe_ip` never raises - `None` is
its only failure signal.

## Deliberate non-defenses (bypass classes out of scope)

Documented so a maintainer does not mistake them for oversights:

- **A hostile PUBLIC server** - `_netsafe` only guarantees the bot connects to
  public addresses. A public host that reverse-proxies into someone's internal
  network, or serves attacker content, is out of scope by design.
- **Port targeting** - any port on a public IP is allowed (probe commands are FOR
  that). Internal-network port scanning is prevented; public-internet port probing
  is a feature.
- **aiohttp / non-requests HTTP** - the pin only intercepts `socket.getaddrinfo`.
  Code that resolves another way is not covered; currently no caller does.
- **Time-of-use drift after `url_is_safe`** - pre-flight only, see above.
- **Response content** - size and content handling are the caller's duty.

## Implementation walk

- Lines 1-39: module docstring (the why of thread-local pinning vs an IP-literal
  adapter), imports, logger, `DEFAULT_MAX_REDIRECTS = 5`, `DEFAULT_TIMEOUT = 10`,
  `METADATA_HOSTS`.
- Lines 42-44: `SSRFBlocked` exception.
- Lines 46-59: `ip_is_blocked` (above).
- Lines 62-79: the thread-local pin, `_orig_getaddrinfo` capture,
  `_pinning_getaddrinfo` wrapper, and the idempotent install guard. The wrapper
  looks up the requested host in the current thread's pin map and substitutes the
  validated IP literal; everything else passes through untouched.
- Lines 82-113: `resolve_safe_ip` (above). Note the zone-id strip on each
  getaddrinfo answer string and the fail-closed `return None` on an unparseable
  answer.
- Lines 116-130: `url_is_safe` (above).
- Lines 133-187: `safe_open` (above).

Nothing unreachable; every branch is on the guard or cleanup path.

## Findings

- test-gap | `_netsafe.py - safe_open()` | The hop-limit branch
  (`redirect limit exceeded`) and the redirect-without-Location branch are untested;
  `test_netsafe.py` covers internal literal, bad scheme, and one redirect-to-internal
  hop only.
- questionable | `_netsafe.py - safe_open()` | Scalar `timeout` bounds connect and
  per-read socket waits, not total duration, and it applies per hop - a slow
  redirect chain can take up to ~`(max_redirects + 1) * timeout` before the
  dispatcher's 60 s command timeout fires (same accumulation pattern flagged for
  `base.fetch_json`).
- questionable | `_netsafe.py - _pinning_getaddrinfo()` | The pin substitutes by
  exact hostname match. Harmless today (the pin map only ever holds the host being
  requested), but a future caller pinning host A while urllib3 resolves host B
  would silently connect unpinned - the no-op-by-default design trades strictness
  for zero blast radius, and that tradeoff should be preserved knowingly.
- doc-drift | `_netsafe.py - module docstring` | Cites "probe.py .headers/.down,
  scinews.py article reader" as the users; the actual dependent set is larger
  (`urls.py`, `linktitle.py`, `ipintel.py` also import it).
