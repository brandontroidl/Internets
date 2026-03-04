"""Async HTTP helper for weather providers.

Uses ``aiohttp`` when available for true async I/O.  Falls back to
``requests`` + ``asyncio.to_thread()`` when aiohttp is not installed.
Both paths present the same interface to callers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("internets.weather.http")

_TIMEOUT = 10  # seconds

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
) -> Any:
    """Async HTTP GET returning parsed JSON.

    Raises on HTTP errors or network failures — callers are expected
    to handle exceptions.
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
    """aiohttp path — true non-blocking I/O."""
    ct = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=ct) as session:
        async with session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


def _requests_get(
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: int,
) -> Any:
    """Blocking requests path — called via asyncio.to_thread."""
    import requests
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


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
