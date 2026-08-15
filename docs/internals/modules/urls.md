# urls.py - URL shortener (is.gd) and SSRF-safe expander

## Purpose

`UrlsModule` shortens URLs via the keyless is.gd API and expands short URLs by
following redirects under the shared SSRF guard. Base contract:
[base](base.md); SSRF machinery: [_netsafe](_netsafe.md).

## Commands

| Command | Handler | Usage |
|---|---|---|
| `.shorten` | `modules/urls.py - UrlsModule.cmd_shorten()` | `.shorten <url>` -> `Short URL https://is.gd/xxxx` |
| `.expand` / `.unshorten` | `UrlsModule.cmd_expand()` | `.expand <url>` -> `Long URL <final url>` or `URL does not redirect` |

Both require an `http://`/`https://` prefix, take only the first
whitespace-separated token, and are rate-limited.

## Integration

- Shorten: `https://is.gd/create.php?format=json&url=...` via
  `base.fetch_json` (size-capped, UA set), 10 s timeout, no key
  (`modules/urls.py - _shorten_sync()`). Privacy: the user's URL is disclosed
  to is.gd by design.
- Expand: `_netsafe.safe_open("HEAD", url, follow_redirects=True)` - every
  redirect hop is re-parsed, host-validated, and DNS-pinned to the checked IP,
  so a redirect chain cannot land on an internal address
  (`modules/urls.py - _expand_sync()`). The final `resp.url` is
  `strip_ctrl`-sanitized before echoing.
- UA: reuses the weather user agent (`base.cred(cfg, "weather_user_agent",
  "weather", "user_agent", "Internets/1.0")`) - shared identity string, not a
  secret.

## Security notes

The module header comment documents the history: shorten validates the user
URL with `_netsafe.url_is_safe()` FIRST so the bot never asks is.gd to
shorten an internal/metadata address; the old in-module IP-literal pinned
adapter for expand was removed because it broke TLS SNI under urllib3 2.x,
and `_netsafe`'s DNS-pin approach (hostname preserved, resolution pinned)
replaced it. `SSRFBlocked` is collapsed into the generic
"shortening/expansion failed" reply - no oracle for probing internal hosts.

## Failure behavior

All failures (SSRF block, transport error, is.gd error payload) degrade to
one-line replies; is.gd's own `errormessage` is surfaced after `strip_ctrl`.
Both sync workers run via `asyncio.to_thread`, so the loop never blocks.

## Findings

- test-gap | urls.py - UrlsModule | no `tests/test_urls*` exists
  (`tests/test_netsafe.py` covers the SSRF layer itself, not this module's
  use of it).
