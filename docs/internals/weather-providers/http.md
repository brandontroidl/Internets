# _http.py - capped async HTTP JSON transport for weather providers

## Purpose

The single outbound HTTP path for all 32 provider sub-packages: one function,
`get_json()`, that GETs a URL and returns parsed JSON with a hard byte cap,
uniform exception wrapping (`HTTPError` / `ResponseTooLargeError`), and an
async implementation that prefers `aiohttp` but degrades to
`requests` + `asyncio.to_thread` when aiohttp is not installed.

## Responsibilities / boundaries

Belongs here: transport, timeouts, the response-size cap, session caching,
error normalization. Not here: retries and fallback (the dispatcher treats one
provider failure as reason to try the next provider, so per-request retry
would fight the chain), rate-limit *handling* (this file only tags 429s;
`_health.py` does the accounting), URL construction and auth headers (each
provider endpoint module), and DNS pinning/SSRF guarding - provider URLs are
operator-vetted constants, unlike the user-supplied URLs that go through
`modules/base.py` netsafe machinery.

Relationship to `modules/base.py - fetch_json()`: that is the bot-wide
*synchronous* equivalent (requests-based, 256 KiB default cap) used by the
ordinary IRC modules. `_http.get_json` is the weather stack's own async
counterpart with the same stream-and-cap discipline and a 1 MiB default cap.
They are parallel implementations of the repo's size-cap rule, not a shared
one - `weather_providers` never imports `modules.base`, keeping the package
importable outside the bot.

## Dependencies and dependents

Dependencies: `aiohttp` (optional, detected at import into `_HAS_AIOHTTP`),
`requests` (imported lazily inside `_requests_get`, only on the fallback
path), stdlib `asyncio`, `atexit`, `json`.

Dependents: every provider endpoint module (86 files import `get_json` /
`HTTPError` from `.._http`), `_dispatch.py` (`HTTPError` for failure
classification), `__init__.py` (re-exports the two error types).

## Lifecycle

Imported with the package; import probes for aiohttp and registers
`_atexit_close`. Sessions are created lazily per event loop on first use and
live until `aclose()` or interpreter exit. `aclose()` is the intended
application shutdown hook; the atexit handler is a best-effort net for scripts
that never call it.

## State

- `_MAX_RESPONSE_BYTES` - module-global default cap (1 MiB), settable via
  `set_max_response_bytes()` (floor 1 KiB, so a misconfiguration cannot
  effectively disable the guard).
- `_session_cache: dict[loop_id, aiohttp.ClientSession]` - one cached session
  per running event loop, created under `_session_lock` (double-checked).
- No persistent state.

## Concurrency

`get_json` is a coroutine on the aiohttp path; on the fallback path the
blocking `requests` call is offloaded with `asyncio.to_thread`, so the event
loop never blocks either way. Session creation uses check / lock / re-check to
avoid duplicate sessions under concurrent first calls. `_session_lock` is a
single module-level `asyncio.Lock` shared across all loops (see Findings).
The per-request `aiohttp.ClientTimeout` is passed on every `session.get` so a
cached session built with one timeout still honors each call's own deadline.

## Failure behavior

Everything surfaces as `HTTPError` (or its subclass):

| condition | status attr | is_rate_limit |
|---|---|---|
| HTTP >= 400 | the code | True iff 429 |
| timeout | None | False |
| transport / client error | None | False |
| JSON decode failure | None | False |
| body over cap | None (ResponseTooLargeError) | False |

Error bodies are read at most 2048 bytes and truncated to 200 chars for the
exception message - context without buffering a hostile error page. No
retries, no redirect policy changes; defaults of the underlying library apply.
The dispatcher maps these to health records: 429 -> rate-limit axis, 401/403
-> `mark_auth_failure`, everything else -> plain failure.

## Security

- SEC-WP-001 (tagged in code): the response body is streamed in 64 KiB chunks
  and the cap enforced incrementally on both paths, so an oversized or
  malicious body aborts before it is buffered - the cap is an OOM guard, not
  just a politeness limit.
- `set_max_response_bytes` rejects values under 1 KiB (fail-closed floor).
- Exception messages include the URL; provider URLs can carry API keys as
  query params, which is why `_dispatch._redact()` scrubs key-shaped params
  before logging. The raw exception still holds the full URL - acceptable
  inside the process, but anything new that logs these exceptions must go
  through the redactor.
- `provider_hint` is the URL host only (`_host_of` via `urlparse`), used for
  log attribution without echoing the full URL.

## Classes

### `HTTPError`

Slots exception with `status` (int or None), `provider_hint` (host string),
`is_rate_limit` (forced True whenever `status == 429`, regardless of the
constructor argument - callers cannot accidentally construct an untagged 429).
The uniform type is what lets `_dispatch._is_rate_limit_error` branch on
structure instead of string-sniffing repr.

### `ResponseTooLargeError(HTTPError)`

Adds `size` and `limit`; `status=None`. `_ResponseTooLarge` remains as a
deprecated back-compat alias for out-of-tree importers of the old private
name.

## Functions and methods

### `set_max_response_bytes(n)` / `get_max_response_bytes()`

Global cap override with the 1 KiB floor; getter for symmetry. Nothing in the
repo calls the setter - it is operator/test surface.

### `_loop_key()` / `_get_session(timeout)`

`_loop_key` is `id(asyncio.get_running_loop())` (0 when no loop - only
reachable from sync context, which `get_json` never is). `_get_session`
returns the cached open session for this loop or creates one under the lock.
The leading `if not _HAS_AIOHTTP: raise RuntimeError` replaces a former
`assert` so the guard survives `python -O` (comment cites Bandit B101);
callers all check the flag first, so it is a broken-invariant report.

### `aclose()` / `_atexit_close()`

`aclose` closes and clears all cached sessions, idempotently, swallowing
per-session close errors at debug level. `_atexit_close` spins up a throwaway
event loop at interpreter exit to run `aclose` for the script case; all
failure paths are swallowed (best-effort by design, `nosec`-annotated).

### `get_json(url, *, params, headers, timeout=10, max_bytes=None)`

The public API. Resolves the effective cap (per-call override or module
default), computes the host hint, and delegates to the aiohttp or requests
path. Raises only `HTTPError` subclasses.

### `_get_json_aiohttp(...)`

Cached session; per-request `ClientTimeout`; on status >= 400 reads a bounded
snippet and raises `HTTPError` with the status; otherwise streams
`iter_chunked(65536)` accumulating into `chunks` with the incremental cap
check; `json.loads` on the joined body, `ValueError` wrapped as decode
`HTTPError`. `asyncio.TimeoutError` and `aiohttp.ClientError` are wrapped;
already-`HTTPError` exceptions pass through untouched.

### `_requests_get(...)` / `_get_json_requests(...)`

The blocking twin, run via `asyncio.to_thread`. Uses `stream=True` and
`iter_content(65536)` for the same incremental cap; closes the response in a
`finally` (and on the error branch) so connections are not leaked; error-body
snippet read from `r.raw` with `decode_content=True`. Same exception mapping
as the aiohttp path.

## Implementation walk

- Docstring + constants: contract statement; `_TIMEOUT = 10` is the per-hop
  default the `_dispatch` budget comments build on (NWS 2-3 sequential 10s
  hops under the 30s per-call cap).
- aiohttp probe: compatibility switch, one boolean decided at import.
- Error types: uniform failure surface.
- Config knobs: validated global cap.
- Session cache block: performance (skip ~1 ms + TLS handshake per call on
  long provider chains), with the loop-keyed design and its caveats.
- atexit block: resource cleanup for the non-bot use case.
- `get_json` + `_host_of`: dispatch between paths, host-only attribution.
- The two transport implementations: protocol processing, security
  enforcement (the cap), error handling - deliberately near-identical shape
  so a fix in one is mechanically portable to the other.

## Findings

- questionable | `_http.py - _session_cache` keying | Sessions are keyed by
  `id(loop)` and never evicted when a loop closes; after a loop is
  garbage-collected its id can be reused by a new loop, and the `sess.closed`
  check would then hand back a session bound to the dead loop (aiohttp raises
  on use). Unreachable in the bot (one long-lived loop) but real for test
  suites that create many loops - which is likely why tests monkeypatch
  `get_json` instead of exercising it.
- questionable | `_http.py - _session_lock` | A single module-level
  `asyncio.Lock` is shared across event loops; on Python >= 3.10 the lock
  binds to the first loop that awaits it, so the documented "used off a
  different loop (rare)" scenario would raise RuntimeError at the lock, not
  transparently create a fresh session as the cache comment implies.
- questionable | `_http.py` duplication with `modules/base.py - fetch_json()` |
  Two independent implementations of the stream-and-cap rule with different
  defaults (1 MiB vs 256 KiB); deliberate decoupling, but a future cap-policy
  change must remember both (the repo's one-source-per-fact preference argues
  for at least a cross-reference comment).
- test-gap | `_http.py - get_json()` | No direct unit tests for the cap
  (either path), the 429 tagging, or the requests fallback;
  `tests/test_new_weather_capabilities.py` monkeypatches `get_json` at the
  provider layer, so the transport itself - including
  `ResponseTooLargeError` - is exercised only in production.
