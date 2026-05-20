"""Pirate Weather helpers."""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import deg_to_card, ms_to_kph  # noqa: F401
ICONS = {
    "clear-day": "Clear", "clear-night": "Clear",
    "rain": "Rain", "snow": "Snow", "sleet": "Sleet",
    "wind": "Windy", "fog": "Fog", "cloudy": "Cloudy",
    "partly-cloudy-day": "Partly Cloudy", "partly-cloudy-night": "Partly Cloudy",
    "hail": "Hail", "thunderstorm": "Thunderstorm", "tornado": "Tornado",
}
def icon_to_desc(icon): return ICONS.get(icon, icon or "Unknown")


# fix: Pirate Weather is a Dark Sky clone — the API key is part of the
# URL path (``/forecast/{KEY}/{lat,lon}``) and there is no header-based
# auth option. That key lands inside HTTPError messages whenever
# upstream returns 4xx/5xx, and from there into any caller log line.
# Redact it before anything escapes this package.
def _redact_key(s: str, key: str) -> str:
    if not s or not key:
        return s
    return s.replace(key, "[REDACTED]")


async def safe_get_json(url: str, key: str, **kw):
    """``get_json`` wrapper that scrubs the API key from any error text."""
    try:
        return await get_json(url, **kw)
    except HTTPError as e:
        # Rebuild the exception with the key scrubbed out of the
        # message and provider_hint. .status / .is_rate_limit preserved.
        scrubbed = _redact_key(str(e), key)
        new = HTTPError(
            scrubbed,
            status=e.status,
            provider_hint=_redact_key(e.provider_hint, key),
            is_rate_limit=e.is_rate_limit,
        )
        raise new from None
