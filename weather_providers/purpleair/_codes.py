"""PurpleAir helpers - EPA PM2.5 → AQI conversion and humidity correction.

The breakpoints below are the EPA's **2024** revised PM2.5 AQI table
(effective 2024-05-06): the AQI=50 boundary dropped from 12.0 to 9.0
µg/m³ and the upper AQI 200/300/500 boundaries were lowered to
125.4/225.4/325.4.  Frozen on purpose - these are a regulatory standard,
not a tunable knob.

Source: EPA, "Final Updates to the Air Quality Index (AQI) for
Particulate Matter" fact sheet (Feb 2024).
"""
from __future__ import annotations

# (conc_low, conc_high, aqi_low, aqi_high) - µg/m³ (24-hr PM2.5) → AQI.
_PM25_BREAKPOINTS: tuple[tuple[float, float, int, int], ...] = (
    (0.0,   9.0,   0,   50),
    (9.1,   35.4,  51,  100),
    (35.5,  55.4,  101, 150),
    (55.5,  125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
)


def pm25_to_aqi(conc: float | None) -> int | None:
    """Convert a PM2.5 concentration (µg/m³) to a US EPA AQI value.

    Uses the EPA piecewise-linear formula on the 2024 breakpoints.  The
    concentration is truncated to 0.1 µg/m³ first, per EPA convention.
    Returns 500 above the top breakpoint, and None for None/negative input.
    """
    if conc is None or conc < 0:
        return None
    c = int(conc * 10) / 10.0  # truncate to 0.1 µg/m³
    for c_lo, c_hi, a_lo, a_hi in _PM25_BREAKPOINTS:
        if c <= c_hi:
            return round((a_hi - a_lo) / (c_hi - c_lo) * (c - c_lo) + a_lo)
    return 500


def epa_correct(pa_pm25: float | None, humidity: float | None) -> float | None:
    """Apply the EPA US-wide correction to a raw PurpleAir PM2.5 reading.

    PurpleAir's PA sensors read high; the EPA / Barkjohn (2021) nationwide
    correction aligns them with regulatory monitors::

        PM2.5 = 0.524 * PA - 0.0862 * RH + 5.75

    Applied only when relative humidity is available; otherwise the raw
    value is returned unchanged.  Clamped to >= 0.
    """
    if pa_pm25 is None:
        return None
    if humidity is None:
        return pa_pm25
    corrected = 0.524 * pa_pm25 - 0.0862 * humidity + 5.75
    return corrected if corrected > 0 else 0.0
