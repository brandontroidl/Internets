"""Async HTTP helper for weather providers.

Uses ``aiohttp`` when available for true async I/O.  Falls back to
``requests`` + ``asyncio.to_thread()`` when aiohttp is not installed.
Both paths present the same interface to callers.

Security: All responses are capped at ``_MAX_RESPONSE_BYTES`` to prevent
OOM from malicious or misconfigured API endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("internets.weather.http")

_TIMEOUT = 10  # seconds
_MAX_RESPONSE_BYTES = 1_048_576  # 1 MB — weather APIs return ~5-50 KB typical

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


class _ResponseTooLarge(Exception):
    """Raised when an API response exceeds the size limit."""


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
) -> Any:
    """Async HTTP GET returning parsed JSON.

    Raises on HTTP errors, network failures, or oversized responses.
    Callers are expected to handle exceptions.
    """
    if _HAS_AIOHTTP:
        return await _get_json_aiohttp(url, params=params, headers=headers, timeout=timeout)
    return await _get_json_requests(url, params=params, headers=headers, timeout=timeout)


async def _get_json_aiohttp(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
) -> Any:
    """aiohttp path — true non-blocking I/O.

    Note: Creates a new session per request.  This avoids leaked-session
    issues and is acceptable at weather-command frequency (~1 req/10s).
    """
    ct = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=ct) as session:
        async with session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            # SEC-WP-001: Cap response body to prevent OOM.
            body = await resp.read()
            if len(body) > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLarge(
                    f"Response too large ({len(body)} bytes, limit {_MAX_RESPONSE_BYTES})")
            import json as _json
            return _json.loads(body)


def _requests_get(
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: int,
) -> Any:
    """Blocking requests path — called via asyncio.to_thread."""
    import requests
    r = requests.get(url, params=params, headers=headers, timeout=timeout,
                     stream=True)
    r.raise_for_status()
    # SEC-WP-001: Read with size cap to prevent OOM.
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            r.close()
            raise _ResponseTooLarge(
                f"Response too large (>{_MAX_RESPONSE_BYTES} bytes)")
        chunks.append(chunk)
    import json as _json
    return _json.loads(b"".join(chunks))


async def _get_json_requests(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
) -> Any:
    """requests + to_thread fallback."""
    return await asyncio.to_thread(
        _requests_get, url, params, headers, timeout
    )
