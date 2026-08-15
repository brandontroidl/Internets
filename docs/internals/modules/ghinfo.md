# ghinfo.py - public GitHub repository info (keyless)

One-command wrapper (106 lines) around the unauthenticated GitHub REST API.
Base contract in [base](base.md).

## Commands

| Command | Usage | Reply shape |
|---|---|---|
| `.gh` | `.gh <owner/repo>` e.g. `.gh torvalds/linux` | `**owner/repo** :: ★ N :: forks N :: issues N :: lang L :: license SPDX :: pushed YYYY-MM-DD` |

Handler: rate-limit gate first, usage on empty arg, clip to `_MAX_INPUT`
(120), sync worker via `asyncio.to_thread`.

## Integration

`_fetch_sync()` normalizes the arg (strip whitespace and surrounding `/`),
requires exactly one `/` with non-empty halves (else a usage string - asserted
network-free in `tests/test_ghinfo.py - test_bad_arg_no_slash`), then calls
`GET https://api.github.com/repos/<owner>/<name>` via `base.fetch_json`
(owner and name each `quote(..., safe="")`-encoded, shared UA - GitHub
requires one - `Accept: application/vnd.github+json`, 10 s timeout, default
256 KB cap, `allow_404=True`). Unauthenticated: 60 requests/hour per source
IP, no key path exists. Formatting details: star/fork/issue counts with
thousands separators, `license.spdx_id` over `license.name` with `none`
fallback, `pushed_at` truncated to the date. All string fields
`strip_ctrl`-capped.

## Configuration

None. Keyless; `is_configured()` returns `True`. `on_load()` resolves the
shared `weather_user_agent` credential.

## Failure behavior

404/non-dict payload returns `repo not found: <arg>`; missing fields render
defaults (`lang n/a`, `license none`, `pushed n/a` -
`test_missing_fields_use_defaults`); any exception logs a warning and returns
`lookup failed`. The docstring's "never raises to the caller" contract is
exercised across `tests/test_ghinfo.py` (not-found, malformed, ResponseTooLarge,
transport error, control-char stripping).

## Security notes

Only `owner/repo` and the UA leave the machine; hardcoded host, no SSRF
surface. Upstream names/languages are third-party controlled and
`strip_ctrl`-sanitized (`test_control_chars_stripped` pins that the only bold
markers in the output are the module's own).

## Findings

- questionable | ghinfo.py - `_fetch_sync()` | Unlike pkginfo's
  `_valid_pkg()`, there is no charset or `..` guard on owner/name: `.gh ../x`
  passes the slash-count check and, because `quote` does not encode dots,
  produces `https://api.github.com/repos/../x` - a traversal-shaped path to
  the trusted host. Impact is negligible (read-only public API, server-side
  normalization), but it is the exact pattern pkginfo explicitly blocks, so
  the two modules disagree on the standard.
- test-gap | ghinfo.py - `_fetch_sync()` | The 60 req/hr unauthenticated
  limit surfaces as a 403 from GitHub, which `fetch_json` raises as an HTTP
  error and the module reports as the generic `lookup failed`; no test or
  message distinguishes rate-limit exhaustion from a genuine failure.
