"""MET Norway - weather alerts (metalerts/2.0, Norway only)."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry

_BASE = "https://api.met.no/weatherapi/metalerts/2.0/current.json"
_HEADERS = {"User-Agent": "Internets-IRC-Bot/2.x github.com/brandontroidl/Internets"}


def _severity(props: dict) -> str:
    # metalerts exposes "severity" and/or "awareness_level"
    # (e.g. "2; yellow; Moderate"). Prefer the explicit severity field.
    sev = props.get("severity")
    if sev:
        return str(sev).lower()
    awl = props.get("awareness_level") or ""
    parts = [p.strip() for p in str(awl).split(";")]
    if len(parts) >= 3 and parts[2]:
        return parts[2].lower()
    return "unknown"


async def fetch(lat: float, lon: float, location: str) -> AlertsResult:
    data = await get_json(_BASE, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                          headers=_HEADERS)
    alerts: list[AlertEntry] = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        when = p.get("interval") or p.get("time") or []
        start = when[0] if isinstance(when, list) and len(when) > 0 else ""
        end = when[1] if isinstance(when, list) and len(when) > 1 else ""
        alerts.append(AlertEntry(
            event=p.get("event") or p.get("eventAwarenessName") or "Unknown",
            severity=_severity(p),
            headline=p.get("title") or p.get("area") or "",
            start=start,
            end=end,
            description=(p.get("description") or "")[:300],
        ))
    return AlertsResult(source="MET Norway", location=location, alerts=alerts)
