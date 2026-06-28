"""AccuWeather - weather alerts."""
from __future__ import annotations
from .._http import get_json
from ..base import AlertsResult, AlertEntry
# fix: was http:// - leaked apikey in query string on the wire.
_B = "https://dataservice.accuweather.com"

# AccuWeather's Priority field is an integer 1-5 (1 = most severe).
# Mapping to the base.AlertEntry severity vocabulary.
_PRIORITY_TO_SEVERITY = {
    1: "extreme", 2: "severe", 3: "moderate", 4: "minor", 5: "minor",
}

def _severity_from_priority(p) -> str:
    # fix: previously called .lower() on a numeric Priority - TypeError
    # crash. Coerce int, then map; fall back to "unknown" for None/junk.
    if p is None:
        return "unknown"
    try:
        return _PRIORITY_TO_SEVERITY.get(int(p), "unknown")
    except (TypeError, ValueError):
        return str(p).lower() or "unknown"

async def fetch(key, loc_key, location):
    data = await get_json(f"{_B}/alerts/v1/{loc_key}", params={"apikey": key})
    alerts = []
    for a in (data if isinstance(data, list) else []):
        desc = a.get("Area",[{}])[0].get("Text","")[:300] if a.get("Area") else ""
        alerts.append(AlertEntry(
            event=a.get("Description",{}).get("Localized","Unknown"),
            severity=_severity_from_priority(a.get("Priority")),
            headline=a.get("Description",{}).get("Localized",""),
            start=a.get("Area",[{}])[0].get("StartTime","") if a.get("Area") else "",
            end=a.get("Area",[{}])[0].get("EndTime","") if a.get("Area") else "",
            description=desc,
        ))
    return AlertsResult(source="AccuWeather", location=location, alerts=alerts)
