"""Tomorrow.io — weather events/alerts.

NOTE: the ``/v4/events`` endpoint is paid-tier only.  Free-tier keys
get 401/403 back.  Rather than letting that error propagate and
poison the dispatcher's fan-out, we catch auth failures and return an
empty ``AlertsResult`` — the registry treats Tomorrow.io as
"no alerts" instead of "alerts errored".  Genuine 5xx and rate-limit
errors are still surfaced so dispatcher health tracking sees them.
"""
from __future__ import annotations
import logging
from .._http import get_json, HTTPError
from ..base import AlertsResult, AlertEntry

log = logging.getLogger("internets.weather.tomorrowio")
_B = "https://api.tomorrow.io/v4"

async def fetch(key, lat, lon, location):
    try:
        data = await get_json(f"{_B}/events", params={"apikey": key, "location": f"{lat},{lon}"})
    except HTTPError as e:
        # fix: /v4/events is paid-tier. Free tier returns 401/403.
        # Degrade to empty alerts instead of bubbling — every other
        # provider's alerts call has to work for the dispatcher to fan
        # out cleanly. Other status codes still propagate.
        if e.status in (401, 403):
            log.debug("Tomorrow.io alerts unavailable on this plan (%s) — returning empty", e.status)
            return AlertsResult(source="Tomorrow.io", location=location)
        raise
    alerts = []
    for ev in data.get("data",{}).get("events",[]):
        alerts.append(AlertEntry(event=ev.get("eventType",ev.get("title","Unknown")), severity=(ev.get("severity") or "unknown").lower(), headline=ev.get("title",""), start=ev.get("startTime",""), end=ev.get("endTime",""), description=(ev.get("description") or "")[:300]))
    return AlertsResult(source="Tomorrow.io", location=location, alerts=alerts)
