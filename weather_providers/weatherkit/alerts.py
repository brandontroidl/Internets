"""WeatherKit - weather alerts.

Apple's WeatherKit ``weatherAlerts.alerts`` list returns
``WeatherAlertSummary`` objects.  The descriptive long-form body of an
alert lives behind ``detailsUrl`` - the summary itself only carries
short fields like ``description`` (a one-line headline),
``eventOnsetTime``, ``eventEndTime``, ``severity``, ``certainty``,
``responses``, and ``source`` (issuing agency).

The previous implementation reused ``description`` for the ``event``,
``headline`` AND ``description`` slots; on payloads where the field is
omitted every alert collapsed to "Unknown" everywhere.  Use the
appropriate field per slot, and fall back across documented aliases.
"""
from __future__ import annotations
from .._http import get_json, HTTPError
from ..base import AlertsResult, AlertEntry
_SEV = {"extreme":"extreme","severe":"severe","moderate":"moderate","minor":"minor"}


def _pick(a: dict, *keys: str, default: str = "") -> str:
    """Return the first non-empty string value among the given keys."""
    for k in keys:
        v = a.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return default


async def _fetch_detail(url: str, headers: dict) -> str:
    """Optional per-alert detail-URL fetch.  Best-effort: returns ``""``
    on any failure rather than poisoning the whole alerts call."""
    if not url:
        return ""
    try:
        data = await get_json(url, headers=headers)
    except HTTPError:
        return ""
    if isinstance(data, dict):
        return (data.get("text") or data.get("description") or "")[:300]
    return ""


async def fetch(url, headers, location):
    data = await get_json(url, params={"dataSets": "weatherAlerts"}, headers=headers)
    alerts = []
    for a in data.get("weatherAlerts",{}).get("alerts",[]):
        # fix: previous code read ``description`` for both event and
        # description, and used ``source`` (the issuing agency) as the
        # headline. Use the documented Apple fields per slot, with
        # explicit fallbacks where the summary schema is sparse.
        event = _pick(a, "eventEndDateName", "name", "description", default="Unknown")[:100]
        headline = _pick(a, "description", "name", "source", default="")
        # The summary lacks a long body; fetch the detail URL when
        # present.  Skip when missing - base.AlertEntry tolerates "".
        description = await _fetch_detail(a.get("detailsUrl", ""), headers)
        if not description:
            description = (a.get("description") or "")[:300]
        alerts.append(AlertEntry(
            event=event,
            severity=_SEV.get((a.get("severity") or "").lower(), "unknown"),
            headline=headline,
            start=a.get("effectiveTime", "") or a.get("eventOnsetTime", ""),
            end=a.get("expireTime", "") or a.get("eventEndTime", ""),
            description=description,
        ))
    return AlertsResult(source="Apple Weather", location=location, alerts=alerts)
