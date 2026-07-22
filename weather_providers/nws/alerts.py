"""NWS - active weather alerts (most authoritative for US)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
from ._codes import map_severity

# NWS asks API clients to identify themselves with a contact, so a misbehaving
# client can be reached before it is blocked.
_HEADERS = {
    "User-Agent": "Internets IRC Bot (https://github.com/brandontroidl/Internets)",
    "Accept": "application/geo+json",
}

async def fetch(lat: float, lon: float, location: str,
                area: str | None = None) -> AlertsResult:
    """Active NWS alerts for a point, or for a whole state when *area* is set.

    A point lookup returns only the alerts whose polygon covers that exact
    coordinate.  For a state-wide question that is badly misleading: with a
    tropical storm on the Mississippi coast, the point for "mississippi"
    landed inland and returned a single Heat Advisory while ``area=MS``
    returned 15 alerts including three Tropical Storm Warnings.  ``area`` and
    ``point`` are mutually exclusive - sending both narrows straight back to
    the point.
    """
    scope = {"area": area} if area else {"point": f"{lat:.4f},{lon:.4f}"}
    data = await get_json(
        "https://api.weather.gov/alerts/active",
        params={**scope, "status": "actual"},
        headers=_HEADERS,
    )
    alerts = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        alerts.append(AlertEntry(
            event=p.get("event", "Unknown"),
            severity=map_severity(p.get("severity")),
            headline=p.get("headline", ""),
            start=p.get("effective", ""),
            end=p.get("expires", ""),
            description=(p.get("description") or "")[:300],
        ))
    return AlertsResult(source="NWS", location=location, alerts=alerts)
