"""Async HTTP helper for weather providers.

Uses ``aiohttp`` when available for true async I/O.  Falls back to
``requests`` + ``asyncio.to_thread()`` when aiohttp is not installed.
Both paths present the same interface to callers.

Security: All responses are capped at ``_MAX_RESPONSE_BYTES`` to prevent
OOM from malicious or misconfigured API endpoints.  Override per call
via ``max_bytes=`` or globally via ``set_max_response_bytes()``.

Errors: Every transport / status / decoding failure is wrapped in
``HTTPError`` with a ``status`` attribute (None for non-status errors)
and an ``is_rate_limit`` flag, so callers (notably the dispatcher) can
branch on exception *type* instead of string-sniffing repr.
"""

from __future__ import annotations

import asyncio
import atexit
import json as _json
import logging
from typing import Any

log = logging.getLogger("internets.weather.http")

_TIMEOUT = 10  # seconds
# 1 MB default - weather APIs return ~5-50 KB typical.  Configurable
# via set_max_response_bytes() or per-call ``max_bytes=``.
_MAX_RESPONSE_BYTES = 1_048_576

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


# ── Public error types ───────────────────────────────────────────────

class HTTPError(Exception):
    """Uniform HTTP-layer error.

    Attributes:
        status: HTTP status code (int) or None for non-status failures
            (timeouts, DNS errors, JSON decode errors, oversized body).
        provider_hint: caller-supplied tag (typically a URL host) - used
            in dispatcher logs to identify which upstream failed.
        is_rate_limit: True when ``status == 429`` or when the upstream
            signalled rate-limiting in a non-status way (aiohttp's
            TooManyRedirects is *not* this - we mean 429 specifically).
    """
    __slots__ = ("status", "provider_hint", "is_rate_limit")

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        provider_hint: str = "",
        is_rate_limit: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.provider_hint = provider_hint
        self.is_rate_limit = is_rate_limit if status != 429 else True


class ResponseTooLargeError(HTTPError):
    """Raised when an API response exceeds the configured byte cap."""

    def __init__(self, size: int, limit: int, *, provider_hint: str = "") -> None:
        super().__init__(
            f"Response too large ({size} bytes, limit {limit})",
            status=None,
            provider_hint=provider_hint,
            is_rate_limit=False,
        )
        self.size = size
        self.limit = limit


# Back-compat alias - some out-of-tree code or tests may have imported
# the old private name.  Deprecated; remove once nothing references it.
_ResponseTooLarge = ResponseTooLargeError


# ── Config knobs ─────────────────────────────────────────────────────

def set_max_response_bytes(n: int) -> None:
    """Override the default response-size cap (bytes).  Use sparingly."""
    global _MAX_RESPONSE_BYTES
    if n < 1024:
        raise ValueError("max_bytes too small (<1 KiB)")
    _MAX_RESPONSE_BYTES = int(n)


def get_max_response_bytes() -> int:
    """Return the current default response-size cap (bytes)."""
    return _MAX_RESPONSE_BYTES


# ── Cached aiohttp session ───────────────────────────────────────────

# Per-call ClientSession creation costs ~1 ms + TLS handshake on first
# hit per host.  At dispatcher fan-out across 14 providers that adds
# up.  Cache one session per running event loop - when aiohttp is used
# off a different loop (rare) we transparently create a fresh one.
_session_cache: dict[int, "aiohttp.ClientSession"] = {}
_session_lock = asyncio.Lock() if _HAS_AIOHTTP else None


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


async def _get_session(timeout: int) -> "aiohttp.ClientSession":
    """Return a cached aiohttp ClientSession keyed by event loop."""
    # Bandit B101 - replace `assert _HAS_AIOHTTP` with a real check so
    # the guard survives `python -O`.  This function is only reachable
    # from code paths that already checked the flag, so the raise is
    # purely defensive (broken-invariant report, not user input).
    if not _HAS_AIOHTTP:
        raise RuntimeError(
            "_get_session called without aiohttp - caller forgot to check _HAS_AIOHTTP")
    key = _loop_key()
    sess = _session_cache.get(key)
    if sess is not None and not sess.closed:
        return sess
    async with _session_lock:  # type: ignore[union-attr]
        sess = _session_cache.get(key)
        if sess is not None and not sess.closed:
            return sess
        ct = aiohttp.ClientTimeout(total=timeout)
        sess = aiohttp.ClientSession(timeout=ct)
        _session_cache[key] = sess
        return sess


async def aclose() -> None:
    """Close any cached aiohttp sessions.  Idempotent.

    Call from your application shutdown hook (e.g. bot teardown).  The
    atexit handler below also catches the simple-script case.
    """
    if not _HAS_AIOHTTP:
        return
    for sess in list(_session_cache.values()):
        try:
            if not sess.closed:
                await sess.close()
        except Exception:  # noqa: BLE001
            log.debug("aiohttp session close failed", exc_info=True)
    _session_cache.clear()


def _atexit_close() -> None:
    if not _HAS_AIOHTTP or not _session_cache:
        return
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(aclose())
        finally:
            loop.close()
    except Exception:  # noqa: BLE001
        pass  # nosec B110: best-effort cleanup


atexit.register(_atexit_close)


# ── Public API ───────────────────────────────────────────────────────

async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
    max_bytes: int | None = None,
) -> Any:
    """Async HTTP GET returning parsed JSON.

    Args:
        url: target URL.
        params: query parameters.
        headers: extra request headers.
        timeout: total request timeout in seconds.
        max_bytes: per-call override of the response size cap.  Defaults
            to ``get_max_response_bytes()``.

    Raises:
        HTTPError: for any failure (network, status, decode, oversize).
            Inspect ``.status`` / ``.is_rate_limit`` on the exception.
    """
    cap = max_bytes if max_bytes is not None else _MAX_RESPONSE_BYTES
    hint = _host_of(url)
    if _HAS_AIOHTTP:
        return await _get_json_aiohttp(
            url, params=params, headers=headers, timeout=timeout,
            max_bytes=cap, provider_hint=hint,
        )
    return await _get_json_requests(
        url, params=params, headers=headers, timeout=timeout,
        max_bytes=cap, provider_hint=hint,
    )


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or ""
    except Exception:  # noqa: BLE001
        return ""


# ── aiohttp path ─────────────────────────────────────────────────────

async def _get_json_aiohttp(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: int,
    max_bytes: int,
    provider_hint: str,
) -> Any:
    """aiohttp path - true non-blocking I/O, with cached session."""
    session = await _get_session(timeout)
    try:
        # Per-request timeout (in case cached session was built with a
        # different default), so each call honours its own deadline.
        ct = aiohttp.ClientTimeout(total=timeout)
        async with session.get(
            url, params=params, headers=headers, timeout=ct,
        ) as resp:
            status = resp.status
            if status >= 400:
                # Best-effort, BOUNDED body for log context (never buffer a
                # huge error body).
                body_snip = ""
                try:
                    body_snip = (await resp.content.read(2048)).decode(
                        "utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass  # nosec B110: best-effort cleanup
                raise HTTPError(
                    f"HTTP {status} for {url} {body_snip!r}",
                    status=status,
                    provider_hint=provider_hint,
                    is_rate_limit=(status == 429),
                )
            # SEC-WP-001: stream + cap INCREMENTALLY so an oversize body can't
            # be fully buffered into memory (OOM) before the cap fires.
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLargeError(
                        total, max_bytes, provider_hint=provider_hint)
                chunks.append(chunk)
            body = b"".join(chunks)
            try:
                return _json.loads(body)
            except ValueError as e:
                raise HTTPError(
                    f"JSON decode failed: {e}",
                    status=None,
                    provider_hint=provider_hint,
                ) from e
    except HTTPError:
        raise
    except asyncio.TimeoutError as e:
        raise HTTPError(
            f"Timeout after {timeout}s for {url}",
            status=None, provider_hint=provider_hint,
        ) from e
    except aiohttp.ClientError as e:
        raise HTTPError(
            f"Client error: {type(e).__name__}: {e}",
            status=None, provider_hint=provider_hint,
        ) from e


# ── requests fallback ────────────────────────────────────────────────

def _requests_get(
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: int,
    max_bytes: int,
    provider_hint: str,
) -> Any:
    """Blocking requests path - called via asyncio.to_thread."""
    import requests
    try:
        r = requests.get(url, params=params, headers=headers,
                         timeout=timeout, stream=True)
    except requests.RequestException as e:
        raise HTTPError(
            f"requests error: {type(e).__name__}: {e}",
            status=None, provider_hint=provider_hint,
        ) from e

    status = r.status_code
    if status >= 400:
        body_snip = ""
        try:
            body_snip = r.raw.read(2048, decode_content=True).decode(
                "utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass  # nosec B110: best-effort cleanup
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass  # nosec B110: best-effort cleanup
        raise HTTPError(
            f"HTTP {status} for {url} {body_snip!r}",
            status=status,
            provider_hint=provider_hint,
            is_rate_limit=(status == 429),
        )

    # SEC-WP-001: Read with size cap to prevent OOM.
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                r.close()
                raise ResponseTooLargeError(
                    total, max_bytes, provider_hint=provider_hint)
            chunks.append(chunk)
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass  # nosec B110: best-effort cleanup

    try:
        return _json.loads(b"".join(chunks))
    except ValueError as e:
        raise HTTPError(
            f"JSON decode failed: {e}",
            status=None, provider_hint=provider_hint,
        ) from e


async def _get_json_requests(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: int,
    max_bytes: int,
    provider_hint: str,
) -> Any:
    """requests + to_thread fallback."""
    return await asyncio.to_thread(
        _requests_get, url, params, headers, timeout, max_bytes, provider_hint,
    )
