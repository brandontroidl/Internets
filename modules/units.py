"""Unit conversion and formatting helpers for weather output."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

WIND_DIRS: list[str] = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def deg_to_card(deg: Optional[float]) -> str:
    """Convert wind direction in degrees to a cardinal abbreviation."""
    if deg is None:
        return ""
    return WIND_DIRS[round(deg / 22.5) % 16]


def cf(c: Optional[float]) -> str:
    """Celsius → 'C / F' string."""
    return f"{c:.1f}C / {c * 9 / 5 + 32:.1f}F" if c is not None else "N/A"


def kph_from_ms(mps: Optional[float]) -> str:
    """Format meters/sec as 'km/h / mph' string."""
    return f"{mps * 3.6:.1f}km/h / {mps * 2.237:.1f}mph" if mps is not None else "N/A"


def kph(k: Optional[float]) -> str:
    """Format km/h as 'km/h / mph' string."""
    return f"{k:.1f}km/h / {k / 1.609:.1f}mph" if k is not None else "N/A"


def km_mi(m: Optional[float]) -> str:
    """Format meters as 'km / mi' string."""
    return f"{m / 1000:.1f}km / {m / 1609.344:.1f}mi" if m is not None else "N/A"


def mb_from_pa(pa: Optional[float]) -> str:
    """Format Pascals as 'mb / inHg' string."""
    return f"{pa / 100:.0f}mb / {pa / 3386.39:.2f}in" if pa is not None else "N/A"


def mb(v: Optional[float]) -> str:
    """Format millibars as 'mb / inHg' string."""
    return f"{v:.0f}mb / {v / 33.864:.2f}in" if v is not None else "N/A"


def fmt_dt(iso: str) -> str:
    """Format an ISO datetime string for display."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%B %d, %I:%M %p %Z"
        )
    except Exception:
        return iso or "N/A"


def fmt_short(iso: str) -> str:
    """Format an ISO datetime as short weekday + time."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%a %I:%M %p"
        ).lstrip("0")
    except Exception:
        return iso or "N/A"
