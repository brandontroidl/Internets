"""US-coverage handling for api.weather.gov.

NWS serves US locations only, and says so with two different statuses::

    /alerts/active?point=  400  Parameter "point" is invalid: out of bounds
    /points/{lat},{lon}    404  Data Unavailable For Requested Point

We build every one of these requests ourselves from validated coordinates, so
neither means we sent something malformed - they mean the POINT is unsupported.
Left as an HTTPError either reaches the dispatcher, which records a provider
failure and dings the NWS circuit breaker; enough non-US queries could open it
and leave US alerts falling through to a less authoritative source.  Observed
live: `.al cirus cirus` geocoded to Spain and logged dispatch_fail for nws.

Endpoints signal the same thing a third way - a 200 whose payload simply has
no station, forecast URL or marine zone for the point (an inland location is
never in a marine zone).  Those raise ``OutOfCoverage`` directly.

All three become a ``None`` result, which the dispatcher treats as "a region it
doesn't cover" - falling through to a global provider WITHOUT recording a
failure.  Anything else (401/403/429/5xx) still raises, so a genuine outage,
rate-limit or auth problem stays a failure the breaker can act on.

Upstream stays the authority on its own coverage.  A hardcoded bounding box
for CONUS + Alaska + Hawaii + the territories would drift the moment NWS
changed what it serves, and would have to get the Aleutians' antimeridian
wrap right to boot.
"""
from __future__ import annotations

from .._http import HTTPError, get_json

# Statuses that mean "nothing here for this point", not "something broke".
_NO_DATA_STATUSES = frozenset({400, 404})


class OutOfCoverage(Exception):
    """api.weather.gov has no data for the requested point."""


async def nws_json(url: str, **kw: object) -> object:
    """``get_json`` for NWS, mapping a no-data status to :class:`OutOfCoverage`.

    Every other status still raises ``HTTPError`` - a real outage must stay a
    failure so the breaker can do its job.
    """
    try:
        return await get_json(url, **kw)
    except HTTPError as e:
        if e.status in _NO_DATA_STATUSES:
            raise OutOfCoverage(str(e)) from e
        raise


async def none_if_uncovered(coro: object) -> object:
    """Await *coro*, returning None if the point is outside NWS coverage."""
    try:
        return await coro
    except OutOfCoverage:
        return None
