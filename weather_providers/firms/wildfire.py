"""NASA FIRMS - active wildfire detections within a bounding box.

The FIRMS area endpoint returns CSV, not JSON, so we can't use the shared
``get_json`` helper.  Instead we do a small capped urllib fetch on a worker
thread (the repo forbids unbounded reads), decode, and parse with the stdlib
``csv`` module.  Columns are read by name via ``DictReader`` so header order
doesn't matter; the VIIRS_SNPP_NRT product header is::

    latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,
    instrument,confidence,version,bright_ti5,frp,daynight
"""
from __future__ import annotations

import asyncio
import csv
import io
import urllib.error
import urllib.request

from .._http import HTTPError
from ..base import WildfireResult, haversine_km as _haversine_km

_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
_SOURCE = "VIIRS_SNPP_NRT"
_DAYS = 1               # most recent day of detections
_BOX = 0.7             # bounding-box half-size in degrees (~78 km N/S)
_MAX_BYTES = 1_000_000  # response cap (matches _http default ~1 MB)


def _fetch_csv(url: str) -> str:
    """Blocking capped GET returning decoded text - run via to_thread."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310: https literal
            # Read one byte past the cap so we can detect oversize bodies.
            raw = resp.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise HTTPError(f"FIRMS: HTTP {e.code}", status=e.code,
                        provider_hint="firms",
                        is_rate_limit=(e.code == 429)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPError(f"FIRMS: {type(e).__name__}: {e}",
                        status=None, provider_hint="firms") from e
    if len(raw) > _MAX_BYTES:
        raise HTTPError("FIRMS: response too large",
                        status=None, provider_hint="firms")
    return raw.decode("utf-8", "replace")


async def fetch(key, lat, lon, location):
    west, east = lon - _BOX, lon + _BOX
    south, north = lat - _BOX, lat + _BOX
    area = f"{west},{south},{east},{north}"
    url = f"{_BASE}/{key}/{_SOURCE}/{area}/{_DAYS}"

    text = await asyncio.to_thread(_fetch_csv, url)

    # An invalid key / bad request returns a plain-text error rather than
    # a CSV header row - guard against that so we don't silently report 0.
    first = text.splitlines()[0] if text else ""
    if "latitude" not in first:
        raise HTTPError("FIRMS: invalid request or MAP_KEY",
                        status=None, provider_hint="firms")

    reader = csv.DictReader(io.StringIO(text))
    count = 0
    nearest = None
    for row in reader:
        try:
            flat = float(row["latitude"])
            flon = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        count += 1
        d = _haversine_km(lat, lon, flat, flon)
        if nearest is None or d < nearest:
            nearest = d

    # Zero detections is valid data (no fires nearby) - return an empty
    # result, don't raise.  Detection-only source: no names or acreage.
    return WildfireResult(
        source="NASA FIRMS",
        location=location,
        fire_count=count,
        nearest_km=round(nearest, 1) if nearest is not None else None,
    )
