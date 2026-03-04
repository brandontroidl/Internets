"""Unit conversion and formatting helpers for weather output."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

WIND_DIRS: list[str] = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def deg_to_card(deg: Optional[float]) -> str:
    if deg is None:
        return ""
    return WIND_DIRS[round(deg / 22.5) % 16]


def cf(c: Optional[float]) -> str:
    """Celsius → 'C / F' string."""
    return f"{c:.1f}C / {c * 9 / 5 + 32:.1f}F" if c is not None else "N/A"


def kph_from_ms(mps: Optional[float]) -> str:
    return f"{mps * 3.6:.1f}km/h / {mps * 2.237:.1f}mph" if mps is not None else "N/A"


def kph(k: Optional[float]) -> str:
    return f"{k:.1f}km/h / {k / 1.609:.1f}mph" if k is not None else "N/A"


def km_mi(m: Optional[float]) -> str:
    return f"{m / 1000:.1f}km / {m / 1609.344:.1f}mi" if m is not None else "N/A"


def mb_from_pa(pa: Optional[float]) -> str:
    return f"{pa / 100:.0f}mb / {pa / 3386.39:.2f}in" if pa is not None else "N/A"


def mb(v: Optional[float]) -> str:
    return f"{v:.0f}mb / {v / 33.864:.2f}in" if v is not None else "N/A"


def fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%B %d, %I:%M %p %Z"
        )
    except Exception:
        return iso or "N/A"


def fmt_short(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%a %I:%M %p"
        ).lstrip("0")
    except Exception:
        return iso or "N/A"
