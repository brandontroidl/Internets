# secinfo.py - security lookups: CVE, password-pwn, hash and crypto helpers

## Purpose

Five keyless security helpers in one module: an NVD CVE lookup, a Have I
Been Pwned breached-password check (k-anonymity, PM-only), and three fully
offline references (hash-type identification, CVSS v3.1 base-score
computation, cipher fact sheet). Module class: `modules/secinfo.py -
SecinfoModule`, built on [base](base.md).

## Commands

| Command | Usage | Network | Reply shape |
|---|---|---|---|
| `.cve` | `.cve <CVE-YYYY-NNNN>` | NVD | `**CVE-...** - CVSS 9.8 (CRITICAL) \| <description <=240 ch> \| published YYYY-MM-DD` |
| `.pwn` | `.pwn <password>` (PM only) | HIBP | `**pwned** - this password appears in N known breaches. ...` or a not-found line |
| `.hashid` | `.hashid <hash>` | none | `<hash> -> MD5, NTLM, MD4 (hex, 32 chars)` etc. |
| `.cvss` | `.cvss <CVSS:3.1/AV:N/...>` | none | `CVSS v3.1 base 9.8 (Critical)` |
| `.cipher` | `.cipher <name>` | none | one bundled reference line (type, sizes, status) |

## Integration

| Source | Endpoint | Auth | Timeout | Cap | Helper |
|---|---|---|---|---|---|
| NVD | `https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=...` | none | 12 s | 512 KB | `_cve_sync()` via `fetch_json(allow_404=True)` |
| HIBP | `https://api.pwnedpasswords.com/range/<5-hex-prefix>` | none | 12 s | 1 MB | `_pwn_sync()` inline stream+cap (plain-text API, not JSON) |

`.cve` parsing: English description picked from `descriptions[]`, truncated
to 240 chars; score preference v3.1 > v3.0 > v2 in `_cve_score()` (v2
severity sits at the entry level, not inside `cvssData`, and the code reads
it there). Behavioral evidence: `tests/test_secinfo.py -
test_cve_score_prefers_v31` / `test_cve_score_falls_back_to_v2`.

`.pwn` sends the `Add-Padding: true` header, so HIBP pads responses to a
uniform size class and a network observer cannot infer the match count from
the response length.

## Configuration

No required or optional key; `is_configured()` returns `True`. The only
credential read is the shared `weather_user_agent` UA in `on_load()` (see
the cross-module naming finding in [ipintel](ipintel.md)).

## State

None owned; nothing persisted or cached. Notably, `_pwn_sync()` never logs
or stores the password - it is hashed and discarded within the call.

## Failure behavior

- `.cve` / `.pwn`: network and parse errors are caught in the sync helpers
  (`requests.RequestException` / `ResponseTooLarge`, then the parse-error
  tuple) and collapse to the literal string `"lookup failed"`; a missing
  CVE (404 or empty `vulnerabilities`) reads `"<id>: not found"`. NVD's
  keyless rate limit (~5 requests / 30 s, noted in the source) is not
  budgeted separately - the per-nick gate is the only throttle, and an NVD
  429 surfaces as `"lookup failed"`.
- Offline commands cannot fail over the network; malformed input produces a
  usage or "invalid" string from the pure helper.

## Security notes

- `.pwn` privacy model (the load-bearing part of this module):
  - k-anonymity: the password is SHA-1 hashed locally and only the first 5
    hex characters of the digest are placed in the URL; the suffix match
    happens client-side against the returned range list. Pinned by
    `test_pwn_only_prefix_sent`, which asserts the suffix never appears in
    the outbound URL. The `usedforsecurity=False` flag on `hashlib.sha1`
    documents that SHA-1 is HIBP's protocol requirement, not a security
    choice.
  - PM-only enforcement: `cmd_pwn()` refuses when `reply_to != nick` (in a
    channel, `reply_to` is the channel), so a password typed in a channel
    is answered only with a NOTICE telling the user never to do that. The
    channel exposure has already happened at that point - the guard limits
    the bot's amplification, it cannot un-send the line.
  - Residual exposure is the IRC transport itself: the password crosses the
    client-server links of the network in the PRIVMSG; that is inherent to
    an IRC command and out of this module's control.
- `.cve` input validation: `_CVE_RE` (`^CVE-\d{4}-\d{4,}$`, case-folded)
  is checked before any request, so junk never reaches NVD and the id needs
  no URL quoting (it rides in `params`).
- Fixed endpoints only; no user-controlled URL or host is ever fetched, so
  `_netsafe` is not needed here.
- Output injection: every upstream-derived reply is passed through
  `strip_ctrl` (whole-line for `.cve`, per the offline helpers' returns in
  the handlers); the `\x02` bold in `.cve` / `.pwn` replies is emitted
  after stripping upstream fields, inside trusted format strings.
- Input caps: `.hashid` / `.cvss` / `.cipher` truncate the argument to
  `_MAX_INPUT` (200) before the pure helper. `.pwn` deliberately does not
  truncate the password (a truncated hash would silently check the wrong
  password); IRC line length bounds it in practice.

## Functions and methods

| Symbol | Purpose |
|---|---|
| `_cve_sync()` | Blocking NVD fetch + one-line format (off-thread) |
| `_cve_score()` | Metric preference ladder v3.1 > v3.0 > v2 |
| `_pwn_sync()` | Local SHA-1, prefix query, client-side suffix match |
| `_hashid()` | Pure: prefix-tagged formats (`$2b$` bcrypt, `$argon2`, `$6$`/`$5$`/`$1$` Unix crypt, `$y$`/`$7$` yescrypt/scrypt, `{SSHA}`), then hex-length table `_HEX_BY_LEN`, then a base64 heuristic |
| `_cvss()` | Pure CVSS v3.1 base-score computation from the vector string: ISS -> impact (scope-dependent polynomial) + exploitability, then spec roundup |
| `_cvss_roundup()` | The v3.1 specification's Roundup pseudocode transcribed exactly (integer-scaled to dodge float artifacts) |
| `_cvss_severity()` | Qualitative rating bands per the spec |
| `_cipher()` | Bundled `_CIPHERS` table lookup with a separator-stripping fallback (`SHA 256` -> `sha-256`) |
| `SecinfoModule.cmd_*()` | Gate -> validate/usage -> (offline helper or `asyncio.to_thread`) -> privmsg |
| `setup()` | Module entry point |

Ordering note in `_cvss()`: an unknown scope value falls into the
scope-unchanged PR table inside the `try`, but the explicit
`scope not in ("U", "C")` check afterwards rejects the vector before any
score is computed, so no wrong result escapes.

## Findings

- questionable | `secinfo.py - _hashid()` | Reply text uses the non-ASCII
  arrow U+2192 as the separator while the rest of the bot's modules format
  with ASCII; cosmetic inconsistency only.
- test-gap | `secinfo.py - SecinfoModule.cmd_pwn()` | The PM-only refusal
  branch (`reply_to != nick`) has no test; `tests/test_secinfo.py` covers
  only the `_pwn_sync` helper, so the one guard that keeps passwords from
  being amplified into a channel is unpinned.
- test-gap | `secinfo.py - _pwn_sync()` | The size-cap branch
  (`len(body) > _HIBP_MAX_BYTES` -> `"lookup failed"`) is not exercised;
  the fake response never exceeds the cap.
